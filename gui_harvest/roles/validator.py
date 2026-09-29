"""Prediction validation, utility gates, and promotion decisions for GUI-HARVEST.

The LLM Validator emits task-local verdicts about pre-registered predictions.
This module performs every aggregation and promotion decision mechanically.
Each decision compares one intervention against the harness it was made on.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter, defaultdict

from gui_harvest.roles.engineer import PROPOSAL_SCHEMA
from gui_harvest.campaign.provenance import create_stage_manifest, write_manifest

VERDICTS_SCHEMA = "gui-harvest-validator-verdicts-v1"
DECISION_SCHEMA = "gui-harvest-promotion-decision-v1"
VERDICTS = {"supported", "contradicted", "inconclusive"}
COMPARISON_KINDS = {"single_intervention"}


def _load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def score_profile(sample):
    """Repeated-run utility and reliability from a GAFT sample artifact."""
    task_rows = sample.get("tasks") or []
    means = []
    all_pass = 0
    mixed = 0
    run_counts = Counter()
    for task in task_rows:
        values = [
            float(value)
            for value in (task.get("rewards") or {}).values()
            if value is not None and math.isfinite(float(value))
        ]
        if not values:
            continue
        run_counts[len(values)] += 1
        means.append(sum(values) / len(values))
        passed = [value > 0 for value in values]
        all_pass += int(all(passed))
        mixed += int(any(passed) and not all(passed))
    n = len(means)
    return {
        "status": sample.get("status") or ("scored" if n else "not_run"),
        "tasks": n,
        "mean_utility": (sum(means) / n) if n else None,
        "pass1": (sum(means) / n) if n else None,
        "pass_k": (all_pass / n) if n else None,
        "outcome_disagreement": (mixed / n) if n else None,
        "mixed_tasks": mixed,
        "run_count_distribution": dict(sorted(run_counts.items())),
        "task_ids": sorted(
            task.get("task_id") for task in task_rows if task.get("task_id")
        ),
        "compatibility": sample.get("compatibility") or {},
        "run_manifests": sample.get("run_manifests") or {},
    }


def _task_means(sample):
    means = {}
    for task in sample.get("tasks") or []:
        values = [
            float(value)
            for value in (task.get("rewards") or {}).values()
            if value is not None and math.isfinite(float(value))
        ]
        if task.get("task_id") and values:
            means[task["task_id"]] = sum(values) / len(values)
    return means


def utility_gate(
    before_s,
    after_s,
    before_v,
    after_v,
    tolerance=1e-12,
):
    profiles = {
        "search_before": score_profile(before_s),
        "search_after": score_profile(after_s),
        "validation_before": score_profile(before_v),
        "validation_after": score_profile(after_v),
    }
    sb, sa = profiles["search_before"], profiles["search_after"]
    vb, va = profiles["validation_before"], profiles["validation_after"]
    coverage_problems = []
    for name, before, after, before_sample, after_sample in (
        ("search", sb, sa, before_s, after_s),
        ("validation", vb, va, before_v, after_v),
    ):
        if before["task_ids"] != after["task_ids"]:
            coverage_problems.append(f"{name} task universe differs before/after")
        if before["tasks"] == 0 or after["tasks"] == 0:
            coverage_problems.append(f"{name} has no scored tasks")
        before_compat = dict(before_sample.get("compatibility") or {})
        after_compat = dict(after_sample.get("compatibility") or {})
        before_compat.pop("behavior_sha256", None)
        after_compat.pop("behavior_sha256", None)
        if before_compat and after_compat and before_compat != after_compat:
            coverage_problems.append(
                f"{name} model/suite/runtime invariants differ before/after"
            )
    ds = (
        sa["mean_utility"] - sb["mean_utility"]
        if sb["tasks"] and sa["tasks"] and sb["task_ids"] == sa["task_ids"]
        else None
    )
    dv = (
        va["mean_utility"] - vb["mean_utility"]
        if vb["tasks"] and va["tasks"] and vb["task_ids"] == va["task_ids"]
        else None
    )
    task_changes = {}
    for name, before, after in (
        ("search", before_s, after_s),
        ("validation", before_v, after_v),
    ):
        before_means = _task_means(before)
        after_means = _task_means(after)
        common = set(before_means) & set(after_means)
        task_changes[name] = sorted(
            [
                {
                    "task_id": task_id,
                    "before_mean": before_means[task_id],
                    "after_mean": after_means[task_id],
                    "delta": after_means[task_id] - before_means[task_id],
                }
                for task_id in common
            ],
            key=lambda row: (row["delta"], row["task_id"]),
        )
    passed = (
        not coverage_problems
        and ds is not None
        and dv is not None
        and ds >= -tolerance
        and dv >= -tolerance
        and max(ds, dv) > tolerance
    )
    rule = "no drop on S or V, and strict improvement on at least one"
    # Validation is an aggregate-only development guard. Task identities,
    # per-task changes, and trajectory roots never enter decision/ledger
    # artifacts that the Engineer can read.
    task_changes["validation"] = []
    for name in ("validation_before", "validation_after"):
        profiles[name] = dict(profiles[name])
        profiles[name].pop("task_ids", None)
        profiles[name].pop("run_manifests", None)
    return {
        "passed": passed,
        "coverage_problems": coverage_problems,
        "profiles": profiles,
        "delta_search": ds,
        "delta_validation": dv,
        "task_changes": task_changes,
        "rule": rule,
        "official_protocol": True,
    }


def _evidence_steps(post_sample):
    labels = post_sample.get("run_labels") or {}
    result = {}
    for task in post_sample.get("tasks") or []:
        task_id = task.get("task_id")
        for run_name, task_dir in (task.get("task_dirs") or {}).items():
            label = labels.get(run_name)
            if not task_id or not label:
                continue
            path = os.path.join(task_dir, "traj.jsonl")
            steps = set()
            try:
                with open(path, encoding="utf-8") as source:
                    for line in source:
                        if line.strip():
                            step = json.loads(line).get("step_num")
                            if isinstance(step, int):
                                steps.add(step)
            except (OSError, json.JSONDecodeError):
                steps = set()
            result[(task_id, label)] = steps
    return result


def verify_verdict_rows(verdict_payload, proposal, post_sample=None):
    problems = []
    if verdict_payload.get("schema_version") != VERDICTS_SCHEMA:
        problems.append(f"schema_version must be {VERDICTS_SCHEMA}")
    if proposal.get("schema_version") != PROPOSAL_SCHEMA:
        problems.append(f"proposal schema must be {PROPOSAL_SCHEMA}")
    predictions = {
        row["prediction_id"]: row for row in proposal.get("predictions") or []
    }
    seen = set()
    for index, row in enumerate(verdict_payload.get("verdicts") or []):
        pid = row.get("prediction_id")
        task_id = row.get("task_id")
        key = (pid, task_id)
        if pid not in predictions:
            problems.append(f"verdicts[{index}] names an unknown prediction")
            continue
        if task_id not in predictions[pid].get("target_task_ids", []):
            problems.append(f"verdicts[{index}] task is outside prediction target set")
        if key in seen:
            problems.append(f"verdicts[{index}] duplicates prediction/task")
        seen.add(key)
        if row.get("verdict") not in VERDICTS:
            problems.append(f"verdicts[{index}] has invalid verdict")
        citations = row.get("citations")
        if not isinstance(citations, list) or not citations:
            problems.append(f"verdicts[{index}] has no run/step citations")
        else:
            for citation in citations:
                if not citation.get("run") or not isinstance(citation.get("step"), int):
                    problems.append(
                        f"verdicts[{index}] has a citation without run and integer step"
                    )
    expected = {
        (prediction["prediction_id"], task_id)
        for prediction in proposal.get("predictions") or []
        if prediction.get("required_for_promotion", True)
        for task_id in prediction.get("target_task_ids") or []
    }
    missing = expected - seen
    if missing:
        problems.append(
            "missing required prediction/task verdicts: "
            + ", ".join(f"{pid}/{task_id}" for pid, task_id in sorted(missing)[:5])
        )
    jobs_expected = verdict_payload.get("jobs_expected")
    jobs_completed = verdict_payload.get("jobs_completed")
    if jobs_expected is not None and int(jobs_expected) != len(expected):
        problems.append(
            f"jobs_expected={jobs_expected} but proposal requires {len(expected)} verdicts"
        )
    if jobs_completed is not None and int(jobs_completed) != len(seen):
        problems.append(
            f"jobs_completed={jobs_completed} but payload contains {len(seen)} unique verdicts"
        )
    if post_sample is not None:
        evidence = _evidence_steps(post_sample)
        for index, row in enumerate(verdict_payload.get("verdicts") or []):
            task_id = row.get("task_id")
            for citation in row.get("citations") or []:
                key = (task_id, citation.get("run"))
                if key not in evidence:
                    problems.append(
                        f"verdicts[{index}] cites run {citation.get('run')!r} "
                        f"not present for task {task_id}"
                    )
                elif citation.get("step") not in evidence[key]:
                    problems.append(
                        f"verdicts[{index}] cites missing "
                        f"{citation.get('run')} step {citation.get('step')}"
                    )
    return problems


def soft_gate(verdict_payload, proposal):
    by_prediction = defaultdict(list)
    for row in verdict_payload.get("verdicts") or []:
        by_prediction[row["prediction_id"]].append(row)
    rows = []
    targets = proposal.get("targets")
    if targets is None:
        target = proposal.get("target")
        targets = [target] if target else []
    lanes = {
        target.get("candidate_id"): target.get("lane")
        for target in targets
        if isinstance(target, dict)
    }
    for prediction in proposal.get("predictions") or []:
        pid = prediction["prediction_id"]
        counts = Counter(row["verdict"] for row in by_prediction.get(pid, []))
        decidable = counts["supported"] + counts["contradicted"]
        candidate_ids = prediction.get("target_candidate_ids") or list(lanes)
        prediction_lanes = {lanes.get(candidate_id) for candidate_id in candidate_ids}
        minimum_decidable = 1
        required_decidable = min(
            minimum_decidable, len(prediction.get("target_task_ids") or [])
        )
        passed = (
            decidable >= required_decidable
            and counts["supported"] > counts["contradicted"]
        )
        rows.append(
            {
                "prediction_id": pid,
                "required_for_promotion": prediction.get(
                    "required_for_promotion", True
                ),
                "supported": counts["supported"],
                "contradicted": counts["contradicted"],
                "inconclusive": counts["inconclusive"],
                "decidable": decidable,
                "required_decidable": required_decidable,
                "support_rate": (
                    counts["supported"] / decidable if decidable else None
                ),
                "passed": passed,
            }
        )
    required = [row for row in rows if row["required_for_promotion"]]
    return {
        "passed": bool(required) and all(row["passed"] for row in required),
        "predictions": rows,
        "rule": (
            "each required prediction has at least one decidable target task "
            "and strictly more support than contradiction"
        ),
    }


def decide(
    proposal,
    verdicts,
    before_s,
    after_s,
    before_v,
    after_v,
    comparison_kind="single_intervention",
    post_sample=None,
):
    if comparison_kind not in COMPARISON_KINDS:
        raise ValueError(f"comparison_kind must be one of {sorted(COMPARISON_KINDS)}")
    verdict_problems = verify_verdict_rows(verdicts, proposal, post_sample)
    checks = proposal.get("checks") or {}
    local_gate = {
        "passed": checks.get("l0") == "passed" and checks.get("l1") == "passed",
        "l0": checks.get("l0"),
        "l1": checks.get("l1"),
    }
    # Utility and behavior are deliberately evaluated independently.  A failed
    # hard gate must not suppress task-level behavioral feedback.
    utility = utility_gate(
        before_s,
        after_s,
        before_v,
        after_v,
    )
    behavior = soft_gate(verdicts, proposal) if not verdict_problems else {
        "passed": False,
        "problems": verdict_problems,
        "predictions": [],
    }
    causal = comparison_kind == "single_intervention"
    promote = causal and local_gate["passed"] and utility["passed"] and behavior["passed"]
    if promote:
        status = "promote"
        reason = "L0/L1, S/V utility, and predicted-behavior gates all passed"
    else:
        status = "rollback"
        failed = [
            name
            for name, gate in (
                ("local", local_gate),
                ("utility", utility),
                ("behavior", behavior),
            )
            if not gate.get("passed")
        ]
        reason = "failed gates: " + ", ".join(failed)
    failed_gates = [
        name
        for name, gate in (
            ("local", local_gate),
            ("utility", utility),
            ("behavior", behavior),
        )
        if not gate.get("passed")
    ]
    behavior_rows = behavior.get("predictions", [])
    preserve_predictions = [
        row["prediction_id"]
        for row in behavior_rows
        if row.get("required_for_promotion") and row.get("passed")
    ]
    repair_predictions = [
        row["prediction_id"]
        for row in behavior_rows
        if row.get("required_for_promotion") and not row.get("passed")
    ]
    partial_support = [
        {
            "prediction_id": row["prediction_id"],
            "supported": row.get("supported", 0),
            "contradicted": row.get("contradicted", 0),
        }
        for row in behavior_rows
        if row.get("required_for_promotion")
        and not row.get("passed")
        and row.get("supported", 0) > 0
    ]
    search_regressions = [
        row["task_id"]
        for row in (utility.get("task_changes") or {}).get("search", [])
        if row["delta"] < 0
    ][:10]
    feedback = {
        "failed_gates": failed_gates,
        "utility": {
            "delta_search": utility.get("delta_search"),
            "delta_validation": utility.get("delta_validation"),
            "coverage_problems": utility.get("coverage_problems", []),
            "largest_regressions": {
                "search": (utility.get("task_changes") or {}).get("search", [])[:10]
            },
            "largest_improvements": {
                "search": list(
                    reversed(
                        (utility.get("task_changes") or {}).get("search", [])[-10:]
                    )
                )
            },
        },
        "behavior": {
            "verdict_problems": behavior.get("problems", []),
            "evidence_contract": (
                "run/step citation existence is checked mechanically when a "
                "post sample is supplied; whether each natural-language claim "
                "supports the prediction remains the AI Validator's soft judgment"
            ),
            "predictions": behavior.get("predictions", []),
            "partial_support_to_preserve": partial_support,
            "contradictions": [
                {
                    "prediction_id": row.get("prediction_id"),
                    "task_id": row.get("task_id"),
                    "summary": row.get("summary"),
                    "citations": row.get("citations") or [],
                }
                for row in verdicts.get("verdicts") or []
                if row.get("verdict") == "contradicted"
            ],
        },
        "engineer_next_step": (
            "Preserve passed predictions "
            f"{preserve_predictions} and task-local partial support "
            f"{partial_support}; repair failed predictions "
            f"{repair_predictions}; investigate Search regressions "
            f"{search_regressions}; explicitly address this proposal in "
            "prior_attempts_addressed before retrying."
            if failed_gates
            else "intervention passed all gates"
        ),
    }
    return {
        "schema_version": DECISION_SCHEMA,
        "proposal_id": proposal.get("proposal_id"),
        "comparison_kind": comparison_kind,
        "causal_intervention_test": causal,
        "official_protocol": True,
        "decision": status,
        "reason": reason,
        "gates": {
            "local": local_gate,
            "utility": utility,
            "behavior": behavior,
        },
        "feedback": feedback,
    }


def append_ledger(path, decision, proposal, verdicts=None, artifacts=None):
    if os.path.exists(path):
        ledger = _load(path)
    else:
        ledger = {"schema_version": "gui-harvest-update-ledger-v1", "attempts": []}
    if any(
        row.get("proposal_id") == decision.get("proposal_id")
        for row in ledger["attempts"]
    ):
        raise ValueError(f"proposal {decision.get('proposal_id')} already recorded")
    ledger["attempts"].append(
        {
            "proposal_id": decision.get("proposal_id"),
            "base_behavior_hash": proposal.get("base_behavior_hash"),
            "targets": proposal.get("targets")
            or ([proposal.get("target")] if proposal.get("target") else []),
            "attribution": proposal.get("attribution"),
            "intervention": proposal.get("intervention"),
            "candidate_root": proposal.get("candidate_root"),
            "checks": proposal.get("checks"),
            "predictions": proposal.get("predictions"),
            "validator_verdicts": (verdicts or {}).get("verdicts", []),
            "artifacts": artifacts or {},
            "feedback": decision.get("feedback"),
            "decision": decision,
        }
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as target:
        json.dump(ledger, target, ensure_ascii=False, indent=2)
        target.write("\n")
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--verdicts", required=True)
    parser.add_argument("--search-before", required=True)
    parser.add_argument("--search-after", required=True)
    parser.add_argument("--validation-before", required=True)
    parser.add_argument("--validation-after", required=True)
    parser.add_argument(
        "--post-sample",
        help="post-edit trajectory sample used to verify Validator run/step citations",
    )
    parser.add_argument(
        "--comparison-kind",
        choices=sorted(COMPARISON_KINDS),
        default="single_intervention",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--ledger")
    args = parser.parse_args()

    proposal = _load(args.proposal)
    verdicts = _load(args.verdicts)
    inputs = [
        args.proposal,
        args.verdicts,
        args.search_before,
        args.search_after,
        args.validation_before,
        args.validation_after,
        *([args.post_sample] if args.post_sample else []),
    ]
    post_sample = _load(args.post_sample) if args.post_sample else None
    decision = decide(
        proposal,
        verdicts,
        *[
            _load(path)
            for path in (
                args.search_before,
                args.search_after,
                args.validation_before,
                args.validation_after,
            )
        ],
        comparison_kind=args.comparison_kind,
        post_sample=post_sample,
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as target:
        json.dump(decision, target, ensure_ascii=False, indent=2)
        target.write("\n")
    if args.ledger:
        append_ledger(args.ledger, decision, proposal, verdicts)
    manifest = create_stage_manifest(
        os.path.dirname(__file__),
        "validator",
        inputs,
        {
            "comparison_kind": args.comparison_kind,
            "verdict_schema": VERDICTS_SCHEMA,
            "decision_schema": DECISION_SCHEMA,
        },
        "fixed L0/L1 + S/V no-drop + prediction-majority gates",
    )
    manifest["summary"] = {
        "decision": decision["decision"],
        "local_gate": decision["gates"]["local"]["passed"],
        "utility_gate": decision["gates"]["utility"]["passed"],
        "behavior_gate": decision["gates"]["behavior"]["passed"],
    }
    write_manifest(args.out + ".manifest.json", manifest)
    print(f"{decision['decision']}: {decision['reason']}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
