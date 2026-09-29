"""Compile Search rollouts into the evidence packet the Analyst reads.

GAFT is the only stage that opens trajectory files for diagnosis. Its output
is rendered task text plus an index. It does not copy trajectories, screenshots,
or result directories, and it is not an input to the Clusterer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gui_harvest.evidence.bundle import build_bundle_v2


def compile_sample(sample, out_dir):
    out_dir = Path(out_dir)
    evidence_dir = out_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for task in sample.get("tasks") or []:
        bundle = build_bundle_v2(task, sample["K_runs"], sample["run_labels"])
        evidence_path = evidence_dir / f"{task['task_id']}.txt"
        evidence_path.write_text(bundle["evidence_text"], encoding="utf-8")
        tasks.append(
            {
                "task_id": task["task_id"],
                "domain": bundle["domain"],
                "stratum": bundle["stratum"],
                "feasible": bundle["feasible"],
                "rewards": {
                    run["label"]: run["reward"] for run in bundle["runs"]
                },
                "evidence_path": str(evidence_path.resolve()),
            }
        )
    index = {
        "schema_version": "gui-harvest-gaft-v1",
        "consumer": "evidence_analyst",
        "tasks": tasks,
    }
    index_path = out_dir / "index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.sample, encoding="utf-8") as source:
        sample = json.load(source)
    index = compile_sample(sample, args.out)
    print(
        f"gaft: compiled {len(index['tasks'])} Search tasks to {args.out}"
    )


if __name__ == "__main__":
    main()
