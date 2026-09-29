"""Task-round bundle: K runs of one task, rendered for the Evidence Analyst.

Principle: the analyst sees exactly what the framework that produced the
rollout wrote down, plus what the benchmark publishes about the task. Nothing
here knows any particular harness's telemetry.

  * TASK CARD. What the task itself states: instruction, domain, related
    apps, feasibility (from the benchmark's published infeasible suite), the
    setup steps, and any hint published with the task. The analyst judges the
    required end state from the instruction; the card never carries the
    benchmark's scoring configuration.

  * STEP ROWS. Per step: the primitive, Δscreen (share of the screenshot that
    changed) and, when the step pointed, Δlocal (the same share inside the
    window around the landing point), the planner's text sections, the
    reflection, the code-agent report, and EVERY OTHER FIELD the framework
    wrote on the row, rendered as `key: value` text under "framework fields".
    A harness card (given separately) explains those fields; this module does
    not interpret them.

  * EVENT-DRIVEN KEYFRAMES. The opening screens are chosen from the run's own
    facts, not by quota: step_0, the last screen, the first screen of every
    no-change stretch, then the largest screen jumps. The cap scales with
    length (≤20 steps: 5, ≤60: 8, longer: 10). Runs that scored get step_0 and
    the last screen only; anything else about them is fetched on request.

  * NO LIBRARY. There is no class vocabulary; labels are named per round.

The bundle is built from explicit task_dirs (one per run), so it can be
pointed at any K result trees.
"""

from __future__ import annotations

import json
import os
from gui_harvest.evidence.trajectory import _read_run_args, load_trajectory
from gui_harvest.evidence.render import (  # noqa: E402
    BIG_CHANGE,
    NO_CHANGE,
    RUN_LABELS,
    _cap,
    _cap_machine,
    _cap_raw,
    _ending,
    _pct,
    change_fractions,
    is_analysis_target,
    local_change_fractions,
    load_crops,
    load_screens,
    landing_point,
    no_change_runs,
    render_code_agent,
    render_measurements,
    select_crops,
    step_no,
)

_CODE_CHARS = 400
_INTENT_CHARS = 700


# ------------------------------------------------------------ keyframes
def keyframe_cap(n_steps):
    if n_steps <= 20:
        return 5
    if n_steps <= 60:
        return 8
    return 10


def select_keyframes(traj, cf, cap=None):
    """Screen indices 0..n chosen from the run's own facts.

    Order of preference: step_0 and last screen; first screen of each
    no-change stretch (the rest of the stretch looks the same); largest
    screen jumps. Nothing here looks at what the screens show.
    """
    n = len(traj.steps)
    cap = cap or keyframe_cap(n)
    chosen = [0, n] if n > 0 else [0]
    if n + 1 <= cap:
        return list(range(n + 1))
    # first screen of a no-change stretch = screen after its first step
    for first, _last in no_change_runs(cf):
        if len(chosen) >= cap:
            break
        if first not in chosen:
            chosen.append(first)
    jumps = sorted(
        (t for t in range(1, n) if cf[t] is not None and cf[t] >= BIG_CHANGE),
        key=lambda t: (-cf[t], t),
    )
    for t in jumps:
        if len(chosen) >= cap:
            break
        if t not in chosen:
            chosen.append(t)
    return sorted(chosen)


# ------------------------------------------------- framework fields (generic)
_FIELD_CHARS = 500
# row keys that are rendered elsewhere or carry nothing the analyst can use
_SKIP_FIELDS = {"reflection_thoughts", "obs", "Error"}


def _compact(v, n=_FIELD_CHARS, machine=False):
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            v = str(v)
    return (_cap_machine if machine else _cap)(str(v), n)


def _machine_field(name):
    name = str(name or "").lower()
    return any(x in name for x in ("output", "stdout", "stderr", "traceback", "terminal", "exception", "error"))


