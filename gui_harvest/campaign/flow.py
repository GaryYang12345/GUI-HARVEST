"""Artifact-driven GUI-HARVEST round orchestration.

This is intentionally a thin deterministic state machine.  It does not hide
role calls in one opaque agent conversation: GAFT, Analyst, Clusterer,
Engineer, and Validator exchange versioned files whose hashes are recorded in
the round manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os

from gui_harvest.roles.engineer import (
    build_queue,
    validate_capability_manifest,
    validate_proposal,
)
from gui_harvest.campaign.adapters import get_adapter
from gui_harvest.campaign.provenance import sha256_file

ROUND_SCHEMA = "gui-harvest-round-v1"
STATES = {
    "discovery_complete",
    "proposal_frozen",
    "promoted",
    "rolled_back",
}


def _load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _write(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")
    os.replace(tmp, path)


def _findings(path):
    rows, findings = [], []
    with open(path, encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            rows.append(row)
            if row.get("analysis_status", "ok") == "ok":
                findings.extend(row.get("findings") or [])
    return rows, findings


def _artifact(path):
    return {"path": os.path.abspath(path), "sha256": sha256_file(path)}


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def harness_behavior_hash(capability, harness_card_path):
    """Hash every editable source file plus the card that describes runtime behavior."""
    root = os.path.abspath(capability["harness_root"])
    patterns = capability.get("editable_paths") or []
    files = {}
    for directory, _subdirs, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            if rel != "HARNESS_CARD.md" and any(
                fnmatch.fnmatch(rel, pattern) for pattern in patterns
            ):
                files[rel] = sha256_file(path)
    payload = {
        "files": dict(sorted(files.items())),
        "harness_card": sha256_file(harness_card_path),
    }
    encoded = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest(), payload


def runtime_behavior_hash(harness_root, adapter_name="agent_s3"):
    return get_adapter(adapter_name).runtime_behavior_hash(harness_root)


def sample_contract(sample):
    problems = []
    tasks = sample.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return ["sample.tasks is missing or empty"], {}
    run_counts = {}
    duplicate_ids = set()
    seen = set()
    for task in tasks:
        task_id = task.get("task_id")
        if not task_id:
            problems.append("a sample task has no task_id")
            continue
        if task_id in seen:
            duplicate_ids.add(task_id)
        seen.add(task_id)
        rewards = task.get("rewards") or {}
        run_counts[len(rewards)] = run_counts.get(len(rewards), 0) + 1
        dirs = task.get("task_dirs") or {}
        if set(rewards) != set(dirs):
            problems.append(f"{task_id}: rewards and task_dirs use different runs")
    if duplicate_ids:
        problems.append(f"duplicate task ids: {sorted(duplicate_ids)[:3]}")
    if len(run_counts) != 1:
        problems.append(f"sample has inconsistent K: {run_counts}")
    k = next(iter(run_counts), 0)
    if k < 2:
        problems.append(f"K={k}; cross-run analysis requires at least two runs")
    return problems, {"tasks": len(tasks), "K": k, "run_count_distribution": run_counts}


def discover(
    sample_path,
    findings_path,
    modes_path,
    capability_manifest_path,
    harness_card_path,
    out_dir,
    ledger_path=None,
    split_manifest_path=None,
):
    sample = _load(sample_path)
    modes = _load(modes_path)
    capability = _load(capability_manifest_path)
    rows, findings = _findings(findings_path)
    problems, sample_summary = sample_contract(sample)
    problems.extend(validate_capability_manifest(capability))
    if not os.path.isfile(harness_card_path):
        problems.append("Harness Card does not exist")
    sample_ids = {task.get("task_id") for task in sample.get("tasks") or []}
    split_manifest = _load(split_manifest_path) if split_manifest_path else None
    if split_manifest:
        search_ids = set(
            (split_manifest.get("search") or {}).get("task_ids")
            or split_manifest.get("search_task_ids")
            or []
        )
        if not sample_ids <= search_ids:
            problems.append("GAFT sample contains tasks outside frozen Search")
        test_record = split_manifest.get("test") or split_manifest.get("sealed_test") or {}
        if test_record.get("opened"):
            problems.append("sealed Test is already marked opened during discovery")
    finding_ids = {row.get("task_id") for row in rows}
    if not finding_ids <= sample_ids:
        problems.append("findings contain tasks outside the GAFT sample")
    mode_member_ids = {
        fid
        for mode in modes.get("modes") or []
        for fid in mode.get("members") or []
    }
    known_findings = {finding.get("finding_id") for finding in findings}
    if not mode_member_ids <= known_findings:
        problems.append("modes contain unknown finding ids")
    if problems:
        raise ValueError("discovery contract failed:\n- " + "\n- ".join(problems))

    queue = build_queue(modes, findings)
    behavior_hash, behavior_inputs = harness_behavior_hash(
        capability, harness_card_path
    )
    os.makedirs(out_dir, exist_ok=True)
    queue_path = os.path.join(out_dir, "engineer_queue.json")
    _write(queue_path, queue)
    created = _now()
    manifest = {
        "schema_version": ROUND_SCHEMA,
        "round_id": os.path.basename(os.path.abspath(out_dir)),
        "state": "discovery_complete",
        "created_at": created,
        "updated_at": created,
        "evidence_policy": {
            "rollout": "reuse_existing",
            "evaluator_used_by_runtime_agent": False,
            "sealed_test_used_for_decisions": False,
            "split_manifest": (
                _artifact(split_manifest_path) if split_manifest_path else None
            ),
        },
        "stages": {
            "harness": {
                "capability_manifest": _artifact(capability_manifest_path),
                "harness_card": _artifact(harness_card_path),
                "behavior_hash": behavior_hash,
                "behavior_hash_inputs": behavior_inputs,
            },
            "gaft": {
                "artifact": _artifact(sample_path),
                "summary": sample_summary,
            },
            "analyst": {
                "artifact": _artifact(findings_path),
                "rows": len(rows),
                "ok_rows": sum(
                    row.get("analysis_status", "ok") == "ok" for row in rows
                ),
                "findings": len(findings),
            },
            "clusterer": {
                "artifact": _artifact(modes_path),
                "modes": modes.get("n_modes"),
                "residue_findings": modes.get("n_residue_findings"),
            },
            "engineer": {
                "queue": _artifact(queue_path),
                "prior_ledger": (
                    _artifact(ledger_path)
                    if ledger_path and os.path.isfile(ledger_path)
                    else None
                ),
                "recurrent": queue["n_recurrent"],
                "opportunistic": queue["n_opportunistic"],
            },
            "validator": None,
        },
        "events": [
            {
                "at": created,
                "event": "discovery_complete",
                "detail": "existing K-run evidence compiled through Engineer queue",
            }
        ],
    }
    manifest_path = os.path.join(out_dir, "round.json")
    _write(manifest_path, manifest)
    return manifest_path, queue_path, manifest


def attach_proposal(round_path, proposal_path):
    round_artifact = _load(round_path)
    if round_artifact.get("state") != "discovery_complete":
        raise ValueError("a proposal can only be frozen after discovery_complete")
    queue_path = round_artifact["stages"]["engineer"]["queue"]["path"]
    queue = _load(queue_path)
    proposal = _load(proposal_path)
    capability_path = round_artifact["stages"]["harness"][
        "capability_manifest"
    ]["path"]
    ledger_artifact = round_artifact["stages"]["engineer"].get("prior_ledger")
    ledger = _load(ledger_artifact["path"]) if ledger_artifact else None
    problems = validate_proposal(
        proposal, queue, _load(capability_path), ledger
    )
    if proposal.get("base_behavior_hash") != round_artifact["stages"]["harness"][
        "behavior_hash"
    ]:
        problems.append("proposal was generated for a different harness behavior hash")
    checks = proposal.get("checks") or {}
    checks_path = checks.get("report_path")
    if checks_path and sha256_file(checks_path) != checks.get("report_sha256"):
        problems.append("checks report is missing or its SHA-256 does not match")
    elif checks_path:
        checks_report = _load(checks_path)
        if checks_report.get("l0", {}).get("status") != checks.get("l0"):
            problems.append("checks.l0 differs from the checks report")
        if checks_report.get("l1", {}).get("status") != checks.get("l1"):
            problems.append("checks.l1 differs from the checks report")
    if problems:
        raise ValueError("Engineer proposal rejected:\n- " + "\n- ".join(problems))
    round_artifact["stages"]["engineer"]["proposal"] = _artifact(proposal_path)
    round_artifact["state"] = "proposal_frozen"
    round_artifact["updated_at"] = _now()
    round_artifact["events"].append(
        {
            "at": round_artifact["updated_at"],
            "event": "proposal_frozen",
            "proposal_id": proposal.get("proposal_id"),
        }
    )
    _write(round_path, round_artifact)


def attach_decision(round_path, decision_path):
    round_artifact = _load(round_path)
    if round_artifact.get("state") != "proposal_frozen":
        raise ValueError("a decision requires a frozen proposal")
    decision = _load(decision_path)
    proposal = _load(round_artifact["stages"]["engineer"]["proposal"]["path"])
    if decision.get("proposal_id") != proposal.get("proposal_id"):
        raise ValueError("decision proposal_id differs from the frozen proposal")
    state = {
        "promote": "promoted",
        "rollback": "rolled_back",
    }.get(decision.get("decision"))
    if state not in STATES:
        raise ValueError(f"unknown decision {decision.get('decision')!r}")
    round_artifact["stages"]["validator"] = {
        "decision": _artifact(decision_path),
        "comparison_kind": decision.get("comparison_kind"),
        "gates": decision.get("gates"),
    }
    round_artifact["state"] = state
    round_artifact["updated_at"] = _now()
    round_artifact["events"].append(
        {
            "at": round_artifact["updated_at"],
            "event": state,
            "reason": decision.get("reason"),
        }
    )
    _write(round_path, round_artifact)


def close_round(round_path, decision_path, verdicts_path, ledger_path):
    """Atomically meaningful round closure: ledger first, then round state.

    A promotion selects the evaluated candidate tree (including its Harness
    Card) as the next round's current harness.  A rollback keeps the baseline
    selected and records the rejected candidate plus full Validator feedback.
    """
    from gui_harvest.roles.validator import append_ledger

    round_artifact = _load(round_path)
    release_path = os.path.join(
        os.path.dirname(os.path.abspath(round_path)), "release.json"
    )
    if round_artifact.get("state") in {
        "promoted",
        "rolled_back",
    }:
        if os.path.isfile(release_path):
            return release_path
        raise ValueError("round is terminal but release.json is missing")
    if round_artifact.get("state") != "proposal_frozen":
        raise ValueError("close-round requires a proposal_frozen round")
    proposal_path = round_artifact["stages"]["engineer"]["proposal"]["path"]
    proposal = _load(proposal_path)
    decision = _load(decision_path)
    verdicts = _load(verdicts_path)
    if decision.get("proposal_id") != proposal.get("proposal_id"):
        raise ValueError("decision proposal_id differs from the frozen proposal")

    outcome = decision.get("decision")
    baseline_root = os.path.abspath(proposal.get("baseline_root") or "")
    candidate_root = os.path.abspath(proposal.get("candidate_root") or "")
    selected_root = baseline_root
    selected_hash = proposal.get("base_behavior_hash")
    candidate_card = os.path.abspath(
        (proposal.get("harness_card") or {}).get("path") or ""
    )
    try:
        card_relative = os.path.relpath(candidate_card, candidate_root)
    except ValueError as error:
        raise ValueError("candidate Harness Card path is invalid") from error
    if card_relative == os.pardir or card_relative.startswith(os.pardir + os.sep):
        raise ValueError("candidate Harness Card must be inside the candidate tree")
    card_path = os.path.join(baseline_root, card_relative)
    promoted_outcomes = {"promote"}
    if outcome in promoted_outcomes:
        if not os.path.isdir(candidate_root):
            raise ValueError("promoted proposal has no existing candidate_root")
        card_path = candidate_card
        if not os.path.isfile(card_path):
            raise ValueError("promoted candidate has no configured Harness Card")
        card_meta = proposal.get("harness_card") or {}
        if card_meta.get("sha256") != sha256_file(card_path):
            raise ValueError(
                "promotion requires proposal.harness_card.sha256 to match the "
                "evaluated candidate card"
            )
        evaluated_sha = (
            (((decision.get("gates") or {}).get("utility") or {}).get("profiles") or {})
            .get("search_after", {})
            .get("compatibility", {})
            .get("behavior_sha256")
        )
        if not evaluated_sha:
            raise ValueError(
                "promotion requires Search-after run-manifest behavior_sha256"
            )
        current_runtime_sha = runtime_behavior_hash(
            candidate_root, proposal.get("harness_adapter", "agent_s3")
        )
        if current_runtime_sha != evaluated_sha:
            raise ValueError(
                "candidate source fingerprint differs from evaluated Search-after tree"
            )
        capability_path = round_artifact["stages"]["harness"][
            "capability_manifest"
        ]["path"]
        capability = _load(capability_path)
        capability["harness_root"] = candidate_root
        selected_hash, _inputs = harness_behavior_hash(capability, card_path)
        selected_root = candidate_root
    elif outcome != "rollback":
        raise ValueError(f"unknown decision {outcome!r}")

    artifacts = {
        "proposal": _artifact(proposal_path),
        "verdicts": _artifact(verdicts_path),
        "decision": _artifact(decision_path),
    }
    existing_ledger = _load(ledger_path) if os.path.isfile(ledger_path) else {}
    existing = next(
        (
            row
            for row in existing_ledger.get("attempts") or []
            if row.get("proposal_id") == proposal.get("proposal_id")
        ),
        None,
    )
    if existing is None:
        append_ledger(
            ledger_path,
            decision,
            proposal,
            verdicts,
            artifacts=artifacts,
        )
    elif (existing.get("decision") or {}).get("decision") != outcome:
        raise ValueError("ledger already contains a different outcome for proposal")
    ledger = _load(ledger_path)
    ledger["current_harness"] = {
        "root": selected_root,
        "adapter": proposal.get("harness_adapter", "agent_s3"),
        "behavior_hash": selected_hash,
        "runtime_behavior_sha256": (
            runtime_behavior_hash(
                selected_root, proposal.get("harness_adapter", "agent_s3")
            )
            if os.path.isdir(selected_root)
            else None
        ),
        "harness_card": _artifact(card_path) if os.path.isfile(card_path) else None,
        "selected_by": (
            proposal.get("proposal_id") if outcome in promoted_outcomes else None
        ),
        "official_protocol": True,
    }
    ledger["last_outcome"] = {
        "proposal_id": proposal.get("proposal_id"),
        "decision": outcome,
        "rejected_candidate_root": candidate_root if outcome == "rollback" else None,
        "evaluated_candidate_behavior_sha256": (
            (((decision.get("gates") or {}).get("utility") or {}).get("profiles") or {})
            .get("search_after", {})
            .get("compatibility", {})
            .get("behavior_sha256")
        ),
        "artifacts": artifacts,
    }
    _write(ledger_path, ledger)

    _write(
        release_path,
        {
            "schema_version": "gui-harvest-release-selection-v1",
            "proposal_id": proposal.get("proposal_id"),
            "decision": outcome,
            "publication_eligible": outcome == "promote",
            "selected_harness": ledger["current_harness"],
            "rejected_candidate_root": (
                candidate_root if outcome == "rollback" else None
            ),
            "evaluated_candidate_behavior_sha256": ledger["last_outcome"][
                "evaluated_candidate_behavior_sha256"
            ],
            "decision_artifact": artifacts["decision"],
        },
    )
    attach_decision(round_path, decision_path)
    return release_path


def revalidate_closed_round(round_path, decision_path, verdicts_path, ledger_path):
    """Attach a corrected Validator pass without erasing the original closure."""
    round_artifact = _load(round_path)
    if round_artifact.get("state") not in {
        "promoted",
        "rolled_back",
    }:
        raise ValueError("revalidation requires a causally closed round")
    proposal = _load(round_artifact["stages"]["engineer"]["proposal"]["path"])
    decision = _load(decision_path)
    verdicts = _load(verdicts_path)
    if decision.get("proposal_id") != proposal.get("proposal_id"):
        raise ValueError("revalidation proposal_id differs from frozen proposal")
    original_decision = _load(
        round_artifact["stages"]["validator"]["decision"]["path"]
    )
    if decision.get("decision") != original_decision.get("decision"):
        raise ValueError(
            "revalidation changes the terminal outcome; start a formal audit "
            "instead of rewriting a closed round"
        )
    artifacts = {
        "decision": _artifact(decision_path),
        "verdicts": _artifact(verdicts_path),
    }
    ledger = _load(ledger_path)
    attempt = next(
        (
            row
            for row in ledger.get("attempts") or []
            if row.get("proposal_id") == proposal.get("proposal_id")
        ),
        None,
    )
    if attempt is None:
        raise ValueError("closed proposal is missing from ledger")
    revalidation = {
        "at": _now(),
        "artifacts": artifacts,
        "decision": decision,
        "validator_verdicts": verdicts.get("verdicts") or [],
        "feedback": decision.get("feedback"),
    }
    attempt.setdefault("revalidations", []).append(revalidation)
    attempt["effective_decision"] = decision
    attempt["effective_validator_verdicts"] = revalidation["validator_verdicts"]
    attempt["effective_feedback"] = decision.get("feedback")
    ledger.setdefault("last_outcome", {})["latest_revalidation"] = artifacts
    _write(ledger_path, ledger)

    round_artifact["stages"]["validator"].setdefault("revalidations", []).append(
        {
            "at": revalidation["at"],
            "decision": artifacts["decision"],
            "verdicts": artifacts["verdicts"],
        }
    )
    round_artifact["updated_at"] = revalidation["at"]
    round_artifact["events"].append(
        {
            "at": revalidation["at"],
            "event": "validator_revalidated",
            "reason": decision.get("reason"),
        }
    )
    _write(round_path, round_artifact)
    release_path = os.path.join(
        os.path.dirname(os.path.abspath(round_path)), "release.json"
    )
    release = _load(release_path)
    release["latest_revalidation"] = artifacts
    _write(release_path, release)
    return release_path


def stopping_status(ledger, current_behavior_hash, round_cap=10, patience=5):
    """Round and attempt budget over GUI-evaluated single-intervention attempts.

    A round is the search at one fixed harness state. Every GUI-evaluated
    candidate is one attempt in the current round; a rollback stays in the
    round, and only a promotion starts the next one. Optimization stops after
    ``round_cap`` rounds or when ``patience`` attempts in one round are all
    rejected.
    """
    attempts = [
        row
        for row in ledger.get("attempts") or []
        if (row.get("decision") or {}).get("causal_intervention_test")
    ]
    promotions = sum(
        1
        for row in attempts
        if (row.get("decision") or {}).get("decision") == "promote"
    )
    trailing = 0
    for row in reversed(attempts):
        decision = row.get("decision") or {}
        if decision.get("decision") == "promote":
            break
        if row.get("base_behavior_hash") != current_behavior_hash:
            break
        if decision.get("decision") == "rollback":
            trailing += 1
            continue
        break
    reasons = []
    if promotions >= round_cap:
        reasons.append("round_cap")
    if trailing >= patience:
        reasons.append("patience")
    return {
        "stop": bool(reasons),
        "reasons": reasons,
        "current_round": min(promotions + 1, round_cap),
        "completed_rounds": promotions + (1 if trailing >= patience else 0),
        "round_cap": round_cap,
        "attempts_in_current_round": trailing,
        "patience": patience,
        "gui_evaluated_attempts": len(attempts),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover")
    discovery.add_argument("--sample", required=True)
    discovery.add_argument("--findings", required=True)
    discovery.add_argument("--modes", required=True)
    discovery.add_argument("--capability-manifest", required=True)
    discovery.add_argument("--harness-card", required=True)
    discovery.add_argument("--out-dir", required=True)
    discovery.add_argument("--ledger", help="prior attempt ledger for Engineer feedback")
    discovery.add_argument(
        "--split-manifest",
        help="frozen public S/V/T manifest; sealed Test ids stay in a separate file",
    )
    proposal = sub.add_parser("attach-proposal")
    proposal.add_argument("--round", required=True)
    proposal.add_argument("--proposal", required=True)
    decision = sub.add_parser("attach-decision")
    decision.add_argument("--round", required=True)
    decision.add_argument("--decision", required=True)
    close = sub.add_parser("close-round")
    close.add_argument("--round", required=True)
    close.add_argument("--decision", required=True)
    close.add_argument("--verdicts", required=True)
    close.add_argument("--ledger", required=True)
    revalidate = sub.add_parser("revalidate-round")
    revalidate.add_argument("--round", required=True)
    revalidate.add_argument("--decision", required=True)
    revalidate.add_argument("--verdicts", required=True)
    revalidate.add_argument("--ledger", required=True)
    status = sub.add_parser("status")
    status.add_argument("--round", required=True)
    status.add_argument("--ledger", required=True)
    status.add_argument("--round-cap", type=int, default=10)
    status.add_argument("--patience", type=int, default=5)
    args = parser.parse_args()

    if args.command == "discover":
        manifest, queue, payload = discover(
            args.sample,
            args.findings,
            args.modes,
            args.capability_manifest,
            args.harness_card,
            args.out_dir,
            args.ledger,
            args.split_manifest,
        )
        print(
            f"discovery_complete: K={payload['stages']['gaft']['summary']['K']}, "
            f"{payload['stages']['analyst']['findings']} findings, "
            f"{payload['stages']['clusterer']['modes']} modes"
        )
        print(f"wrote {manifest} and {queue}")
    elif args.command == "attach-proposal":
        attach_proposal(args.round, args.proposal)
        print(f"proposal_frozen in {args.round}")
    elif args.command == "attach-decision":
        attach_decision(args.round, args.decision)
        print(f"decision attached to {args.round}")
    elif args.command == "close-round":
        release = close_round(
            args.round, args.decision, args.verdicts, args.ledger
        )
        print(f"round closed; release selection written to {release}")
    elif args.command == "revalidate-round":
        release = revalidate_closed_round(
            args.round, args.decision, args.verdicts, args.ledger
        )
        print(f"closed round revalidated; release record updated at {release}")
    else:
        round_artifact = _load(args.round)
        state = stopping_status(
            _load(args.ledger),
            round_artifact["stages"]["harness"]["behavior_hash"],
            args.round_cap,
            args.patience,
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
