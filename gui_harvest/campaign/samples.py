"""Build one canonical repeated-run sample for Analyst, Validator, and gates."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

from gui_harvest.campaign.provenance import sha256_file


def _load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _score(path):
    text = Path(path).read_text(encoding="utf-8").strip()
    try:
        value = float(text)
    except ValueError:
        payload = json.loads(text)
        if isinstance(payload, (int, float)):
            value = float(payload)
        else:
            value = float(
                payload.get("score", payload.get("reward", payload.get("result")))
            )
    if not math.isfinite(value):
        raise ValueError(f"non-finite score in {path}")
    return value


def _tasks(result_root):
    rows = {}
    for result in Path(result_root).rglob("result.txt"):
        task_id = result.parent.name
        if task_id in rows:
            raise ValueError(f"duplicate task {task_id} in {result_root}")
        rows[task_id] = {
            "score": _score(result),
            "task_dir": str(result.parent.resolve()),
            "domain": result.parent.parent.name,
        }
    if not rows:
        raise ValueError(f"no result.txt files under {result_root}")
    return rows


def _compatibility(manifest):
    code = manifest.get("code") or {}
    model = manifest.get("model") or {}
    run = manifest.get("run") or {}
    return {
        "suite_meta_sha256": run.get("suite_meta_sha256"),
        "behavior_sha256": code.get("behavior_sha256") or code.get("sources_sha256"),
        "model_name": model.get("name"),
        "temperature": model.get("temperature"),
        "seed": model.get("seed"),
        "max_steps": run.get("max_steps"),
        "screen_size": run.get("screen_size"),
        "action_space": run.get("action_space"),
        "observation_type": run.get("observation_type"),
    }


def _stratum(values):
    positive = [value > 0 for value in values]
    if not any(positive):
        return "never_positive"
    if all(positive):
        return "always_positive"
    return "flip"


def build_sample(runs, task_ids=None, harness_card=None, expected_k=None):
    """runs is an ordered mapping from run name to completed result root."""
    if expected_k is not None and len(runs) != int(expected_k):
        raise ValueError(f"expected K={expected_k} runs; found {len(runs)}")
    task_maps = {name: _tasks(root) for name, root in runs.items()}
    universes = {name: set(rows) for name, rows in task_maps.items()}
    first = next(iter(universes.values()))
    if any(tasks != first for tasks in universes.values()):
        raise ValueError(
            "run task universes differ: "
            + ", ".join(f"{name}={len(tasks)}" for name, tasks in universes.items())
        )
    selected = set(task_ids) if task_ids is not None else first
    if not selected <= first:
        raise ValueError(f"requested tasks absent from runs: {sorted(selected - first)[:5]}")

    manifests = {}
    compatibility = {}
    for name, root in runs.items():
        path = os.path.join(root, "run_manifest.json")
        manifest = _load(path)
        manifests[name] = {
            "path": os.path.abspath(path),
            "sha256": sha256_file(path),
        }
        compatibility[name] = _compatibility(manifest)
    values = list(compatibility.values())
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"run manifests are incompatible: {compatibility}")

    tasks = []
    for task_id in sorted(selected):
        domains = {task_maps[name][task_id]["domain"] for name in runs}
        if len(domains) != 1:
            raise ValueError(f"{task_id}: domain differs across runs")
        rewards = {
            name: task_maps[name][task_id]["score"] for name in runs
        }
        tasks.append(
            {
                "task_id": task_id,
                "domain": domains.pop(),
                "rewards": rewards,
                "stratum": _stratum(list(rewards.values())),
                "task_dirs": {
                    name: task_maps[name][task_id]["task_dir"] for name in runs
                },
            }
        )
    return {
        "schema_version": "gui-harvest-run-sample-v1",
        "status": "scored",
        "K_runs": {
            name: os.path.abspath(root) for name, root in runs.items()
        },
        "run_labels": {name: chr(65 + index) for index, name in enumerate(runs)},
        "run_manifests": manifests,
        "compatibility": values[0],
        "harness_card": (
            os.path.abspath(harness_card) if harness_card else None
        ),
        "tasks": tasks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        metavar="NAME=RESULT_DIR",
        help="completed repeated run; pass once per repetition",
    )
    parser.add_argument("--task-ids", help="optional JSON list selecting a split")
    parser.add_argument(
        "--split",
        choices=["search", "validation", "test"],
        help="when --task-ids is a GUI-HARVEST split manifest, select this split",
    )
    parser.add_argument("--harness-card")
    parser.add_argument("--expected-k", type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    runs = {}
    for item in args.run:
        name, separator, root = item.partition("=")
        if not separator or not name or not root:
            raise SystemExit(f"invalid --run {item!r}; expected NAME=RESULT_DIR")
        runs[name] = os.path.abspath(root)
    task_ids = _load(args.task_ids) if args.task_ids else None
    if isinstance(task_ids, dict):
        if not args.split:
            raise SystemExit("--split is required for a split manifest")
        split = task_ids.get(args.split) or {}
        task_ids = split.get("task_ids")
        if task_ids is None:
            raise SystemExit(
                f"split manifest does not expose {args.split} task ids"
            )
    if task_ids is not None and not isinstance(task_ids, list):
        raise SystemExit("--task-ids must contain a JSON list or split manifest")
    sample = build_sample(
        runs,
        task_ids,
        harness_card=args.harness_card,
        expected_k=args.expected_k,
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(sample, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {len(sample['tasks'])} tasks x {len(runs)} runs to {args.out}; "
        f"behavior={sample['compatibility']['behavior_sha256']}"
    )


if __name__ == "__main__":
    main()
