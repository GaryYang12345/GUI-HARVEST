"""Persistent campaign state and workspace layout."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from gui_harvest.campaign.adapters import get_adapter
from gui_harvest.config import load_config, state_path
from gui_harvest.campaign.provenance import sha256_file
from gui_harvest.campaign.splits import install_splits


CAMPAIGN_SCHEMA = "gui-harvest-campaign-v1"
LEDGER_SCHEMA = "gui-harvest-update-ledger-v1"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def attempt_dir(config, attempt_number):
    """Workspace of one candidate attempt; attempts are numbered campaign-wide."""
    return state_path(config, "attempts", f"attempt-{int(attempt_number):03d}")


def campaign_path(config):
    return state_path(config, "campaign.json")


def ledger_path(config):
    return state_path(config, "ledger.json")


def default_capability(config):
    adapter = config["harness"]["adapter"]
    card = Path(config["harness"]["harness_card"])
    card_pattern = card.as_posix()
    model_client = []
    if adapter == "agent_s3":
        editable = ["gui_agents/s3/*.py", "gui_agents/s3/**/*.py", card_pattern]
        create = ["gui_agents/s3/*.py", "gui_agents/s3/**/*.py"]
        model_client = ["gui_agents/s3/core/engine.py", "gui_agents/s3/core/mllm.py"]
        l1 = [
            ["{python}", "-m", "compileall", "-q", "gui_agents/s3"],
            ["{python}", "-m", "gui_harvest.campaign.smoke", "--root", "."],
        ]
    else:
        editable = ["*.py", "**/*.py", card_pattern]
        create = ["*.py", "**/*.py"]
        l1 = [["{python}", "-m", "compileall", "-q", "."]]
    return {
        "schema_version": "gui-harvest-capability-manifest-v1",
        "harness_id": Path(config["harness"]["baseline_root"]).name,
        "harness_root": config["harness"]["baseline_root"],
        "editable_paths": editable,
        "create_paths": create,
        "read_only_paths": model_client + [
            "evaluation_examples/**",
            "**/evaluation_examples/**",
            "evaluators/**",
            "**/evaluators/**",
            ".env*",
            "**/.env*",
            ".git/**",
            "**/.git/**",
        ],
        "limits": {
            "max_touched_files": 8,
            "max_changed_lines": 800,
            "allow_dependency_changes": False,
        },
        "forbidden_content_regex": [
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        ],
        "l1_commands": l1,
        "l1_timeout_seconds": 300,
        "invariants": [
            "model weights and decoding settings remain frozen",
            "tasks, evaluators, environment, and resource limits remain frozen",
            "runtime code receives no evaluator outcome or sealed-test data",
        ],
    }


def initialize(config_file):
    config = load_config(config_file)
    root = Path(config["project"]["state_dir"])
    root.mkdir(parents=True, exist_ok=True)
    for directory in ("rounds", "harnesses", "splits", "samples", "logs"):
        (root / directory).mkdir(exist_ok=True)
    capability_path = Path(config["harness"]["capability_manifest"])
    if not capability_path.exists():
        write(capability_path, default_capability(config))
    baseline = Path(config["harness"]["baseline_root"])
    if not baseline.is_dir():
        raise ValueError(f"baseline harness does not exist: {baseline}")
    adapter = get_adapter(config["harness"]["adapter"])
    card = Path(
        adapter.harness_card_path(
            baseline, config["harness"]["harness_card"]
        )
    )
    if not card.is_file():
        raise ValueError(f"Harness Card does not exist: {card}")
    runtime_hash = adapter.runtime_behavior_hash(baseline)
    from gui_harvest.campaign.flow import harness_behavior_hash

    behavior_hash, _behavior_inputs = harness_behavior_hash(
        load(capability_path), str(card)
    )
    public, sealed = install_splits(
        config["splits"]["search_file"],
        config["splits"]["validation_file"],
        config["splits"]["test_file"],
        {
            "search": config["splits"]["search"],
            "validation": config["splits"]["validation"],
            "test": config["splits"]["test"],
        },
    )
    public_path = state_path(config, "splits", "public.json")
    sealed_path = state_path(config, "splits", "sealed-test.json")
    write(public_path, public)
    write(sealed_path, sealed)
    campaign = {
        "schema_version": CAMPAIGN_SCHEMA,
        "project_name": config["project"]["name"],
        "created_at": now(),
        "updated_at": now(),
        "config": {
            "path": str(Path(config_file).resolve()),
            "sha256": sha256_file(config_file),
        },
        "protocol": config["protocol"],
        "splits": {
            "public": {
                "path": str(Path(public_path).resolve()),
                "sha256": sha256_file(public_path),
            },
            "sealed": {
                "path": str(Path(sealed_path).resolve()),
                "sha256": sha256_file(sealed_path),
                "commitment_sha256": sealed["commitment_sha256"],
            },
            "sizes": {
                "search": len(public["search"]["task_ids"]),
                "validation": len(public["validation"]["task_ids"]),
                "test": public["test"]["count"],
            },
        },
        "baseline_sample": None,
        "current_harness": {
            "root": str(baseline.resolve()),
            "adapter": config["harness"]["adapter"],
            "runtime_behavior_sha256": runtime_hash,
            "behavior_hash": behavior_hash,
            "harness_card": {
                "path": str(card.resolve()),
                "sha256": sha256_file(card),
            },
        },
        "optimization_round": 1,
        "attempts_evaluated": 0,
        "current_attempt": None,
        "status": "awaiting_baseline",
        "accounting": {
            "gui_task_executions": 0,
            "candidate_evaluations": 0,
            "promotions": 0,
            "rollbacks": 0,
        },
    }
    write(campaign_path(config), campaign)
    if not Path(ledger_path(config)).exists():
        write(
            ledger_path(config),
            {
                "schema_version": LEDGER_SCHEMA,
                "attempts": [],
                "current_harness": campaign["current_harness"],
            },
        )
    return config, campaign


def load_project(config_file):
    config = load_config(config_file)
    path = campaign_path(config)
    if not os.path.isfile(path):
        raise ValueError(
            f"GUI-HARVEST is not initialized; run `gui-harvest init "
            f"--config {config_file}`"
        )
    return config, load(path)


def save_campaign(config, campaign):
    campaign["updated_at"] = now()
    write(campaign_path(config), campaign)

