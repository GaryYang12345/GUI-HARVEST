"""Audit external rollout directories and produce deterministic rerun plans."""

from __future__ import annotations

import json
import math
from pathlib import Path


def _score(path):
    try:
        text = path.read_text(encoding="utf-8").strip()
        try:
            value = float(text)
        except ValueError:
            payload = json.loads(text)
            if isinstance(payload, (int, float)):
                value = float(payload)
            else:
                value = float(
                    payload.get(
                        "score", payload.get("reward", payload.get("result"))
                    )
                )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return value if math.isfinite(value) else None


def audit_runs(runs, expected_task_ids, expected_k):
    expected = set(expected_task_ids)
    problems = []
    rerun = {}
    if len(runs) != int(expected_k):
        problems.append(f"expected K={expected_k} run roots; found {len(runs)}")
    for run_name, root_value in runs.items():
        root = Path(root_value)
        failed = {}
        if not (root / "run_manifest.json").is_file():
            problems.append(f"{run_name}: missing run_manifest.json")
            failed["__run__"] = ["missing run_manifest.json"]
        task_dirs = {
            path.parent.name: path.parent for path in root.rglob("result.txt")
        }
        for task_id in sorted(expected):
            directory = task_dirs.get(task_id)
            reasons = []
            if directory is None:
                reasons.append("missing result.txt")
            else:
                if _score(directory / "result.txt") is None:
                    reasons.append("invalid/non-finite result")
                if not (directory / "traj.jsonl").is_file():
                    reasons.append("missing traj.jsonl")
                if not any(directory.glob("step_*.png")):
                    reasons.append("missing screenshots")
            if reasons:
                failed[task_id] = reasons
        extras = sorted(set(task_dirs) - expected)
        if extras:
            problems.append(
                f"{run_name}: {len(extras)} unexpected task result(s)"
            )
            failed.setdefault("__run__", []).extend(
                f"unexpected task result: {task_id}" for task_id in extras
            )
        if failed:
            rerun[run_name] = failed
    return {
        "schema_version": "gui-harvest-rollout-audit-v1",
        "complete": not rerun and len(runs) == int(expected_k),
        "expected_k": int(expected_k),
        "expected_tasks": len(expected),
        "problems": problems,
        "rerun": rerun,
    }


def write_audit(path, audit):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

