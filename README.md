# GUI-HARVEST

GUI-HARVEST automatically improves the executable harness around a frozen
GUI-capable model. It learns from repeated GUI rollouts, discovers recurring
behavior failures, edits harness source, and promotes or rolls back each edit
with fixed score and behavior gates.

The optimized object is the **harness**: prompts, context construction, memory,
action/tool interfaces, control flow, verification, recovery, and termination.
Model weights, benchmark tasks, evaluators, environments, and resource limits
stay frozen.

![GUI-HARVEST overview](assets/gui-harvest-overview.png)

A frozen backbone and the current harness produce repeated Search executions; the Evidence Analyst and
Cross-task Clusterer turn them into failure modes, the Harness Engineer edits
the source with explicit predictions, and the Validator promotes or rolls back
the edit. The Test suite stays sealed until optimization stops.

Protocol parameters (all set in `gui-harvest.toml`):

| parameter | default | meaning |
|---|---:|---|
| `K` | 3 | independent rollouts per task and harness |
| Search | 80 | tasks whose full rollouts the optimizer roles can read |
| Validation | 80 | tasks the outer runner reports as aggregate scores only |
| Test | 201 | sealed; opened once, after optimization stops |
| `B` | 10 | maximum rounds; a round is the search at one fixed harness state |
| `P` | 5 | maximum GUI-evaluated candidate attempts per round |

A candidate is promoted only if ΔSearch ≥ 0, ΔValidation ≥ 0, their maximum is
positive, all required behavioral predictions pass, and L0/L1 checks pass.
See `docs/METHOD.md` for the full contract.

## Setup

GUI-HARVEST does not bundle a benchmark or a harness. Install OSWorld and a seed
harness (for example Agent S3) from their own repositories.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env                              # add your model API key
cp config/gui-harvest.example.toml gui-harvest.toml
set -a && source .env && set +a
```

Create the task splits from OSWorld's `evaluation_examples` (the 361-task suite
is `test_nogdrive.json`). `--seed` is required; `--search`, `--validation`,
and `--test` set the split sizes.

```bash
gui-harvest splits make \
  --examples /path/to/OSWorld/evaluation_examples/examples \
  --universe /path/to/OSWorld/evaluation_examples/test_nogdrive.json \
  --seed <your-seed> --out-dir splits
```

The split is stratified by domain and written as OSWorld suite files
(`{"domain": ["task-id", ...]}`). Keep the files fixed for every backbone.

Then edit `gui-harvest.toml` (`harness.baseline_root`, role models, optional
`rollout.command`; see `docs/CONFIGURATION.md`). The seed harness checkout must
contain a `HARNESS_CARD.md` describing its loop, primitives, and trajectory
fields. For other runtimes see `docs/ADAPTERS.md`.

```bash
gui-harvest init --config gui-harvest.toml
```

## Workflow

Every command takes `--config gui-harvest.toml`; `status` shows the state and
the next step at any time.

**1. Baseline.** `baseline plan` writes the rollout plan; `baseline run` runs it
if `rollout.command` is set, otherwise run the plan with your own evaluator.
Each task directory needs `result.txt`, `traj.jsonl`, `step_*.png`, and each
repeat root a `run_manifest.json`. Then:

```bash
gui-harvest baseline ingest --run r1=/results/base-r1 --run r2=... --run r3=...
```

**2. Start a round.** `round start` prepares the attempt: it runs the Analyst
and Clusterer when fresh evidence is needed, clones a candidate, and writes
`ENGINEER_BRIEF.md`. A rollback keeps the round open at the same harness; only
a promotion starts the next round. `round abort` drops an unevaluated attempt
without consuming one.

**3. Run the Engineer.** Default: `engineer run`, then give the printed
`ENGINEER_BRIEF.md` to a local coding agent (a portable skill is at
`skills/gui-harvest-engineer/SKILL.md`). Alternatively set
`[models.engineer] backend = "command"` (external agent, `{brief}` /
`{candidate}` placeholders) or `"api"` (restricted tool loop). The Engineer
edits only the candidate and never sees Validation or Test details.

**4. Freeze.** `round freeze` runs L0/L1 checks, records the source patch and
predictions, and emits the candidate rollout plan. L0/L1 repairs do not consume
an attempt; an attempt counts toward `P` once a valid candidate enters GUI
evaluation.

**5. Candidate rollouts.** `rollout plan` / `rollout run`, or run the plan
externally. Candidates are evaluated on Search and Validation for `K` repeats.

**6. Validate.**

```bash
gui-harvest round validate --run r1=/results/cand-r1 --run r2=... --run r3=...
```

This checks run identities, builds the Search and Validation samples, runs the
task-local Validator, evaluates the gates, and prints the single next command.

**7. Continue.** `round next` after either outcome: promotion opens attempt 1
of the next round, rollback opens the next attempt of the same round with
ledger feedback. Optimization stops when all `P` attempts of a round are
rejected or after `B` rounds.

**8. Test.** After stopping, `test open --reason <reason>` releases the Test
suite for the frozen selected harness; then `test plan`, `test run` (or run
externally), and `test ingest --run r1=... --run r2=... --run r3=...`. This
writes `.gui-harvest/final-report.json`.

## Package layout

```text
gui_harvest/
├── cli.py         command line (the supported interface)
├── config.py      project configuration
├── llm.py         OpenAI / OpenRouter clients
├── evidence/      trajectory and screenshot evidence, outcome categories
├── roles/         Analyst, Clusterer, Engineer, Validator
├── campaign/      splits, rollouts, checks, promotion, ledger
└── resources/     portable Engineer skill
```

Run a round through `gui-harvest` so workspace files, hashes, and the ledger
stay consistent. Adapters are registered in `gui_harvest/campaign/adapters.py`;
capability manifests define which files the Engineer may edit.

## Harness demos

Coming soon. See `harness_demo/`.

## Development

```bash
python -m unittest discover -s tests -v
```

More: `docs/METHOD.md`, `docs/CONFIGURATION.md`, `docs/ADAPTERS.md`.
