"""Baseline ingestion and campaign split freezing."""

from __future__ import annotations

import json
from pathlib import Path

from gui_harvest.campaign.project import load, save_campaign, state_path, write
from gui_harvest.campaign.provenance import sha256_file
from gui_harvest.campaign.samples import build_sample
from gui_harvest.campaign.rollouts import audit_runs, write_audit


def _optimization_task_ids(config):
    public = load(state_path(config, "splits", "public.json"))
    return sorted(
        set(public["search"]["task_ids"]) | set(public["validation"]["task_ids"])
    )


def ingest_baseline(config, campaign, runs):
    protocol = config["protocol"]
    adapter = config["harness"]["adapter"]
    card = campaign["current_harness"]["harness_card"]["path"]
    expected_ids = _optimization_task_ids(config)
    audit = audit_runs(runs, expected_ids, protocol["rollouts_k"])
    audit_path = state_path(config, "baseline-rollout-audit.json")
    write_audit(audit_path, audit)
    if not audit["complete"]:
        raise ValueError(
            f"baseline rollouts are incomplete; rerun plan written to {audit_path}"
        )
    sample = build_sample(
        runs,
        harness_card=card,
        expected_k=protocol["rollouts_k"],
    )
    expected_behavior = campaign["current_harness"]["runtime_behavior_sha256"]
    observed_behavior = sample["compatibility"].get("behavior_sha256")
    if observed_behavior and observed_behavior != expected_behavior:
        raise ValueError(
            "baseline rollout fingerprint does not match configured harness: "
            f"{observed_behavior} != {expected_behavior}"
        )
    sample_path = state_path(config, "samples", "baseline.json")
    write(sample_path, sample)
    covered = {task["task_id"] for task in sample["tasks"]}
    if covered != set(expected_ids):
        raise ValueError("baseline sample does not match the Search and Validation splits")

    rewards = [
        value
        for task in sample["tasks"]
        for value in (task.get("rewards") or {}).values()
    ]
    campaign["baseline_sample"] = {
        "path": str(Path(sample_path).resolve()),
        "sha256": sha256_file(sample_path),
        "tasks": len(sample["tasks"]),
        "k": protocol["rollouts_k"],
        "mean_utility": sum(rewards) / len(rewards),
        "adapter": adapter,
    }
    campaign["current_sample"] = campaign["baseline_sample"]
    campaign["accounting"]["gui_task_executions"] += (
        len(sample["tasks"]) * protocol["rollouts_k"]
    )
    campaign["status"] = "ready_for_round"
    save_campaign(config, campaign)
    return sample_path, campaign

