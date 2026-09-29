"""High-level round and attempt lifecycle used by the public CLI.

A round is the search at one fixed harness state. Each candidate the Engineer
produces in that round is an attempt with its own workspace. A rollback keeps
the round open at the same harness; a promotion advances to the next round.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from gui_harvest.campaign import checks
from gui_harvest.campaign.adapters import get_adapter
from gui_harvest.roles.cluster import project_findings
from gui_harvest.roles.engineer import validate_proposal
from gui_harvest.campaign.flow import (
    attach_proposal,
    close_round,
    discover,
    stopping_status,
)
from gui_harvest.campaign.project import (
    attempt_dir,
    ledger_path,
    load,
    now,
    save_campaign,
    state_path,
    write,
)
from gui_harvest.campaign.provenance import sha256_file
from gui_harvest.campaign.samples import build_sample
from gui_harvest.campaign.rollouts import audit_runs, write_audit


def _run(argv, *, cwd=None, env=None):
    result = subprocess.run(
        [str(value) for value in argv],
        cwd=cwd,
        env=env,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): "
            + " ".join(shlex.quote(str(value)) for value in argv)
        )


def subset_sample(sample, task_ids, *, schema=None):
    selected = set(task_ids)
    tasks = [task for task in sample.get("tasks") or [] if task.get("task_id") in selected]
    found = {task["task_id"] for task in tasks}
    if found != selected:
        raise ValueError(
            "sample is missing split tasks: " + ", ".join(sorted(selected - found)[:5])
        )
    result = dict(sample)
    result["tasks"] = tasks
    if schema:
        result["schema_version"] = schema
    return result


def _split_ids(config):
    public = load(state_path(config, "splits", "public.json"))
    return (
        public["search"]["task_ids"],
        public["validation"]["task_ids"],
    )


def _role_env(config, role):
    settings = config["models"][role]
    env = os.environ.copy()
    env["GUI_HARVEST_LLM_BACKEND"] = settings["backend"]
    return env


def _reuse_evidence(config, campaign):
    if not campaign.get("current_attempt"):
        return None
    previous = Path(attempt_dir(config, campaign["current_attempt"]))
    meta_path = previous / "round-meta.json"
    release_path = previous / "release.json"
    if not meta_path.is_file() or not release_path.is_file():
        return None
    release = load(release_path)
    if release.get("decision") != "rollback":
        return None
    meta = load(meta_path)
    return {
        "findings": meta.get("findings"),
        "modes": meta.get("modes"),
    }


def _write_engineer_brief(config, campaign, number, directory, meta):
    previous_attempts = len((load(ledger_path(config)).get("attempts") or []))
    brief = f"""\
# GUI-HARVEST Harness Engineer — round {meta['round']}, attempt {meta['attempt_in_round']} of {config['protocol']['rejection_patience']}

This workspace is frozen by the GUI-HARVEST controller.

## Read in order

1. `SESSION.json`
2. `engineer_queue.json`
3. `findings.jsonl`
4. `modes.json`
5. `capability.json`
6. `ledger.json`

The ledger contains {previous_attempts} prior GUI-evaluated attempt(s). If a
rollback exists at the current behavior hash, the new `proposal.pre.json` must
contain `prior_attempts_addressed` entries explaining the materially different
mechanism.

## Your job

1. Inspect source only inside the baseline/candidate trees and permitted paths.
2. Select one candidate, or a small compatible set with one shared mechanism.
3. Before editing, write `proposal.pre.json` using schema
   `gui-harvest-intervention-v1`. Freeze targets, source attribution,
   predictions, invariants, and risks.
4. Edit only the candidate tree. Update its source and `HARNESS_CARD.md` in the same edit. Promotion keeps both. Rollback restores both to the pre-edit snapshot.
5. Run local tests as useful, then write `ENGINEER_REPORT.md`.
6. Do not run GUI evaluation and do not decide promotion.

When finished, the operator runs:

```bash
gui-harvest round freeze --config {config['_path']} --attempt {number}
```

## Tool-independent use

- Local coding agent: start it in `{meta['candidate_root']}` and give it this
  file plus the skill printed by `gui-harvest engineer skill` as the task.
