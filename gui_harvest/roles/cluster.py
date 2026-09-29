"""Cross-task Clusterer: one constrained partition call per outcome category.

The Analyst already supplies an abstract mechanism and an evidence-backed
counterfactual. The Clusterer only groups findings inside their deterministic
outcome category and writes a falsifiable membership test plus a shared
behavioral target. It never selects source files; the Harness Engineer reads
source code.

Groups seen in fewer than two distinct tasks are residue, not modes. Missing
or duplicate assignments are preserved visibly rather than merged into an
unrelated group.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))

from gui_harvest.evidence.outcome_categories import (
    OUTCOME_CATEGORIES,
    derive_outcome_category,
    normalize_signature,
)
from gui_harvest.campaign.provenance import create_stage_manifest, write_manifest  # noqa: E402

MAX_OUTPUT_TOKENS = 8000
MIN_TASKS = 2  # a group seen in fewer distinct tasks is residue, not a mode
TRAJECTORY_FIELDS = {
    "quotes",
    "evidence",
    "evidence_text",
    "task_dirs",
    "trajectory",
    "screenshots",
    "opening_screens",
    "screen_requests",
    "raw_responses",
}
CLUSTER_FINDING_FIELDS = {
    "finding_id",
    "task_id",
    "domain",
    "outcome_category",
    "signature",
    "mechanism",
    "counterfactual",
    "contrast",
    "did",
    "machine",
    "run",
    "decisive_step",
    "task_stratum",
    "task_feasible",
}

CLUSTER_PROMPT = """\
Outcome category {outcome_category}: {outcome_category_def}.

Below are all {n} findings in this outcome category from {n_tasks} tasks. Each
finding contains an Analyst mechanism, what the record showed, an optional
successful contrast, and an optional evidence-backed counterfactual.

Default to ONE behavioral mode for the outcome category. Split only when the proposed
modes require materially different `behavioral_target` values AND each has a
membership predicate over the analyst finding that the other mode's members
would fail. Application, control kind, task story, literal values, and whether
the run later retried or stopped are not split criteria.

A mode is publishable only if its members span at least TWO distinct task ids.
Put findings that do not support such a cross-task mode in `residue`; do not
invent a named one-task mode for them. Every finding id must appear exactly
once, either in one mode or in residue. You receive analyst findings only.
Trajectory text, screenshots, and result directories are not in this input.

For each group return:
- label: UPPER_SNAKE naming what the runs did.
- mechanism: one cross-task observable mechanism sentence.
- membership_test: a predicate testable from one analyst finding.
- behavioral_target: the smallest shared alternative behavior supported by
  member counterfactuals or scored contrasts; use null when evidence does not
  support one.
- members: finding ids.

Do not name a harness module, patch surface, prompt edit, or task-specific
answer. The Harness Engineer has source code; you do not. `behavioral_target` is what
the AGENT should do differently at that state (for example, verify an unchanged
screen and switch approach), never a wish that the application control should
start working. Findings with the same required agent response belong together
even when their applications and controls differ.

FINDINGS
{findings}

Answer with ONE JSON object and nothing else:
{{"modes": [{{"label": "UPPER_SNAKE", "mechanism": "...",
  "membership_test": "...", "behavioral_target": "..." or null,
  "members": ["finding_id", "..."]}}],
  "residue": ["finding_id", "..."]}}
