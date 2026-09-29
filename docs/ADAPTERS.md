# Harness and rollout adapters

GUI-HARVEST's optimizer roles are harness-agnostic. Runtime integration is
isolated behind a small adapter and one rollout-output contract.

## Harness adapter

Implement `HarnessAdapter` in `gui_harvest/campaign/adapters.py`:

```python
class MyAdapter(HarnessAdapter):
    name = "my_runtime"

    def runtime_behavior_hash(self, harness_root):
        ...

    def harness_card_path(self, harness_root, configured):
        ...

    def clone(self, source, destination):
        ...
```

Register it in `ADAPTERS`. The fingerprint must cover every runtime source that
can change GUI behavior and match the value written by the evaluator's
`run_manifest.json`.

## Rollout output

Each repeat root contains:

```text
run_manifest.json
.../<domain>/<task_id>/
    result.txt
    traj.jsonl
    step_*.png
```

`result.txt` is a finite float or JSON containing `score`, `reward`, or
`result`. `traj.jsonl` contains one JSON step per line. GUI-HARVEST supports
Agent-S3 `plan` and compatible `executor_plan` rows.

Required run-manifest fields:

```json
{
  "code": {"behavior_sha256": "..."},
  "model": {"name": "...", "temperature": 0, "seed": 7},
  "run": {
    "suite_meta_sha256": "...",
    "max_steps": 15,
    "screen_size": [1920, 1080],
    "action_space": "pyautogui",
    "observation_type": "screenshot"
  }
}
```

All K roots must match on these invariants. Before/after may differ only in
`behavior_sha256`.

The upstream Agent S3 runner does not write `run_manifest.json`; the evaluator
writes it next to each repeat root. Compute `behavior_sha256` with the adapter
so it matches what GUI-HARVEST checks:

```python
from gui_harvest.campaign.adapters import get_adapter

behavior_sha256 = get_adapter("agent_s3").runtime_behavior_hash("/path/to/harness")
```

## External evaluator

Leave `rollout.command=[]`. GUI-HARVEST writes:

- exact task IDs;
- selected harness root and fingerprint;
- K and repeat tags;
- expected result contract.

After external execution, pass each resolved repeat root with
`--run NAME=RESULT_DIR`. The audit emits a task/run-specific rerun plan for
missing, invalid, or screenshot-less executions.