- External command/API: `gui-harvest engineer run` uses the configured adapter.
"""
    (Path(directory) / "ENGINEER_BRIEF.md").write_text(brief, encoding="utf-8")


def start_round(config, campaign, *, run_roles=True):
    """Open the next candidate attempt in the current round."""
    if campaign["status"] not in {"ready_for_round", "attempt_closed"}:
        raise ValueError(
            f"campaign status is {campaign['status']!r}; cannot start a new attempt"
        )
    ledger = load(ledger_path(config))
    stop = stopping_status(
        ledger,
        campaign["current_harness"]["behavior_hash"],
        config["protocol"]["round_budget"],
        config["protocol"]["rejection_patience"],
    )
    if stop["stop"]:
        raise ValueError(f"optimization has stopped: {stop['reasons']}")

    number = campaign["attempts_evaluated"] + 1
    directory = Path(attempt_dir(config, number))
    if directory.exists():
        raise FileExistsError(
            f"attempt workspace already exists: {directory}; resume it instead"
        )
    directory.mkdir(parents=True)
    adapter = get_adapter(campaign["current_harness"]["adapter"])
    baseline_root = campaign["current_harness"]["root"]
    snapshot_root = state_path(
        config, "harnesses", f"attempt-{number:03d}-baseline"
    )
    candidate_root = state_path(
        config, "harnesses", f"attempt-{number:03d}-candidate"
    )
    adapter.clone(baseline_root, snapshot_root)
    adapter.clone(snapshot_root, candidate_root)
    card_path = adapter.harness_card_path(
        snapshot_root, config["harness"]["harness_card"]
    )

    capability = load(config["harness"]["capability_manifest"])
    capability["schema_version"] = "gui-harvest-capability-manifest-v1"
    capability["harness_root"] = str(Path(candidate_root).resolve())
    capability["harness_id"] = Path(candidate_root).name
    capability_path = directory / "capability.json"
    write(capability_path, capability)

    current_sample = load(campaign["current_sample"]["path"])
    search_ids, _validation_ids = _split_ids(config)
    search_sample = subset_sample(current_sample, search_ids)
    search_sample["harness_card"] = card_path
    sample_path = directory / "search-sample.json"
    write(sample_path, search_sample)
    gaft_dir = directory / "gaft"

    reused = _reuse_evidence(config, campaign)
    findings_path = directory / "findings.jsonl"
    cluster_findings_path = directory / "cluster-findings.jsonl"
    modes_path = directory / "modes.json"
    if reused:
        shutil.copy2(reused["findings"], findings_path)
        project_findings(findings_path, cluster_findings_path)
        shutil.copy2(reused["modes"], modes_path)
        evidence_source = "reused_after_rollback"
    elif run_roles:
        _run(
            [
                sys.executable,
                "-m",
                "gui_harvest.evidence.gaft",
                "--sample",
                sample_path,
                "--out",
                gaft_dir,
            ]
        )
        analyst = config["models"]["analyst"]
        _run(
            [
                sys.executable,
                "-m",
                "gui_harvest.roles.analyst",
                "--sample",
                sample_path,
                "--gaft",
                gaft_dir / "index.json",
                "--out",
                findings_path,
                "--harness-card",
                card_path,
                "--repo-root",
                Path(config["_path"]).parent,
                "--backend",
                analyst["backend"],
                "--model",
                analyst["model"],
                "--workers",
                analyst["workers"],
                "--temperature",
                analyst["temperature"],
            ],
            env=_role_env(config, "analyst"),
        )
        project_findings(findings_path, cluster_findings_path)
        clusterer = config["models"]["clusterer"]
        _run(
            [
                sys.executable,
                "-m",
                "gui_harvest.roles.cluster",
                "--findings",
                cluster_findings_path,
                "--out",
                modes_path,
                "--report",
                directory / "modes-report.md",
                "--cache-dir",
                directory / "cluster-cache",
                "--backend",
                clusterer["backend"],
                "--model",
                clusterer["model"],
                "--temperature",
                clusterer["temperature"],
            ],
            env=_role_env(config, "clusterer"),
        )
        evidence_source = "fresh_current_harness_analysis"
    else:
        raise ValueError(
            "current harness needs fresh Analyst/Clusterer evidence; "
            "run without --no-roles"
        )

    round_path, _queue_path, round_payload = discover(
        str(sample_path),
        str(findings_path),
        str(modes_path),
        str(capability_path),
        adapter.harness_card_path(
            candidate_root, config["harness"]["harness_card"]
        ),
        str(directory),
        ledger_path(config),
        campaign["splits"]["public"]["path"],
    )
    shutil.copy2(ledger_path(config), directory / "ledger.json")
    meta = {
        "schema_version": "gui-harvest-round-meta-v1",
        "attempt": number,
        "round": stop["current_round"],
        "attempt_in_round": stop["attempts_in_current_round"] + 1,
        "created_at": now(),
        "baseline_root": str(Path(baseline_root).resolve()),
        "baseline_snapshot": str(Path(snapshot_root).resolve()),
        "baseline_runtime_sha256": adapter.runtime_behavior_hash(snapshot_root),
        "baseline_card_sha256": sha256_file(card_path),
        "candidate_root": str(Path(candidate_root).resolve()),
        "current_sample": campaign["current_sample"],
        "search_sample": str(sample_path.resolve()),
        "gaft": str((gaft_dir / "index.json").resolve()) if gaft_dir.is_dir() else None,
        "findings": str(findings_path.resolve()),
        "cluster_findings": str(cluster_findings_path.resolve()),
        "modes": str(modes_path.resolve()),
        "capability": str(capability_path.resolve()),
        "round_state": str(Path(round_path).resolve()),
        "base_behavior_hash": round_payload["stages"]["harness"]["behavior_hash"],
        "evidence_source": evidence_source,
        "status": "awaiting_engineer",
    }
    write(directory / "round-meta.json", meta)
    session = {
        "schema_version": "gui-harvest-engineer-session-v1",
        **meta,
        "ledger": str((directory / "ledger.json").resolve()),
        "queue": str((directory / "engineer_queue.json").resolve()),
        "official_protocol": True,
    }
    write(directory / "SESSION.json", session)
    _write_engineer_brief(config, campaign, number, directory, meta)
    campaign["optimization_round"] = meta["round"]
    campaign["current_attempt"] = number
    campaign["status"] = "awaiting_engineer"
    save_campaign(config, campaign)
    return meta


def run_engineer(config, campaign, number):
    directory = Path(attempt_dir(config, number))
    meta = load(directory / "round-meta.json")
    settings = config["models"]["engineer"]
    backend = settings["backend"]
    if backend == "local":
        result = {
            "status": "manual",
            "brief": str((directory / "ENGINEER_BRIEF.md").resolve()),
            "candidate_root": meta["candidate_root"],
        }
        write(directory / "engineer-run.json", result)
        return result
    if backend == "command":
        command = settings.get("command") or []
        if not command:
            raise ValueError("models.engineer.command is empty")
        replacements = {
            "{brief}": str((directory / "ENGINEER_BRIEF.md").resolve()),
            "{candidate}": meta["candidate_root"],
            "{round}": str(meta["round"]),
            "{attempt}": str(number),
        }
        argv = [
            replacements.get(str(value), str(value))
            for value in command
        ]
        _run(argv, cwd=meta["candidate_root"])
        result = {"status": "completed", "command": argv}
        write(directory / "engineer-run.json", result)
        return result
    if backend == "api":
        from gui_harvest.roles.engineer_agent import run_api_engineer

        result = run_api_engineer(
            backend=settings.get("api_backend")
            or os.environ.get("GUI_HARVEST_LLM_BACKEND", "openrouter"),
            model=settings["model"],
            temperature=settings["temperature"],
            candidate_root=meta["candidate_root"],
            baseline_root=meta["baseline_root"],
            round_root=str(directory),
        )
        write(directory / "engineer-run.json", result)
        return result
    raise ValueError("models.engineer.backend must be local, command, or api")


def freeze_round(config, campaign, number):
    directory = Path(attempt_dir(config, number))
    meta = load(directory / "round-meta.json")
    if meta.get("status") == "awaiting_rollouts":
        return load(directory / "rollout-plan.json")
    pre_path = directory / "proposal.pre.json"
    if not pre_path.is_file():
        raise ValueError(f"Engineer did not write {pre_path}")
    proposal = load(pre_path)
    capability = load(meta["capability"])
    report = {
        "schema_version": checks.CHECKS_SCHEMA,
        "before_root": meta["baseline_snapshot"],
        "after_root": meta["candidate_root"],
        # Compare against the frozen snapshot: the live checkout may hold
        # environments or outputs that the clone deliberately skipped.
        "l0": checks.run_l0(
            meta["baseline_snapshot"], meta["candidate_root"], capability
        ),
        "l1": checks.run_l1(meta["candidate_root"], capability),
    }
    report["passed"] = (
        report["l0"]["status"] == "passed"
        and report["l1"]["status"] == "passed"
    )
    checks_path = directory / "checks.json"
    write(checks_path, report)
    if not report["passed"]:
        meta["status"] = "engineer_repair_required"
        write(directory / "round-meta.json", meta)
        raise ValueError("L0/L1 failed; Engineer may repair without consuming an attempt")
    card = get_adapter(config["harness"]["adapter"]).harness_card_path(
        meta["candidate_root"], config["harness"]["harness_card"]
    )
    card_sha = sha256_file(card)
    changed_paths = [row["path"] for row in report["l0"]["changed_files"]]
    try:
        card_rel = (
            Path(card)
            .resolve()
            .relative_to(Path(meta["candidate_root"]).resolve())
            .as_posix()
        )
    except ValueError:
        card_rel = None
    source_changed = [path for path in changed_paths if path != card_rel]
    if card_sha == meta.get("baseline_card_sha256") or not source_changed:
        meta["status"] = "engineer_repair_required"
        write(directory / "round-meta.json", meta)
        raise ValueError(
            "an update must change harness source and HARNESS_CARD.md together; "
            "repair without consuming an attempt"
        )

    proposal["schema_version"] = "gui-harvest-intervention-v1"
    proposal["stage"] = "post_edit"
    proposal["base_behavior_hash"] = meta["base_behavior_hash"]
    proposal["baseline_root"] = meta["baseline_root"]
    proposal["candidate_root"] = meta["candidate_root"]
    proposal["harness_adapter"] = config["harness"]["adapter"]
    proposal.setdefault("intervention", {})["patch_manifest"] = [
        {
            "action": row["action"],
            "path": row["path"],
            "sha256": row.get("after_sha256"),
            "before_sha256": row.get("before_sha256"),
            "changed_lines": row.get("changed_lines"),
        }
        for row in report["l0"]["changed_files"]
    ]
    proposal["checks"] = {
        "l0": report["l0"]["status"],
        "l1": report["l1"]["status"],
        "report_path": str(checks_path.resolve()),
        "report_sha256": sha256_file(checks_path),
    }
    proposal["harness_card"] = {
        "path": str(Path(card).resolve()),
        "sha256": card_sha,
    }
    queue = load(directory / "engineer_queue.json")
    problems = validate_proposal(
        proposal, queue, capability, load(ledger_path(config))
    )
    if problems:
        raise ValueError("Engineer proposal rejected:\n- " + "\n- ".join(problems))
    proposal_path = directory / "proposal.json"
    write(proposal_path, proposal)
    attach_proposal(str(directory / "round.json"), str(proposal_path))
    plan = make_rollout_plan(config, campaign, number, proposal)
    write(directory / "rollout-plan.json", plan)
    meta["status"] = "awaiting_rollouts"
    meta["proposal"] = str(proposal_path.resolve())
    meta["rollout_plan"] = str((directory / "rollout-plan.json").resolve())
    write(directory / "round-meta.json", meta)
    campaign["status"] = "awaiting_rollouts"
    save_campaign(config, campaign)
    return plan


def make_rollout_plan(config, campaign, number, proposal):
    search_ids, validation_ids = _split_ids(config)
    task_ids = sorted(set(search_ids) | set(validation_ids))
    directory = Path(attempt_dir(config, number))
    task_file = directory / "evaluation-task-ids.json"
    write(task_file, task_ids)
    command_template = config["rollout"].get("command") or []
    runs = []
    for repeat in range(1, config["protocol"]["rollouts_k"] + 1):
        tag = f"gui-harvest-a{number}-candidate-k{repeat}"
        replacements = {
            "{harness}": proposal["candidate_root"],
            "{split_file}": str(task_file.resolve()),
            "{repeat}": str(repeat),
            "{tag}": tag,
            "{results_root}": config["benchmark"]["results_root"],
        }
        command = [
            replacements.get(str(value), str(value))
            for value in command_template
        ]
        runs.append(
            {
                "name": f"r{repeat}",
                "tag": tag,
                "command": command,
                "result_dir": None,
            }
        )
    return {
        "schema_version": "gui-harvest-rollout-plan-v1",
        "attempt": number,
        "round": campaign.get("optimization_round"),
        "candidate_root": proposal["candidate_root"],
        "candidate_behavior_sha256": get_adapter(
            config["harness"]["adapter"]
        ).runtime_behavior_hash(proposal["candidate_root"]),
        "task_ids": task_ids,
        "task_file": str(task_file.resolve()),
        "splits": ["search", "validation"],
        "k": config["protocol"]["rollouts_k"],
        "runs": runs,
        "external_execution_required": not bool(command_template),
    }


def abort_round(config, campaign, number, reason):
    directory = Path(attempt_dir(config, number))
    meta = load(directory / "round-meta.json")
    if meta.get("status") == "closed":
        raise ValueError("a GUI-evaluated attempt cannot be aborted")
    stamp = now().replace(":", "").replace("+", "_")
    archive = Path(state_path(config, "aborted", f"attempt-{number:03d}-{stamp}"))
    archive.parent.mkdir(parents=True, exist_ok=True)
    if meta.get("baseline_snapshot"):
        get_adapter(campaign["current_harness"]["adapter"]).restore(
            meta["baseline_snapshot"], meta["baseline_root"]
        )
    candidate = Path(meta["candidate_root"])
    if candidate.exists():
        archived_candidate = archive.parent / (archive.name + "-candidate")
        shutil.move(str(candidate), archived_candidate)
    meta["status"] = "aborted_before_gui_evaluation"
    meta["abort_reason"] = reason
    write(directory / "round-meta.json", meta)
    shutil.move(str(directory), archive)
    campaign["current_attempt"] = (
        campaign["attempts_evaluated"] if campaign["attempts_evaluated"] else None
    )
    campaign["status"] = (
        "attempt_closed" if campaign["attempts_evaluated"] else "ready_for_round"
    )
    save_campaign(config, campaign)
    return str(archive)


def _score_sample(sample):
    """Validation packet: repeated-run scores, without trajectories."""
    return {
        "schema_version": "gui-harvest-aggregate-scores-v1",
        "status": sample.get("status") or "scored",
        "compatibility": sample.get("compatibility") or {},
        "tasks": [
            {
                "task_id": task.get("task_id"),
                "rewards": task.get("rewards") or {},
            }
            for task in sample.get("tasks") or []
        ],
    }


def _selected_harness(config, root):
    adapter = get_adapter(config["harness"]["adapter"])
    card = adapter.harness_card_path(root, config["harness"]["harness_card"])
    capability = load(config["harness"]["capability_manifest"])
    capability = dict(capability)
    capability["harness_root"] = str(Path(root).resolve())
    from gui_harvest.campaign.flow import harness_behavior_hash

    behavior_hash, _inputs = harness_behavior_hash(capability, card)
    return {
        "root": str(Path(root).resolve()),
        "adapter": config["harness"]["adapter"],
        "runtime_behavior_sha256": adapter.runtime_behavior_hash(root),
        "behavior_hash": behavior_hash,
        "harness_card": {
            "path": str(Path(card).resolve()),
            "sha256": sha256_file(card),
        },
    }


def validate_round(config, campaign, number, runs):
    directory = Path(attempt_dir(config, number))
    meta = load(directory / "round-meta.json")
    if meta.get("status") == "closed":
        decision = load(meta["decision"])
        release = load(meta["release"])
        return decision, release, load(directory / "next-action.json")
    plan = load(directory / "rollout-plan.json")
    audit = audit_runs(
        runs, plan["task_ids"], config["protocol"]["rollouts_k"]
    )
    audit_path = directory / "rollout-audit.json"
    write_audit(audit_path, audit)
    if not audit["complete"]:
        raise ValueError(
            f"candidate rollouts are incomplete; rerun plan written to {audit_path}"
        )
    proposal = load(directory / "proposal.json")
    after_full = build_sample(
        runs,
        harness_card=proposal["harness_card"]["path"],
        expected_k=config["protocol"]["rollouts_k"],
    )
    expected_sha = get_adapter(config["harness"]["adapter"]).runtime_behavior_hash(
        meta["candidate_root"]
    )
    observed_sha = after_full["compatibility"].get("behavior_sha256")
    if observed_sha and observed_sha != expected_sha:
        raise ValueError("candidate rollout fingerprint does not match candidate tree")
    after_full_path = directory / "candidate-sample.json"
    write(after_full_path, after_full)

    before_full = load(campaign["current_sample"]["path"])
    search_ids, validation_ids = _split_ids(config)
    samples = {
        "search-before.json": subset_sample(before_full, search_ids),
        "search-after.json": subset_sample(after_full, search_ids),
        "validation-before.json": _score_sample(
            subset_sample(before_full, validation_ids)
        ),
        "validation-after.json": _score_sample(
            subset_sample(after_full, validation_ids)
        ),
    }
    for name, payload in samples.items():
        write(directory / name, payload)

    validator = config["models"]["validator"]
    verdicts_path = directory / "verdicts.json"
    _run(
        [
            sys.executable,
            "-m",
            "gui_harvest.roles.validator_agent",
            "--proposal",
            directory / "proposal.json",
            "--post-sample",
            directory / "search-after.json",
            "--findings",
            directory / "findings.jsonl",
            "--out",
            verdicts_path,
            "--cache-dir",
            directory / "validator-cache",
            "--backend",
            validator["backend"],
            "--model",
            validator["model"],
            "--max-tokens",
            validator["max_tokens"],
            "--workers",
            validator["workers"],
            "--temperature",
            validator["temperature"],
        ],
        env=_role_env(config, "validator"),
    )
    decision_path = directory / "decision.json"
    _run(
        [
            sys.executable,
            "-m",
            "gui_harvest.roles.validator",
            "--proposal",
            directory / "proposal.json",
            "--verdicts",
            verdicts_path,
            "--search-before",
            directory / "search-before.json",
            "--search-after",
            directory / "search-after.json",
            "--validation-before",
            directory / "validation-before.json",
            "--validation-after",
            directory / "validation-after.json",
            "--post-sample",
            directory / "search-after.json",
            "--comparison-kind",
            "single_intervention",
            "--out",
            decision_path,
        ]
    )
    decision = load(decision_path)
    if decision["decision"] != "promote":
        get_adapter(config["harness"]["adapter"]).restore(
            meta["baseline_snapshot"], meta["baseline_root"]
        )
        campaign["current_harness"] = _selected_harness(
            config, meta["baseline_root"]
        )
    release_path = close_round(
        str(directory / "round.json"),
        str(decision_path),
        str(verdicts_path),
        ledger_path(config),
    )
    release = load(release_path)
    if decision["decision"] == "promote":
        campaign["current_harness"] = release["selected_harness"]
        campaign["current_sample"] = {
            "path": str(after_full_path.resolve()),
            "sha256": sha256_file(after_full_path),
            "tasks": len(after_full["tasks"]),
            "k": config["protocol"]["rollouts_k"],
        }
    campaign["attempts_evaluated"] = number
    campaign["accounting"]["gui_task_executions"] += (
        len(after_full["tasks"]) * config["protocol"]["rollouts_k"]
    )
    campaign["accounting"]["candidate_evaluations"] += 1
    if decision["decision"] == "promote":
        campaign["accounting"]["promotions"] += 1
    else:
        campaign["accounting"]["rollbacks"] += 1
    stop = stopping_status(
        load(ledger_path(config)),
        campaign["current_harness"]["behavior_hash"],
        config["protocol"]["round_budget"],
        config["protocol"]["rejection_patience"],
    )
    campaign["optimization_round"] = stop["current_round"]
    campaign["status"] = "attempt_closed"
    save_campaign(config, campaign)
    meta["status"] = "closed"
    meta["decision"] = str(decision_path.resolve())
    meta["release"] = str(Path(release_path).resolve())
    write(directory / "round-meta.json", meta)
    next_action = {
        "decision": decision["decision"],
        "official_protocol": decision.get("official_protocol"),
        "stop": stop,
        "next_command": (
            f"gui-harvest test open --config {config['_path']} --reason "
            f"{'round_budget' if 'round_cap' in stop['reasons'] else 'patience'}"
            if stop["stop"]
            else f"gui-harvest round next --config {config['_path']}"
        ),
    }
    write(directory / "next-action.json", next_action)
    return decision, release, next_action