"""

# ------------------------------------------------------------ io / helpers
def project_findings(source_path, out_path):
    """Write the Clusterer packet: analyst findings, without trajectory fields."""
    projected = []
    with open(source_path, encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            findings = []
            for finding in row.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                findings.append(
                    {
                        key: finding[key]
                        for key in CLUSTER_FINDING_FIELDS
                        if key in finding
                    }
                )
            projected.append(
                {
                    "task_id": row.get("task_id"),
                    "domain": row.get("domain"),
                    "stratum": row.get("stratum"),
                    "feasible": row.get("feasible"),
                    "analysis_status": row.get("analysis_status", "ok"),
                    "findings": findings,
                }
            )
    with open(out_path, "w", encoding="utf-8") as target:
        for row in projected:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
    return str(out_path)


def load_findings(path):
    rows, findings = [], []
    with open(path, encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            leaked = TRAJECTORY_FIELDS & set(row)
            for finding in row.get("findings") or []:
                if isinstance(finding, dict):
                    leaked |= TRAJECTORY_FIELDS & set(finding)
            if leaked:
                raise ValueError(
                    "clusterer input contains trajectory fields "
                    + ", ".join(sorted(leaked))
                )
            rows.append(row)
            # Rows without a status count as analysed; explicit null or dropout
            # rows are never clustered.
            if row.get("analysis_status", "ok") != "ok":
                continue
            for f in row.get("findings") or []:
                f = dict(f)
                f.setdefault("task_stratum", row.get("stratum"))
                f["signature"] = normalize_signature(f.get("signature"))
                f.setdefault("task_feasible", row.get("feasible", True))
                f["outcome_category"] = derive_outcome_category(
                    f["signature"], f["task_feasible"]
                )
                f.setdefault("mechanism", f.get("label") or "")
                f.setdefault("counterfactual", None)
                if "contrast" not in f and isinstance(f.get("divergence"), dict):
                    old = f["divergence"]
                    f["contrast"] = {
                        "reference_run": old.get("reference_run"),
                        "step": old.get("step"),
                        "reference_did": old.get("reference_did"),
                        "primitive_relation": old.get("kind"),
                    }
                findings.append(f)
    return rows, findings


def _c(text, n):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _sig(f):
    s = f.get("signature") or {}
    return f"{s.get('action')}/{s.get('outcome')}/{s.get('noticed')}"


def render_finding(f, chars=300):
    contrast = f.get("contrast") or {}
    lines = [
        f"[{f['finding_id']}] task={f['task_id'][:8]} | {f['domain']} | {f.get('task_stratum')} | run {f['run']} "
        f"| {_sig(f)} | outcome_category={f.get('outcome_category')}",
        f"  did: {_c(f.get('did'), chars)}",
        f"  machine: {_c(f.get('machine'), chars)}",
        f"  mechanism: {_c(f.get('mechanism'), chars)}",
        f"  counterfactual: {_c(f.get('counterfactual'), chars) if f.get('counterfactual') else 'null'}",
    ]
    if contrast:
        lines.append(
            f"  contrast@{contrast.get('step')} ({contrast.get('primitive_relation')}): "
            f"reference did {_c(contrast.get('reference_did'), 180)}"
        )
    return "\n".join(lines)


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


def _canon_label(s):
    return re.sub(r"[^A-Z0-9]+", "_", str(s or "").upper()).strip("_")


def _cache_get(cache_dir, name):
    path = cache_dir and os.path.join(cache_dir, name)
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read()
    return None


def _cache_put(cache_dir, name, text):
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)


class LLM:
    def __init__(self, client, model, cache_dir, temperature=0.0):
        self.client, self.model, self.cache_dir = client, model, cache_dir
        self.temperature = float(temperature)
        self.tokens = collections.Counter()

    def ask(self, name, prompt):
        cached = _cache_get(self.cache_dir, name)
        if cached is not None:
            return cached, "cached"
        for attempt in range(4):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                ch = resp.choices[0]
                text = ch.message.content or ""
                u = getattr(resp, "usage", None)
                if u:
                    self.tokens["in"] += u.prompt_tokens or 0
                    self.tokens["out"] += u.completion_tokens or 0
                if text.strip():
                    _cache_put(self.cache_dir, name, text)
                    return text, getattr(ch, "finish_reason", None)
                print(f"  ! empty response for {name}; retry")
            except Exception as e:  # noqa: BLE001
                print(f"  ! api error {e.__class__.__name__}: {str(e)[:120]}; retry")
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"api failed for {name}")


# ------------------------------------------------------------ partition
def _n_tasks(fs):
    return len({f["task_id"] for f in fs})


def phase_cluster(llm, findings):
    """One constrained partition call per outcome category.

    Invalid modes become residue.
    """
    by_category = collections.defaultdict(list)
    for finding in findings:
        by_category[finding["outcome_category"]].append(finding)
    category_defs = dict(OUTCOME_CATEGORIES)
    modes = []
    for category in category_defs:
        fs = by_category.get(category) or []
        if not fs:
            continue
        base = {"outcome_category": category}
        if _n_tasks(fs) < MIN_TASKS:
            modes.append({
                **base,
                "label": "RESIDUE",
                "mechanism": None,
                "membership_test": None,
                "behavioral_target": None,
                "members": [f["finding_id"] for f in fs],
                "assignment_status": "residue",
            })
            continue
        rendered_findings = "\n\n".join(render_finding(f, 360) for f in fs)
        # The cache key covers everything the model sees, so a revised Analyst
        # row never reuses an answer generated for different evidence.
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "outcome_category": category,
                    "outcome_category_definition": category_defs[category][0],
                    "prompt_template": CLUSTER_PROMPT,
                    "rendered_findings": rendered_findings,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
        text, fin = llm.ask(
            f"cluster_{category}_{fingerprint}.txt",
            CLUSTER_PROMPT.format(
                outcome_category=category,
                outcome_category_def=category_defs[category][0],
                n=len(fs),
                n_tasks=_n_tasks(fs),
                findings=rendered_findings,
            ),
        )
        answer = _json_from(text) or {}
        parsed = answer.get("modes")
        residue_answer = answer.get("residue")
        if not isinstance(parsed, list) or not isinstance(residue_answer, list):
            raise RuntimeError(
                f"cluster response for {category} had invalid mode/residue lists (finish={fin})"
            )
        by_id = {f["finding_id"]: f for f in fs}
        seen = set()
        category_modes = []
        for group in parsed:
            if not isinstance(group, dict):
                continue
            members = [
                member for member in group.get("members") or []
                if member in by_id and member not in seen
            ]
            if not members:
                continue
            if _n_tasks([by_id[member] for member in members]) < MIN_TASKS:
                continue
            seen.update(members)
            target = group.get("behavioral_target")
            target = str(target).strip() if target is not None else ""
            if target.lower() in ("", "null", "none"):
                target = None
            category_modes.append({
                **base,
                "label": _canon_label(group.get("label")) or "UNNAMED_MODE",
                "mechanism": str(group.get("mechanism") or "").strip() or None,
                "membership_test": str(group.get("membership_test") or "").strip() or None,
                "behavioral_target": target,
                "members": members,
                "assignment_status": "model_mode",
            })
        residue = {
            finding_id for finding_id in residue_answer
            if finding_id in by_id and finding_id not in seen
        }
        residue.update(finding["finding_id"] for finding in fs if finding["finding_id"] not in seen)
        if residue:
            category_modes.append({
                **base,
                "label": "RESIDUE",
                "mechanism": None,
                "membership_test": None,
                "behavioral_target": None,
                "members": [f["finding_id"] for f in fs if f["finding_id"] in residue],
                "assignment_status": "residue",
            })
        modes.extend(category_modes)
        n_modes = sum(m["assignment_status"] == "model_mode" for m in category_modes)
        print(
            f"  {category}: {len(fs)} findings / {_n_tasks(fs)} tasks -> "
            f"{n_modes} modes + {len(residue)} residue"
        )
    for index, mode in enumerate(modes, 1):
        mode["mode_id"] = f"M{index:02d}"
    return modes

# ------------------------------------------------------------ counts / report
def recount(modes, findings):
    by_id = {f["finding_id"]: f for f in findings}
    for m in modes:
        fs = [by_id[x] for x in m["members"] if x in by_id]
        tasks = sorted({f["task_id"] for f in fs})
        m["counts"] = {
            "findings": len(fs),
            "tasks": len(tasks),
            "flip_tasks": len({f["task_id"] for f in fs if f.get("task_stratum") == "flip"}),
            "never_positive_tasks": len({f["task_id"] for f in fs if f.get("task_stratum") == "never_positive"}),
            "domains": dict(collections.Counter(f["domain"] for f in fs)),
            "contrast_relations": dict(collections.Counter(
                ((f.get("contrast") or {}).get("primitive_relation") or "none") for f in fs
            )),
            "signatures": dict(collections.Counter(_sig(f) for f in fs)),
            "member_outcome_categories": dict(
                collections.Counter(f["outcome_category"] for f in fs)
            ),
        }
        m["task_ids"] = tasks
        m["member_mechanisms"] = sorted({str(f.get("mechanism") or "") for f in fs})
        # A mode is both cross-task and explicitly asserted as a mode by the
        # model; residue containers never become modes because they span tasks.
        m["is_mode"] = m.get("assignment_status") == "model_mode" and len(tasks) >= MIN_TASKS
        m["patch_candidate"] = m["is_mode"] and bool(m.get("behavioral_target"))
        if not m["is_mode"] and not m.get("mechanism"):
            m["mechanism"] = next((f.get("mechanism") for f in fs if f.get("mechanism")), None)
    return modes


def report(modes, findings, n_tasks):
    by_id = {f["finding_id"]: f for f in findings}
    category_counts = collections.Counter(f["outcome_category"] for f in findings)
    category_tasks = collections.defaultdict(set)
    for f in findings:
        category_tasks[f["outcome_category"]].add(f["task_id"])
    real = [m for m in modes if m["is_mode"]]
    patchable = [m for m in real if m["patch_candidate"]]
    resid = [m for m in modes if not m["is_mode"]]
    covered = sum(m["counts"]["findings"] for m in real)
    lines = [
        f"# Modes -- {len(findings)} findings, {n_tasks} tasks, {len(category_counts)} outcome categories; "
        f"{len(real)} modes (>= {MIN_TASKS} tasks) covering {covered} findings; "
        f"{len(patchable)} have evidence-backed behavioral targets; "
        f"{sum(m['counts']['findings'] for m in resid)} findings in residue\n"
    ]
    lines.append("| outcome category | findings | tasks | modes | residue findings | definition |")
    lines.append("|---|---|---|---|---|---|")
    for category in OUTCOME_CATEGORIES:
        if category_counts.get(category):
            n_modes = sum(1 for m in real if m["outcome_category"] == category)
            n_res = sum(
                m["counts"]["findings"]
                for m in resid
                if m["outcome_category"] == category
            )
            lines.append(
                f"| {category} | {category_counts[category]} | "
                f"{len(category_tasks[category])} | {n_modes} | {n_res} | "
                f"{OUTCOME_CATEGORIES[category][0]} |"
            )
    lines.append("")
    mode_categories = list(
        dict.fromkeys(
            list(OUTCOME_CATEGORIES) + [m["outcome_category"] for m in modes]
        )
    )
    for category in mode_categories:
        category_modes = sorted(
            (m for m in real if m["outcome_category"] == category),
            key=lambda m: -m["counts"]["findings"],
        )
        category_residue = [
            m for m in resid if m["outcome_category"] == category
        ]
        if not category_modes and not category_residue:
            continue
        definition = OUTCOME_CATEGORIES[category][0]
        lines.append(f"## {category} -- {definition}\n")
        for m in category_modes:
            c = m["counts"]
            lines.append(
                f"### {m['mode_id']} {m['label']}  ({c['findings']} findings / {c['tasks']} tasks, "
                f"flip {c['flip_tasks']} / never positive {c['never_positive_tasks']}; "
                f"contrast {c['contrast_relations']})"
            )
            lines.append(f"- mechanism: {m.get('mechanism')}")
            lines.append(f"- membership_test: {m.get('membership_test')}")
            lines.append(f"- behavioral_target: {m.get('behavioral_target')}")
            lines.append(f"- signatures: {c['signatures']}")
            lines.append(f"- tasks: {', '.join(t[:8] for t in m['task_ids'])}")
            lines.append("")
        if category_residue:
            lines.append(
                f"Residue ({sum(m['counts']['findings'] for m in category_residue)} findings; "
                "not a cross-task mode and not patched on):"
            )
            for m in category_residue:
                for member in m["members"]:
                    finding = by_id.get(member) or {}
                    lines.append(
                        f"- [{str(finding.get('task_id') or '?')[:8]}] "
                        f"{_c(finding.get('mechanism'), 180)}"
                    )
            lines.append("")
    return "\n".join(lines)


def main():
    from gui_harvest.llm import make_client

    ap = argparse.ArgumentParser()
    ap.add_argument("--findings", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", default="")
    ap.add_argument(
        "--backend",
        default=os.environ.get("GUI_HARVEST_CLUSTERER_BACKEND")
        or os.environ.get("GUI_HARVEST_LLM_BACKEND", "openrouter"),
    )
    ap.add_argument(
        "--model",
        default=os.environ.get(
            "GUI_HARVEST_CLUSTERER_MODEL", "anthropic/claude-sonnet-5"
        ),
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--cache-dir", default="", help="LLM cache dir (default: <out dir>/cluster_cache)")
    args = ap.parse_args()
    if not args.report:
        args.report = os.path.join(os.path.dirname(os.path.abspath(args.out)), "modes_report.md")

    rows, findings = load_findings(args.findings)
    n_tasks = len({r["task_id"] for r in rows if r.get("findings")})
    categories = collections.Counter(f["outcome_category"] for f in findings)
    print(
        f"clusterer: {len(findings)} findings over {n_tasks} tasks; "
        f"outcome categories: {dict(categories)}"
    )
    cache_dir = args.cache_dir or os.path.join(os.path.dirname(args.out), "cluster_cache")
    llm = LLM(
        make_client(args.backend),
        args.model,
        cache_dir,
        temperature=args.temperature,
    )

    modes = recount(
        phase_cluster(llm, findings),
        findings,
    )
    out = {
        "schema_version": "gui-harvest-cluster-v1",
        "model": args.model, "n_findings": len(findings), "n_tasks": n_tasks,
        "outcome_categories": {
            key: {"findings": value, "definition": OUTCOME_CATEGORIES[key][0]}
            for key, value in categories.items()
        },
        "n_groups": len(modes), "n_modes": sum(1 for m in modes if m["is_mode"]),
        "n_patch_candidates": sum(1 for m in modes if m["patch_candidate"]),
        "n_residue_findings": sum(m["counts"]["findings"] for m in modes if not m["is_mode"]),
        "tokens_this_run": dict(llm.tokens),
        "modes": modes,
    }
    manifest = create_stage_manifest(
        HERE,
        "cluster",
        [args.findings],
        {
            "model": args.model,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "min_tasks": MIN_TASKS,
        },
        CLUSTER_PROMPT,
    )
    manifest["summary"] = {
        "n_findings": len(findings),
        "n_tasks": n_tasks,
        "n_modes": out["n_modes"],
        "n_patch_candidates": out["n_patch_candidates"],
        "n_residue_findings": out["n_residue_findings"],
        "tokens": dict(llm.tokens),
    }
    out["manifest_id"] = manifest["manifest_id"]
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
    open(args.report, "w").write(report(modes, findings, n_tasks))
    manifest_dir = os.path.dirname(os.path.abspath(args.out))
    write_manifest(os.path.join(manifest_dir, "cluster_manifest.json"), manifest)
    write_manifest(
        os.path.join(manifest_dir, f"cluster_manifest_{manifest['manifest_id']}.json"),
        manifest,
    )
    print(
        f"  {out['n_modes']} modes + {out['n_residue_findings']} residue findings "
        f"in {len(categories)} outcome categories; tokens {dict(llm.tokens)}"
    )
    print(f"  wrote {args.out}, {args.report}")


if __name__ == "__main__":
    main()
