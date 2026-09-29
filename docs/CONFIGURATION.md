# Configuration reference

GUI-HARVEST uses one TOML file. Paths are resolved relative to that file.

## `[project]`

- `name`: campaign name.
- `state_dir`: controller artifacts; default `.gui-harvest`.

## `[protocol]`

- `rollouts_k`: valid repeated runs per task/harness, default `3`.
- `round_budget`: maximum optimization rounds `B`, default `10`. A round is the search at one fixed harness state; only a promotion starts the next round.
- `rejection_patience`: maximum GUI-evaluated candidate attempts per round `P`, default `5`. When all `P` attempts of a round are rejected, optimization stops.

Every campaign uses Search, Validation, and sealed Test.

## `[splits]`

- `search_file`, `validation_file`, `test_file`: OSWorld evaluation suites created before any rollout.
- `search`, `validation`, `test`: expected task counts; defaults `80/80/201`.

Create them with `gui-harvest splits make`. The same three files are reused for every backbone.

## `[harness]`

- `adapter`: included values are `agent_s3` and `generic_python`.
- `baseline_root`: seed harness \(H_0\).
- `harness_card`: relative path inside the harness tree. It is cloned,
  fingerprinted, promoted, and rolled back with source.
- `capability_manifest`: editable/create/read-only paths and L1 commands. For
  `agent_s3`, the model client (`gui_agents/s3/core/engine.py`, `core/mllm.py`)
  is read-only. The
  default `agent_s3` manifest compiles `gui_agents/s3` and runs
  `python -m gui_harvest.campaign.smoke`, which imports the harness, checks the
  `AgentS3`/`OSWorldACI` interface the OSWorld runner calls, constructs both
  offline, and runs the candidate's `tests/` when present. L1 uses the Python
  that runs `gui-harvest`, so install the harness dependencies in that
  environment.

If the capability file does not exist, `init` creates a conservative template.

## `[benchmark]`

- `results_root`: rollout output root supplied to command templates.

## `[rollout]`

- `command`: argv list; no shell is used. Tokens:
  `{harness}`, `{split_file}`, `{repeat}`, `{tag}`, `{results_root}`.

If `command` is empty, GUI-HARVEST writes plans for an external evaluator and
ingests the resulting directories. Invalid runs produce a precise rerun audit;
the external evaluator remains responsible for clean snapshots and relaunches.

## `[models.<role>]`

Roles are `analyst`, `clusterer`, `engineer`, and `validator`.

- `backend`: `openai` for the official OpenAI API, or `openrouter` for the official OpenRouter API. Credentials are `OPENAI_API_KEY` and `OPENROUTER_API_KEY`.
- `model`: provider model identifier.
- `temperature`: frozen role decoding temperature.
- `workers`: task-local concurrency where supported.

Engineer backends:

- `local`: write the brief and wait for a local coding agent;
- `command`: execute configured argv with `{brief}`, `{candidate}`, `{round}`;
- `api`: built-in restricted file/command tool loop.