def framework_fields(step):
    """Every non-stock key the framework wrote on this step's row, as text.
    No interpretation: the harness card tells the analyst what they mean."""
    out = []
    for k in sorted(step.extra):
        if k in _SKIP_FIELDS:
            continue
        v = step.extra[k]
        if v in (None, "", "None", {}, []):
            continue
        out.append(f"{k}: {_compact(v, machine=_machine_field(k))}")
    return out


def code_summary(step):
    """Completion reason + first stderr line of a code-agent step, if any."""
    out = step.extra.get("code_agent_output") or step.code_agent_output
    if not out:
        return ""
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except (ValueError, TypeError):
            return f"code agent: {_cap(out, 200)}"
    if not isinstance(out, dict):
        return ""
    reason = out.get("completion_reason") or ""
    summ = str(out.get("summary") or "")
    stderr = ""
    for line in summ.splitlines():
        if "stderr:" in line:
            stderr = line.strip()
            break
    text = f"code agent: {reason}"
    if stderr:
        text += f"; first {_cap(stderr, 200)}"
    return text


def _delta_text(cf, lcf, t):
    text = f"Δscreen={_pct(cf[t])}"
    if lcf and lcf[t] is not None:
        text += f"; at the landing point Δlocal={_pct(lcf[t])}"
    return text


def render_step_v2(s, cf, lcf=None):
    """Render one addressable step block."""
    lines = []
    t = step_no(s)
    lines.append(f"[{t}] {_cap_raw(s.plan_code_norm, _CODE_CHARS) or '(no plan_code)'}")
    lines.append(f"     screen: {_delta_text(cf, lcf, t)}")
    if s.next_action_intent:
        lines.append(f"     intent: {_cap(s.next_action_intent, _INTENT_CHARS)}")
    check = s.sections.get("Previous action verification", "")
    if check:
        lines.append(f"     agent's own check: {_cap(check, _INTENT_CHARS)}")
    if s.reflection:
        lines.append(f"     reflection: {_cap(s.reflection, _INTENT_CHARS)}")
    block = render_code_agent(s)
    if block:
        lines.append(block)
    else:
        cs = code_summary(s)
        if cs:
            lines.append(f"     {cs}")
    ff = framework_fields(s)
    if ff:
        lines.append("     framework fields: " + " | ".join(ff))
    return "\n".join(lines)


def render_steps_v2(traj, cf, lcf=None):
    """One independently addressable block per trajectory step."""
    return "\n\n".join(render_step_v2(s, cf, lcf) for s in traj.steps) + ("\n" if traj.steps else "")


def raw_step_text(step):
    """Untruncated source used for audit only; this is not shown to the model."""
    values = [
        step.plan_code, step.plan, step.reflection, step.code_agent_output,
        step.action, step.exec_code,
    ]
    values.extend(json.dumps(v, ensure_ascii=False, default=str) for v in step.extra.values())
    return "\n".join(str(v) for v in values if v not in (None, ""))


def evidence_regions(bundle):
    """Return run/step/region/layer locations for strict quote matching."""
    regions = [
        {"run": None, "step": None, "region": "task_card", "layer": "rendered", "text": bundle["task_card"]},
        {"run": None, "step": None, "region": "instruction", "layer": "rendered", "text": bundle["instruction"]},
    ]
    for r in bundle["runs"]:
        for s in r["trajectory"].steps:
            t = step_no(s)
            regions.append(
                {"run": r["label"], "step": t, "region": "step", "layer": "rendered",
                 "text": render_step_v2(s, r["cf"], r.get("lcf"))}
            )
            regions.append(
                {"run": r["label"], "step": t, "region": "step", "layer": "raw",
                 "text": raw_step_text(s)}
            )
    return regions


def render_overview_row(traj, cf, lcf=None):
    rows = []
    for s in traj.steps:
        t = step_no(s)
        delta = f"Δ{_pct(cf[t])}"
        if lcf and lcf[t] is not None:
            delta += f" local {_pct(lcf[t])}"
        rows.append(f"  [{t:>2}] {_cap(s.plan_code_norm, 130) or '(no plan_code)'}   {delta}")
    return "\n".join(rows)


