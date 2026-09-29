"""Project configuration for the public GUI-HARVEST workflow."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path


CONFIG_VERSION = 1
DEFAULT_CONFIG = {
    "project": {
        "name": "gui-harvest-project",
        "state_dir": ".gui-harvest",
    },
    "protocol": {
        "rollouts_k": 3,
        "round_budget": 10,
        "rejection_patience": 5,
    },
    "splits": {
        "search_file": "splits/search.json",
        "validation_file": "splits/validation.json",
        "test_file": "splits/test.json",
        "search": 80,
        "validation": 80,
        "test": 201,
    },
    "harness": {
        "adapter": "agent_s3",
        "baseline_root": "../agent-s3",
        "harness_card": "HARNESS_CARD.md",
        "capability_manifest": "gui-harvest-capability.json",
    },
    "benchmark": {
        "results_root": "../results",
    },
    "rollout": {
        "command": [],
    },
    "models": {
        "analyst": {
            "backend": "openrouter",
            "model": "anthropic/claude-sonnet-5",
            "temperature": 0.0,
            "workers": 4,
        },
        "clusterer": {
            "backend": "openrouter",
            "model": "anthropic/claude-sonnet-5",
            "temperature": 0.0,
            "workers": 1,
        },
        "engineer": {
            "backend": "local",
            "api_backend": "openrouter",
            "model": "anthropic/claude-sonnet-5",
            "temperature": 0.0,
            "command": [],
        },
        "validator": {
            "backend": "openrouter",
            "model": "anthropic/claude-sonnet-5",
            "temperature": 0.0,
            "workers": 4,
            "max_tokens": 4096,
        },
    },
}


TEMPLATE = """\
config_version = 1

[project]
name = "my-gui-harvest-project"
state_dir = ".gui-harvest"

[protocol]
rollouts_k = 3
round_budget = 10
rejection_patience = 5

[splits]
# OSWorld evaluation suites, fixed before any rollout.
search_file = "splits/search.json"
validation_file = "splits/validation.json"
test_file = "splits/test.json"
search = 80
validation = 80
test = 201

[harness]
adapter = "agent_s3"
baseline_root = "../agent-s3"
harness_card = "HARNESS_CARD.md"
capability_manifest = "gui-harvest-capability.json"

[benchmark]
results_root = "../results"

[rollout]
# Optional argv template used by `gui-harvest rollout run`.
# Available tokens: {harness}, {split_file}, {repeat}, {tag}, {results_root}.
command = []

[models.analyst]
backend = "openrouter"
model = "anthropic/claude-sonnet-5"
temperature = 0.0
workers = 4

[models.clusterer]
backend = "openrouter"
model = "anthropic/claude-sonnet-5"
temperature = 0.0
workers = 1

[models.engineer]
# "local" writes a portable brief for a local coding agent.
# "command" executes the argv below in the candidate tree.
# "api" uses the configured OpenAI-compatible backend and file tools.
backend = "local"
api_backend = "openrouter"
model = "anthropic/claude-sonnet-5"
temperature = 0.0
command = []

[models.validator]
backend = "openrouter"
model = "anthropic/claude-sonnet-5"
temperature = 0.0
workers = 4
max_tokens = 4096
"""


def _merge(defaults, provided):
    result = copy.deepcopy(defaults)
    for key, value in provided.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve(base, value):
    if not value:
        return value
    path = Path(os.path.expanduser(str(value)))
    return str(path if path.is_absolute() else (base / path).resolve())


def load_config(path):
    path = Path(path).resolve()
    with path.open("rb") as source:
        provided = tomllib.load(source)
    if int(provided.get("config_version", CONFIG_VERSION)) != CONFIG_VERSION:
        raise ValueError(f"unsupported config_version in {path}")
    config = _merge(DEFAULT_CONFIG, provided)
    config["config_version"] = CONFIG_VERSION
    config["_path"] = str(path)
    base = path.parent
    config["project"]["state_dir"] = _resolve(
        base, config["project"]["state_dir"]
    )
    for key in ("baseline_root", "capability_manifest"):
        config["harness"][key] = _resolve(base, config["harness"][key])
    config["benchmark"]["results_root"] = _resolve(
        base, config["benchmark"]["results_root"]
    )
    for key in ("search_file", "validation_file", "test_file"):
        config["splits"][key] = _resolve(base, config["splits"].get(key))
    validate_config(config)
    return config


def validate_config(config):
    problems = []
    protocol = config["protocol"]
    for key in ("rollouts_k", "round_budget", "rejection_patience"):
        if int(protocol.get(key, 0)) <= 0:
            problems.append(f"protocol.{key} must be positive")
    if "mode" in protocol:
        problems.append("protocol.mode is not a setting; remove it")
    splits = config["splits"]
    sizes = [int(splits.get(key, 0)) for key in ("search", "validation", "test")]
    if any(size <= 0 for size in sizes):
        problems.append("Search, Validation, and Test sizes must be positive")
    for key in ("search_file", "validation_file", "test_file"):
        if not splits.get(key):
            problems.append(f"splits.{key} is required")
    if not config["harness"].get("baseline_root"):
        problems.append("harness.baseline_root is required")
    card = Path(config["harness"].get("harness_card") or "")
    if not str(card) or card.is_absolute() or ".." in card.parts:
        problems.append(
            "harness.harness_card must be a path inside the harness tree"
        )
    for role in ("analyst", "clusterer", "engineer", "validator"):
        if not config["models"][role].get("backend"):
            problems.append(f"models.{role}.backend is required")
        if not config["models"][role].get("model"):
            problems.append(f"models.{role}.model is required")
    if problems:
        raise ValueError("invalid GUI-HARVEST config:\n- " + "\n- ".join(problems))
    return config


def state_path(config, *parts):
    return os.path.join(config["project"]["state_dir"], *parts)


def write_template(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    path.write_text(TEMPLATE, encoding="utf-8")
    return str(path.resolve())

