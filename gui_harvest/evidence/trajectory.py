"""Framework-agnostic view over benchmark result trees.

Runners may store the step plan under ``plan`` or ``executor_plan``, and may
place the model name at different path depths. Task directories are therefore
located by ``result.txt``, not by a fixed glob depth.
"""

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

def _infeasible_meta():
    root = os.environ.get("GUI_HARVEST_BENCHMARK_ROOT")
    if not root:
        return ""
    return os.path.join(
        os.path.abspath(root), "evaluation_examples", "test_infeasible.json"
    )


# agent-s injects this bootstrap into every type() call; it drowns out the
# actual action when comparing steps for repetition.
_PYPERCLIP_BOILERPLATE = re.compile(
    r"try:\s*\n\s*import pyperclip.*?import pyperclip\n", re.S
)
_SECTION_HEADER = re.compile(r"^\(([^)\n]{2,40})\)\s*$", re.M)
_PLAN_FIELDS = ("plan", "executor_plan", "response", "natural_language_action")
# A runner that records no separate plan_code keeps the executed program in
# `action` (native pyautogui agents do this). Treat that as the step's code so
# the decisive primitive is recoverable.
_CODE_LIKE = re.compile(r"^[A-Za-z_][\w.]*\s*\(")
_TERMINATE = re.compile(
    r"terminate\s*\(\s*['\"]?(DONE|FAIL|SUCCESS)", re.IGNORECASE
)
# Bare terminal sentinels written instead of a call.
_SENTINELS = {
    "DONE": "agent.done()",
    "SUCCESS": "agent.done()",
    "FAIL": "agent.fail()",
    "WAIT": "agent.wait()",
}


def _step_code(row):
    code = str(row.get("plan_code") or "").strip()
    if not code:
        candidate = str(row.get("action") or "").strip()
        if _CODE_LIKE.match(candidate) or candidate.upper() in _SENTINELS:
            code = candidate
    sentinel = _SENTINELS.get(code.upper())
    if sentinel:
        return sentinel
    match = _TERMINATE.search(code)
    if match:
        return "agent.fail()" if match.group(1).upper() == "FAIL" else "agent.done()"
    return code
# Row keys a stock Agent S3 runner writes; anything else is harness
# telemetry and lands in Step.extra.
STOCK_ROW_KEYS = frozenset(
    {
        "step_num", "action", "plan", "executor_plan", "plan_code", "exec_code",
        "reflection", "info", "code_agent_output", "screenshot_file", "reward",
        "done", "obs", "Error", "action_timestamp", "response",
        "natural_language_action",
    }
)
_SLEEP_ONLY = re.compile(r"^(?:import time;?\s*)?time\.sleep\([\d.]+\)\s*$")