# --------------------------------------------------------------- task card
def _examples_path(domain, task_id):
    """Public benchmark example, only when GUI_HARVEST_BENCHMARK_ROOT is set."""
    root = os.environ.get("GUI_HARVEST_BENCHMARK_ROOT")
    if not root:
        return None
    return os.path.join(
        os.path.abspath(root),
        "evaluation_examples",
        "examples",
        domain,
        f"{task_id}.json",
    )


def _setup_summary(config):
    """One line per setup step: its type and a short view of its parameters."""
    out = []
    for c in config or []:
        if not isinstance(c, dict):
            continue
        p = c.get("parameters") or {}
        if isinstance(p, dict):
            brief = {k: v for k, v in p.items() if k in ("command", "files", "path", "url", "urls", "launch")}
            brief = brief or p
        else:
            brief = p
        out.append(f"- {c.get('type')}: {_compact(brief, 240)}")
    return "\n".join(out)


def task_card(domain, task_id, feasible):
    """What the task itself states: domain, apps, feasibility, setup, hint."""
    lines = [
        f"- feasible: {'yes' if feasible else 'no -- the task cannot be done in this environment; the correct ending is fail()'}",
    ]
    path = _examples_path(domain, task_id)
    ex = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                ex = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            ex = {}
    lines.insert(
        0,
        f"- domain: {domain}; related apps: {', '.join(ex.get('related_apps') or []) or '?'}",
    )
    if ex.get("hint"):
        lines.append(f"- hint (published with the task): {_compact(ex['hint'], 400)}")
    setup = _setup_summary(ex.get("config"))
    if setup:
        lines.append("- setup before the run:\n" + setup)
    return "\n".join(lines)


# ---------------------------------------------------------------- bundle
RUN_TEMPLATE = """\
=============================== RUN {label} ===============================
score={reward}; {n_steps} steps of a {max_steps} budget; ended because {ending}
{role}

STEPS
{steps}

COUNTS OVER RUN {label} (arithmetic, reported for every run alike)
{measurements}
"""

EVIDENCE_TEMPLATE = """\
TASK: {instruction}
RUNS THIS ROUND: {n_runs} -- {summary}
TASK STRATUM (from the scores above): {stratum}

TASK CARD (what the task states)
{task_card}

OVERVIEW (one line per step per run; Δ = share of the picture that changed after the step)
{overview}

{run_blocks}"""


def _role_text(reward):
    if is_analysis_target(reward):
        return "SCORED 0 -- ANALYSED (findings may cite this run)"
    return "SCORED -- reference run (a finding's divergence may point at it; it may not be the subject of a finding)"


