"""Run the AI Validator over frozen predictions and post-edit trajectories.

The runner deliberately does not read evaluator results.  Each job is one
prediction x target task and returns a task-level verdict with trajectory
citations.  Deterministic aggregation and promotion remain in validator.py.
"""

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image

from gui_harvest.evidence.render import NO_CHANGE, _pct, screen_change
from gui_harvest.llm import extra_body_for, make_client


SYSTEM_PROMPT = """\
You are the behavioral Validator in a GUI-agent harness experiment.
Judge only whether the frozen behavioral prediction is supported by the
post-edit trajectories. Do not judge task success, utility, or promotion.

Use only the supplied trajectory evidence. Each row is explicitly rendered as
one RESPONSE ROW. Its EFFECT OF PRECEDING ACTION is the screen change measured
from the saved screenshots after the previous action, while its CURRENT
RESPONSE plan/plan_code is the agent's immediate response after seeing that
effect. Do not move forward one row to find the response: it is already in the
same row. Evaluate all supplied runs.

Return exactly one JSON object:
{
  "prediction_id": "...",
  "task_id": "...",
  "verdict": "supported|contradicted|inconclusive",
  "summary": "brief evidence-based explanation",
  "run_assessments": [
    {"run": "A", "verdict": "supported|contradicted|inconclusive",
     "reason": "..."}
  ],
  "citations": [
    {"run": "A", "step": 3, "claim": "what this row proves"}
  ]
}

Rules:
- supported: relevant trigger(s) occurred and the claimed behavior held.
- contradicted: a relevant trigger occurred and at least one clear response
  violated the prediction.
- inconclusive: no relevant trigger occurred, evidence is missing, or the
  observable test cannot be decided from the supplied rows.
- Never infer behavior from evaluator score; no score is supplied.
- Cite exact run labels and step numbers. Do not invent evidence.
- A citation step must identify the RESPONSE ROW whose EFFECT OF PRECEDING
  ACTION and CURRENT RESPONSE support the claim.
"""


def _json_from(text):
    decoder = json.JSONDecoder()
    objects = []
    text = str(text or "")
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        cursor = start + max(end, 1)
    return objects[-1] if objects else None


def _call_chat(backend, model, messages, max_tokens, temperature):
    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    provider = model.split("/", 1)[0] if "/" in model else ""
    extra_body = extra_body_for(provider, False, backend)
    if extra_body:
        kwargs["extra_body"] = extra_body
    response = make_client(backend).chat.completions.create(**kwargs)
    content = response.choices[0].message.content
    usage_record = {
        "input_tokens": int(
            getattr(getattr(response, "usage", None), "prompt_tokens", 0) or 0
        ),
        "output_tokens": int(
            getattr(getattr(response, "usage", None), "completion_tokens", 0) or 0
        ),
        "cache_hit": False,
    }
    if isinstance(content, str):
        return content, usage_record
    if isinstance(content, list):
        return (
            "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            ),
            usage_record,
        )
    return str(content or ""), usage_record


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _clip(value, n):
    text = str(value or "").strip()
    return text if len(text) <= n else text[:n] + "...[truncated]"


def _step_rows(rows):
    return [row for row in rows if isinstance(row.get("step_num"), int) and row["step_num"] > 0]


def _preceding_effects(task_dir, rows):
    """{step: (screen_change, screenshot the agent saw)} for each response row.

    Row s was planned on the screen left by action s-1, so the effect it
    responds to is the change from screen s-2 to screen s-1. Measured from the
    saved screenshots with the same function and thresholds the Analyst uses.
    """
    task_dir = Path(task_dir)
    screens = {0: task_dir / "step_0.png"}
    for row in _step_rows(rows):
        screens[row["step_num"]] = task_dir / str(row.get("screenshot_file") or "")
    effects = {}
    for row in _step_rows(rows):
        step = row["step_num"]
        before, after = screens.get(step - 2), screens.get(step - 1)
        if before is None or after is None or not before.is_file() or not after.is_file():
            effects[step] = (None, None)
            continue
        effects[step] = (screen_change(str(before), str(after)), after)
    return effects


def _trigger(change):
    return isinstance(change, (int, float)) and change < NO_CHANGE