@functools.lru_cache(maxsize=8192)
def _file_digest(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_action(action) -> str:
    if not isinstance(action, str):
        return "" if action is None else str(action)
    return " ".join(_PYPERCLIP_BOILERPLATE.sub("", action).split())


def normalize_text(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def split_plan_sections(plan: Optional[str]) -> Dict[str, str]:
    """Split a `(Section)\\n body` plan into {section_name: body}."""
    if not plan:
        return {}
    marks = list(_SECTION_HEADER.finditer(plan))
    if not marks:
        return {}
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(plan)
        out[m.group(1).strip()] = plan[m.end() : end].strip()
    return out


@functools.lru_cache(maxsize=1)
def infeasible_task_ids() -> frozenset:
    meta = _infeasible_meta()
    if not meta or not os.path.exists(meta):
        return frozenset()
    with open(meta, encoding="utf-8") as f:
        suite = json.load(f)
    return frozenset(tid for ids in suite.values() for tid in ids)


@dataclass
class Step:
    idx: int
    step_num: Optional[int]
    action: str
    action_norm: str
    plan: str
    sections: Dict[str, str]
    plan_code: str
    plan_code_norm: str
    info: dict
    action_outcome: dict
    exec_code: str
    reflection: str
    code_agent_output: str
    screenshot_file: str
    # The code-agent report as written (a dict in Agent-S3: task_instruction,
    # completion_reason, summary, execution_history, ...). `code_agent_output`
    # above is its str(); the analyst's renderer wants the structure back so
    # per-step stderr survives.
    code_agent_record: dict = field(default_factory=dict)
    # Every other key the harness wrote on the row. A harness under
    # optimisation grows fields; keeping them here (unparsed) is what lets the
    # analyst's case renderer show the ones the harness card declares.
    extra: dict = field(default_factory=dict)

    @property
    def next_action_intent(self) -> str:
        return normalize_text(self.sections.get("Next Action", ""))

    @property
    def primitive(self) -> str:
        m = re.match(r"agent\.(\w+)", self.plan_code.strip())
        return m.group(1) if m else ""

    @property
    def is_idle(self) -> bool:
        """The executed step does nothing observable.

        agent-s appends a settle `time.sleep` to most actions, so a substring
        test would flag ordinary clicks and scrolls. Only a step whose entire
        body is the sleep is a no-op -- which covers both a deliberate
        `agent.wait()` and an action that collapsed to nothing after grounding
        produced no call.
        """
        return bool(_SLEEP_ONLY.match(self.action_norm))

    @property
    def is_done(self) -> bool:
        return self.action_norm == "DONE" or bool(self.info.get("done"))

    @property
    def is_fail(self) -> bool:
        return self.action_norm == "FAIL" or bool(self.info.get("fail"))


@dataclass
class Trajectory:
    task_dir: str
    run: str
    domain: str
    task_id: str
    reward: Optional[float]
    instruction: str
    steps: List[Step]
    n_raw_rows: int
    fatal_error: Optional[str]
    max_steps: Optional[int]
    framework: str

    @property
    def failed(self) -> bool:
        return self.reward is not None and self.reward == 0.0

    @property
    def is_infeasible_task(self) -> bool:
        return self.task_id in infeasible_task_ids()

    @property
    def terminated_voluntarily(self) -> bool:
        return bool(self.steps) and (self.steps[-1].is_done or self.steps[-1].is_fail)

    @property
    def budget_exhausted(self) -> bool:
        """Loop ended because steps ran out, not because the agent stopped.

        Reaching max_steps is not sufficient: a trajectory can emit DONE on its
        final permitted step, and counting that as a budget failure would mix
        termination errors into the exploration bucket.
        """
        if not self.max_steps or self.terminated_voluntarily:
            return False
        return len(self.steps) >= self.max_steps

    @functools.cached_property
    def runtime_log(self) -> str:
        path = os.path.join(self.task_dir, "runtime.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    @functools.cached_property
    def screenshot_digests(self) -> List[str]:
        shots = sorted(
            f
            for f in os.listdir(self.task_dir)
            if f.startswith("step_") and f.endswith(".png")
        )
        return [_file_digest(os.path.join(self.task_dir, n)) for n in shots]

    @functools.cached_property
    def observed_digests(self) -> List[Optional[str]]:
        """Digest of the screen each step *produced*, aligned to step idx.

        The harness writes step_{i+1}.png after executing action i, so the file
        recorded on a row is that action's result, not the observation it was
        chosen from. Anything comparing element i against i-1 is therefore comparing consecutive
        outcomes, which is what the repetition counts want, but callers that
        need the screen a step was decided on must take element i-1 (and
        step_0.png for the first). None where the shot is missing.
        """
        out = []
        for s in self.steps:
            path = (
                os.path.join(self.task_dir, s.screenshot_file)
                if s.screenshot_file
                else ""
            )
            out.append(_file_digest(path) if path and os.path.exists(path) else None)
        return out


@dataclass
class Run:
    root: str
    name: str
    max_steps: Optional[int]
    framework: str
    trajectories: List[Trajectory] = field(default_factory=list)


def find_task_dirs(root: str) -> List[str]:
    """Task dirs are wherever traj.jsonl lives; depth varies by framework."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Failed Agent-S3 retries are preserved below task/attempts for audit,
        # but are not separate scored trajectories.
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", "attempts"}]
        if "traj.jsonl" in filenames:
            out.append(dirpath)
    return sorted(out)


def _read_run_args(root: str) -> dict:
    for dirpath, _, filenames in os.walk(root):
        if "args.json" in filenames:
            try:
                with open(
                    os.path.join(dirpath, "args.json"), encoding="utf-8"
                ) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def _read_reward(task_dir: str) -> Optional[float]:
    path = os.path.join(task_dir, "result.txt")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return float(f.read().strip().splitlines()[0])
    except (ValueError, IndexError, OSError):
        return None


def _read_instruction(task_dir: str) -> str:
    path = os.path.join(task_dir, "instruction.txt")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def load_trajectory(
    task_dir: str, run_name: str, max_steps: Optional[int], framework: str
) -> Trajectory:
    rows = []
    fatal_error = None
    bad_json_rows = 0
    try:
        source = open(os.path.join(task_dir, "traj.jsonl"), encoding="utf-8")
    except OSError:
        source = None
    if source is not None:
        with source as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    bad_json_rows += 1
                    continue
                if not isinstance(row, dict):
                    bad_json_rows += 1
                    continue
                rows.append(row)

    # Old retrying runners appended attempts into one file. An initial
    # observation is an unambiguous attempt boundary, so analyze only the last
    # segment, which is the one associated with the final result.txt.
    starts = [i for i, row in enumerate(rows) if row.get("action") == "INITIAL_OBSERVATION"]
    if starts:
        rows = rows[starts[-1] :]
    for row in rows:
        if "Error" in row and len(row) == 1:
            fatal_error = str(row["Error"])

    steps = []
    for row in rows:
        action = row.get("action")
        # The seeded observation is not an agent decision; excluding it keeps
        # step indices aligned with what the agent actually chose to do.
        if action in (None, "INITIAL_OBSERVATION"):
            continue
        plan = ""
        for key in _PLAN_FIELDS:
            if row.get(key):
                plan = row[key]
                break
        plan_code = _step_code(row)
        info = row.get("info")
        steps.append(
            Step(
                idx=len(steps),
                step_num=row.get("step_num"),
                action=str(action),
                action_norm=normalize_action(action),
                plan=plan,
                sections=split_plan_sections(plan),
                plan_code=plan_code,
                plan_code_norm=normalize_text(plan_code),
                info=info if isinstance(info, dict) else {},
                action_outcome=(
                    row.get("action_outcome")
                    if isinstance(row.get("action_outcome"), dict)
                    else {}
                ),
                exec_code=str(row.get("exec_code") or ""),
                reflection=str(row.get("reflection") or ""),
                code_agent_output=str(row.get("code_agent_output") or ""),
                screenshot_file=str(row.get("screenshot_file") or ""),
                code_agent_record=(
                    row["code_agent_output"]
                    if isinstance(row.get("code_agent_output"), dict)
                    else {}
                ),
                extra={k: v for k, v in row.items() if k not in STOCK_ROW_KEYS},
            )
        )

    return Trajectory(
        task_dir=task_dir,
        run=run_name,
        domain=os.path.basename(os.path.dirname(task_dir)),
        task_id=os.path.basename(task_dir),
        reward=_read_reward(task_dir),
        instruction=_read_instruction(task_dir),
        steps=steps,
        n_raw_rows=len(rows),
        fatal_error=fatal_error,
        max_steps=max_steps,
        framework=framework,
    )


def load_run(root: str) -> Run:
    root = os.path.abspath(root.rstrip("/"))
    name = os.path.basename(root)
    args = _read_run_args(root)
    max_steps = args.get("max_steps")
    framework = "agent_s3"
    run = Run(root=root, name=name, max_steps=max_steps, framework=framework)
    for task_dir in find_task_dirs(root):
        run.trajectories.append(
            load_trajectory(task_dir, name, max_steps, framework)
        )
    return run