def build_bundle_v2(task, k_runs, run_labels):
    """task: {"task_id","domain","task_dirs":{run_label_name: dir}}.
    k_runs: {run_name: results_root_path}. run_labels: {run_name: "A"}.
    """
    runs = []
    for run_name in run_labels:
        task_dir = task["task_dirs"].get(run_name)
        if not task_dir:
            continue
        if not os.path.isabs(task_dir):
            # Sample paths start with the results directory name; resolve them
            # next to the run root, falling back to the working directory.
            beside_run = os.path.join(
                os.path.dirname(k_runs[run_name].rstrip("/")), *task_dir.split("/")[1:]
            )
            task_dir = beside_run if os.path.isdir(beside_run) else os.path.abspath(task_dir)
        max_steps = _read_run_args(k_runs[run_name]).get("max_steps") or 15
        traj = load_trajectory(task_dir, run_name, max_steps, "")
        cf = change_fractions(traj)
        lcf = local_change_fractions(traj)
        label = run_labels[run_name]
        target = is_analysis_target(traj.reward)
        if target:
            screens = select_keyframes(traj, cf)
            # Landing-point crops are attached unasked. They are the only view
            # that separates "landed off the control" from "landed on it and
            # nothing happened", and they show small controls that reacted.
            crops = select_crops(traj, cf, lcf)
        else:
            screens = sorted({0, len(traj.steps)})
            crops = []
        runs.append(
            {
                "label": label,
                "run": run_name,
                "task_dir": task_dir,
                "trajectory": traj,
                "reward": traj.reward,
                "n_steps": len(traj.steps),
                "max_steps": traj.max_steps,
                "ending": _ending(traj),
                "analysis_target": target,
                "cf": cf,
                "lcf": lcf,
                "screens": screens,
                "shots": load_screens(traj, screens),
                "crop_points": crops,
                "crops": load_crops(traj, crops) if crops else [],
            }
        )
    runs.sort(key=lambda r: r["label"])
    first = runs[0]["trajectory"]
    n_fail = sum(1 for r in runs if r["analysis_target"])
    if n_fail == len(runs):
        stratum = "never_positive"
    elif n_fail == 0:
        stratum = "always_positive"
    else:
        stratum = "flip"
    summary = "; ".join(f"run {r['label']} score={r['reward']}" for r in runs)
    for r in runs:
        r["block_text"] = RUN_TEMPLATE.format(
            label=r["label"],
            reward=r["reward"],
            n_steps=r["n_steps"],
            max_steps=r["max_steps"],
            ending=r["ending"],
            role=_role_text(r["reward"]),
            steps=render_steps_v2(r["trajectory"], r["cf"], r["lcf"]),
            measurements=render_measurements(r["trajectory"], r["cf"], r["lcf"]),
        )
        r["overview_text"] = (
            f"RUN {r['label']}: score={r['reward']}, {r['n_steps']} steps, ended because {r['ending']}\n"
            + render_overview_row(r["trajectory"], r["cf"], r["lcf"])
        )
        r["evidence_text"] = r["overview_text"] + "\n\n" + r["block_text"]
    feasible = not first.is_infeasible_task
    card = task_card(first.domain, first.task_id, feasible)
    bundle = {
        "task_id": first.task_id,
        "domain": first.domain,
        "instruction": first.instruction,
        "stratum": stratum,
        "feasible": feasible,
        "runs": runs,
        "runs_by_label": {r["label"]: r for r in runs},
        "n_runs": len(runs),
        "task_card": card,
    }
    bundle["evidence_text"] = EVIDENCE_TEMPLATE.format(
        instruction=first.instruction[:1200],
        n_runs=len(runs),
        summary=summary,
        stratum=stratum
        + (" (some runs scored: they are the reference)" if stratum == "flip" else " (no run scored)"),
        task_card=card,
        overview="\n\n".join(r["overview_text"] for r in runs),
        run_blocks="\n".join(r["block_text"] for r in runs),
    )
    bundle["evidence_regions"] = evidence_regions(bundle)
    return bundle


# ------------------------------------------------------------ requests
def parse_requests(raw, bundle, cap):
    """[{"run": label, "step": N, "kind": "before"|"after"|"crop"}]."""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        run = str(item.get("run") or "").strip().upper()[:2]
        step = item.get("step")
        kind = str(item.get("kind") or "after").lower()
        if run not in bundle["runs_by_label"] or isinstance(step, bool) or not isinstance(step, int):
            continue
        n = bundle["runs_by_label"][run]["n_steps"]
        if kind not in ("before", "after", "crop") or not (1 <= step <= n):
            continue
        key = (run, step, kind)
        if key in seen:
            continue
        seen.add(key)
        out.append({"run": run, "step": step, "kind": kind})
        if len(out) >= cap:
            break
    return out


def shots_for_requests(bundle, requests):
    """[(label, kind, screen_index, point, data_url)]."""
    out = []
    for req in requests:
        r = bundle["runs_by_label"][req["run"]]
        traj = r["trajectory"]
        if req["kind"] == "crop":
            pt = landing_point(traj.steps[req["step"] - 1])
            if not pt:
                continue
            for t, which, p, url in load_crops(traj, [(req["step"], pt)]):
                out.append((r["label"], f"crop-{which}", t, p, url))
            continue
        screen = req["step"] - 1 if req["kind"] == "before" else req["step"]
        for t, url, mark in load_screens(traj, [screen]):
            out.append((r["label"], "screen", t, mark, url))
    return out