def _effect_text(change):
    if change is None:
        return "none (first step, or a screenshot is missing)"
    verdict = "no visible change" if _trigger(change) else "screen changed"
    return f"screen change {_pct(change)} ({verdict}; threshold {_pct(NO_CHANGE)})"


def _render_run(label, rows, effects):
    lines = [f"RUN {label}"]
    for row in _step_rows(rows):
        step = row["step_num"]
        change, _ = effects.get(step, (None, None))
        lines.extend(
            [
                f"RESPONSE ROW STEP {step}",
                f"EFFECT OF PRECEDING ACTION: {_effect_text(change)}",
                f"CURRENT RESPONSE plan_code: {_clip(row.get('plan_code'), 800)}",
                f"CURRENT RESPONSE plan: {_clip(row.get('plan'), 1800)}",
                f"CURRENT RESPONSE reflection: {_clip(row.get('reflection'), 700)}",
            ]
        )
    return "\n".join(lines)


def _image_url(path):
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > 768:
            im = im.resize((768, round(im.height * 768 / im.width)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _media(effects_by_run):
    """Attach, per run, the screen seen at the first row that responds to no change."""
    content = []
    for label, effects in effects_by_run.items():
        for step in sorted(effects):
            change, seen = effects[step]
            if not _trigger(change):
                continue
            content.append({"type": "text", "text": f"RUN {label} STEP {step}: screen the agent saw"})
            content.append({"type": "image_url", "image_url": {"url": _image_url(seen)}})
            break
    return content


def _validate_verdict(value, prediction_id, task_id, labels, allowed_steps=None):
    if not isinstance(value, dict):
        raise ValueError("model did not return a JSON object")
    verdict = str(value.get("verdict") or "").lower()
    if verdict not in {"supported", "contradicted", "inconclusive"}:
        raise ValueError(f"invalid verdict: {verdict!r}")
    value["prediction_id"] = prediction_id
    value["task_id"] = task_id
    value["verdict"] = verdict
    citations = value.get("citations")
    if not isinstance(citations, list):
        raise ValueError("citations must be a list")
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("run") not in labels:
            raise ValueError(f"invalid citation: {citation!r}")
        if not isinstance(citation.get("step"), int):
            raise ValueError(f"citation step must be an integer: {citation!r}")
        if allowed_steps is not None and citation["step"] not in allowed_steps[
            citation["run"]
        ]:
            raise ValueError(f"citation names a missing trajectory step: {citation!r}")
    return value


def _run_job(job, backend, model, max_tokens, temperature, cache_dir):
    prediction, task, task_dirs, pre_edit_findings = job
    prediction_id = prediction["prediction_id"]
    task_id = task["task_id"]
    rows_by_run = {
        label: _read_jsonl(Path(task_dir) / "traj.jsonl")
        for label, task_dir in task_dirs.items()
    }
    effects_by_run = {
        label: _preceding_effects(task_dirs[label], rows)
        for label, rows in rows_by_run.items()
    }
    evidence = "\n\n".join(
        _render_run(label, rows, effects_by_run[label])
        for label, rows in rows_by_run.items()
    )
    user_text = f"""\
FROZEN PREDICTION
prediction_id: {prediction_id}
task_id: {task_id}
statement: {prediction.get('statement')}
observable_test: {prediction.get('observable_test')}

PRE-EDIT TASK-LOCAL FINDINGS
{_clip(json.dumps(pre_edit_findings, ensure_ascii=False), 8000)}

POST-EDIT TRAJECTORIES
{evidence}
"""
    content = [{"type": "text", "text": user_text}] + _media(effects_by_run)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "system": SYSTEM_PROMPT,
                "user_text": user_text,
                "backend": backend,
                "model": model,
                "max_tokens": max_tokens,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    cache_path = (
        Path(cache_dir) / f"{prediction_id}__{task_id}__{cache_key}.txt"
    )
    usage_path = cache_path.with_suffix(".usage.json")
    raw = cache_path.read_text(encoding="utf-8") if cache_path.exists() else None
    usage = (
        json.loads(usage_path.read_text(encoding="utf-8"))
        if usage_path.exists()
        else {"input_tokens": 0, "output_tokens": 0}
    )
    usage["cache_hit"] = raw is not None
    allowed_steps = {
        label: {
            row.get("step_num")
            for row in rows
            if isinstance(row.get("step_num"), int)
        }
        for label, rows in rows_by_run.items()
    }
    try:
        verdict = _validate_verdict(
            _json_from(raw), prediction_id, task_id, set(task_dirs), allowed_steps
        )
    except ValueError:
        raw, usage = _call_chat(
            backend,
            model,
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
            max_tokens,
            temperature,
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(raw, encoding="utf-8")
        usage_path.write_text(
            json.dumps(usage, indent=2) + "\n", encoding="utf-8"
        )
        verdict = _validate_verdict(
            _json_from(raw), prediction_id, task_id, set(task_dirs), allowed_steps
        )
    verdict["raw_response_path"] = str(cache_path)
    verdict["usage"] = usage
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--post-sample", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument(
        "--findings",
        help="optional frozen Analyst findings.jsonl for pre-edit task evidence",
    )
    ap.add_argument(
        "--backend",
        default=os.environ.get("GUI_HARVEST_VALIDATOR_BACKEND")
        or os.environ.get("GUI_HARVEST_LLM_BACKEND", "openrouter"),
    )
    ap.add_argument(
        "--model",
        default=os.environ.get(
            "GUI_HARVEST_VALIDATOR_MODEL", "anthropic/claude-sonnet-5"
        ),
    )
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    proposal = json.load(open(args.proposal, encoding="utf-8"))
    sample = json.load(open(args.post_sample, encoding="utf-8"))
    tasks = {task["task_id"]: task for task in sample["tasks"]}
    run_labels = sample["run_labels"]
    findings_by_task = {}
    if args.findings:
        with open(args.findings, encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                findings_by_task.setdefault(row.get("task_id"), []).extend(
                    row.get("findings") or []
                )

    jobs = []
    for prediction in proposal["predictions"]:
        if not prediction.get("required_for_promotion", True):
            continue
        for task_id in prediction["target_task_ids"]:
            task = tasks[task_id]
            task_dirs = {
                run_labels[run_name]: task["task_dirs"][run_name]
                for run_name in run_labels
            }
            jobs.append(
                (
                    prediction,
                    task,
                    task_dirs,
                    findings_by_task.get(task_id, []),
                )
            )

    verdicts = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                _run_job,
                job,
                args.backend,
                args.model,
                args.max_tokens,
                args.temperature,
                args.cache_dir,
            )
            for job in jobs
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                verdicts.append(future.result())
            except Exception as error:
                errors.append(
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                )
    verdicts.sort(key=lambda row: (row["prediction_id"], row["task_id"]))

    output = {
        "schema_version": "gui-harvest-validator-verdicts-v1",
        "backend": args.backend,
        "model": args.model,
        "proposal_id": proposal.get("proposal_id"),
        "post_sample": {
            "path": str(Path(args.post_sample).resolve()),
            "sha256": hashlib.sha256(
                Path(args.post_sample).read_bytes()
            ).hexdigest(),
        },
        "pre_edit_findings": (
            {
                "path": str(Path(args.findings).resolve()),
                "sha256": hashlib.sha256(
                    Path(args.findings).read_bytes()
                ).hexdigest(),
            }
            if args.findings
            else None
        ),
        "jobs_expected": len(jobs),
        "jobs_completed": len(verdicts),
        "usage": {
            "input_tokens": sum(
                int((row.get("usage") or {}).get("input_tokens") or 0)
                for row in verdicts
            ),
            "output_tokens": sum(
                int((row.get("usage") or {}).get("output_tokens") or 0)
                for row in verdicts
            ),
            "cache_hits": sum(
                bool((row.get("usage") or {}).get("cache_hit"))
                for row in verdicts
            ),
        },
        "errors": errors,
        "verdicts": verdicts,
    }
    Path(args.out).write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"out": args.out, "jobs": len(verdicts)}, indent=2))
    if errors or len(verdicts) != len(jobs):
        raise SystemExit(
            f"Validator incomplete: {len(verdicts)}/{len(jobs)} jobs, "
            f"{len(errors)} errors; partial output was preserved"
        )


if __name__ == "__main__":
    main()
