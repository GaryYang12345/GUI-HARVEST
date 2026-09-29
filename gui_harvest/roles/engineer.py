"""Deterministic boundary around the source-aware Harness Engineer.

The Clusterer discovers behavioral regularities without reading source.  This
module turns its output into a transparent work queue, then validates the
artifact returned by a source-reading Engineer session.  It deliberately does
not pretend that frequency is the same thing as fixability:

* recurrent candidates are cross-task modes with a behavioral target;
* opportunistic candidates are evidenced residue findings that may still admit
  a small, general repair.

The Engineer itself may be a tool-using coding agent.  Its output is data, not
the promotion decision; Validator and deterministic gates own that decision.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
from collections import Counter

from gui_harvest.evidence.outcome_categories import OUTCOME_CATEGORIES
from gui_harvest.campaign.provenance import create_stage_manifest, write_manifest

QUEUE_SCHEMA = "gui-harvest-engineer-queue-v1"
PROPOSAL_SCHEMA = "gui-harvest-intervention-v1"
LANES = {"recurrent", "opportunistic"}
CHANGE_KINDS = {"parameter", "prompt", "control_flow", "new_component"}
PATCH_ACTIONS = {"modify", "create", "delete"}
_GENERICITY_MARKERS = ("task_id", "uuid", "specific task", "one task only")


def _load_json(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _load_findings(path):
    rows = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("analysis_status", "ok") == "ok":
                rows.extend(row.get("findings") or [])
    return rows


def _candidate_id(lane, outcome_category, member_ids):
    payload = json.dumps(
        [lane, outcome_category, sorted(member_ids)], ensure_ascii=False
    ).encode()
    return "C-" + hashlib.sha256(payload).hexdigest()[:10]


def _rank_key(candidate):
    """Fixed, inspectable ordering; never an Engineer self-score."""
    evidence = candidate["evidence"]
    return (
        -int(candidate["lane"] == "recurrent"),
        -evidence["tasks"],
        -evidence["findings"],
        -evidence["successful_contrasts"],
        candidate["candidate_id"],
    )


def build_queue(modes_payload, findings):
    by_id = {f["finding_id"]: f for f in findings}
    candidates = []
    assigned = set()

    for mode in modes_payload.get("modes") or []:
        members = [fid for fid in mode.get("members") or [] if fid in by_id]
        if mode.get("is_mode") and mode.get("behavioral_target") and members:
            fs = [by_id[fid] for fid in members]
            assigned.update(members)
            outcome_category = mode["outcome_category"]
            candidates.append(
                {
                    "candidate_id": _candidate_id("recurrent", outcome_category, members),
                    "lane": "recurrent",
                    "outcome_category": outcome_category,
                    "mode_id": mode.get("mode_id"),
                    "label": mode.get("label"),
                    "mechanism": mode.get("mechanism"),
                    "membership_test": mode.get("membership_test"),
                    "behavioral_target": mode.get("behavioral_target"),
                    "member_finding_ids": members,
                    "target_task_ids": sorted({f["task_id"] for f in fs}),
                    "evidence": {
                        "findings": len(fs),
                        "tasks": len({f["task_id"] for f in fs}),
                        "domains": dict(Counter(f.get("domain") for f in fs)),
                        "successful_contrasts": sum(
                            bool(f.get("contrast")) for f in fs
                        ),
                    },
                    "eligibility": {
                        "source_attribution_required": True,
                        "not_yet_a_patch": True,
                    },
                }
            )

    # Residue is not silently discarded.  Each evidenced finding becomes an
    # Engineer inspection candidate, but never a publishable recurrent mode.
    for finding in findings:
        fid = finding["finding_id"]
        if fid in assigned or not finding.get("counterfactual"):
            continue
        outcome_category = finding.get("outcome_category")
        if outcome_category not in OUTCOME_CATEGORIES or outcome_category == "EXOGENOUS":
            continue
        candidates.append(
            {
                "candidate_id": _candidate_id("opportunistic", outcome_category, [fid]),
                "lane": "opportunistic",
                "outcome_category": outcome_category,
                "mode_id": None,
                "label": "EVIDENCED_RESIDUE",
                "mechanism": finding.get("mechanism"),
                "membership_test": (
                    "the cited finding's decisive-step evidence satisfies its "
                    "recorded action/outcome signature"
                ),
                "behavioral_target": finding.get("counterfactual"),
                "member_finding_ids": [fid],
                "target_task_ids": [finding["task_id"]],
                "evidence": {
                    "findings": 1,
                    "tasks": 1,
                    "domains": {finding.get("domain"): 1},
                    "successful_contrasts": int(bool(finding.get("contrast"))),
                },
                "eligibility": {
                    "source_attribution_required": True,
                    "must_justify_cross_task_generality": True,
                    "not_yet_a_patch": True,
                },
            }
        )

    candidates.sort(key=_rank_key)
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
    return {
        "schema_version": QUEUE_SCHEMA,
        "policy": {
            "lanes": {
                "recurrent": "cross-task mode with evidence-backed behavioral target",
                "opportunistic": (
                    "one-task residue with a counterfactual; Engineer must prove "
                    "a shared source lever and cross-task-safe intervention"
                ),
            },
            "ranking": (
                "lane, independent task support, finding support, successful "
                "contrasts; source attribution and risk are checked later"
            ),
        },
        "n_candidates": len(candidates),
        "n_recurrent": sum(c["lane"] == "recurrent" for c in candidates),
        "n_opportunistic": sum(c["lane"] == "opportunistic" for c in candidates),
        "candidates": candidates,
    }


def _path_matches(path, patterns):
    normalized = str(path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns or [])


def validate_capability_manifest(manifest):
    problems = []
    if manifest.get("schema_version") != "gui-harvest-capability-manifest-v1":
        problems.append("capability manifest has an unknown schema_version")
    if not manifest.get("harness_root"):
        problems.append("capability manifest has no harness_root")
    if not manifest.get("editable_paths"):
        problems.append("capability manifest has no editable_paths")
    limits = manifest.get("limits") or {}
    if int(limits.get("max_touched_files") or 0) <= 0:
        problems.append("capability manifest needs a positive max_touched_files")
    return problems


def validate_proposal(proposal, queue, capability_manifest=None, ledger=None):
    """Return a list of contract violations. Empty means structurally usable."""
    problems = []
    if proposal.get("schema_version") != PROPOSAL_SCHEMA:
        problems.append(f"schema_version must be {PROPOSAL_SCHEMA}")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(proposal.get("base_behavior_hash") or "")
    ):
        problems.append("base_behavior_hash must be a full SHA-256")
    candidates = {
        c["candidate_id"]: c for c in queue.get("candidates") or []
    }
    raw_targets = proposal.get("targets")
    if raw_targets is None:
        raw_targets = []
    if not isinstance(raw_targets, list) or not raw_targets:
        problems.append("targets must select at least one frozen Engineer candidate")
        return problems
    selected = []
    seen_candidates = set()
    for index, target in enumerate(raw_targets):
        if not isinstance(target, dict):
            problems.append(f"targets[{index}] must be an object")
            continue
        candidate_id = target.get("candidate_id")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            problems.append(
                f"targets[{index}].candidate_id is not in the frozen Engineer queue"
            )
            continue
        if candidate_id in seen_candidates:
            problems.append(f"targets[{index}] duplicates candidate {candidate_id}")
            continue
        seen_candidates.add(candidate_id)
        selected.append(candidate)
        if target.get("lane") != candidate["lane"]:
            problems.append(f"targets[{index}].lane differs from the frozen queue")
        if target.get("outcome_category") != candidate["outcome_category"]:
            problems.append(
                f"targets[{index}].outcome_category differs from the frozen queue"
            )
    if not selected:
        return problems

    attribution = proposal.get("attribution") or {}
    locations = attribution.get("source_locations")
    if not isinstance(locations, list) or not locations:
        problems.append("attribution.source_locations must be a non-empty list")
    else:
        for index, location in enumerate(locations):
            if not isinstance(location, dict) or not location.get("path"):
                problems.append(f"source_locations[{index}] has no path")
            if not (location or {}).get("anchor"):
                problems.append(f"source_locations[{index}] has no source anchor")

    intervention = proposal.get("intervention") or {}
    if intervention.get("change_kind") not in CHANGE_KINDS:
        problems.append(f"intervention.change_kind must be one of {sorted(CHANGE_KINDS)}")
    patch = intervention.get("patch_manifest")
    if not isinstance(patch, list) or not patch:
        problems.append("intervention.patch_manifest must be non-empty")
    elif capability_manifest:
        cap_problems = validate_capability_manifest(capability_manifest)
        problems.extend(cap_problems)
        editable = capability_manifest.get("editable_paths") or []
        create = capability_manifest.get("create_paths") or []
        read_only = capability_manifest.get("read_only_paths") or []
        limits = capability_manifest.get("limits") or {}
        max_files = int(limits.get("max_touched_files") or 0)
        paths = {str(row.get("path") or "") for row in patch}
        if max_files and len(paths) > max_files:
            problems.append(
                f"patch touches {len(paths)} files; capability limit is {max_files}"
            )
        for index, row in enumerate(patch):
            path = row.get("path")
            action = row.get("action", "modify")
            if action not in PATCH_ACTIONS:
                problems.append(
                    f"patch_manifest[{index}].action must be one of "
                    f"{sorted(PATCH_ACTIONS)}"
                )
                continue
            allowed = create if action == "create" else editable
            if _path_matches(path, read_only):
                problems.append(f"patch_manifest[{index}] touches read-only {path}")
            elif not _path_matches(path, allowed):
                problems.append(
                    f"patch_manifest[{index}] path {path!r} is outside the "
                    f"capability manifest for action {action!r}"
                )
    genericity = str(intervention.get("genericity") or "")
    if len(genericity.split()) < 8:
        problems.append("intervention.genericity must explain cross-task scope")
    if any(marker in genericity.lower() for marker in _GENERICITY_MARKERS):
        problems.append("intervention.genericity appears task-specific")

    predictions = proposal.get("predictions")
    if not isinstance(predictions, list) or not predictions:
        problems.append("predictions must be a non-empty pre-registered list")
    else:
        ids = set()
        allowed_tasks = {
            task_id
            for candidate in selected
            for task_id in candidate["target_task_ids"]
        }
        allowed_candidates = {candidate["candidate_id"] for candidate in selected}
        for index, prediction in enumerate(predictions):
            pid = prediction.get("prediction_id")
            if not pid or pid in ids:
                problems.append(f"predictions[{index}] has missing/duplicate id")
            ids.add(pid)
            if not prediction.get("observable_test"):
                problems.append(f"predictions[{index}] has no observable_test")
            tasks = set(prediction.get("target_task_ids") or [])
            if not tasks:
                problems.append(f"predictions[{index}] has no target tasks")
            if not tasks <= allowed_tasks:
                problems.append(
                    f"predictions[{index}] adds tasks outside frozen evidence"
                )
            prediction_candidates = set(
                prediction.get("target_candidate_ids") or allowed_candidates
            )
            if not prediction_candidates:
                problems.append(
                    f"predictions[{index}] has no target candidate binding"
                )
            elif not prediction_candidates <= allowed_candidates:
                problems.append(
                    f"predictions[{index}] names a candidate outside targets"
                )

    checks = proposal.get("checks") or {}
    if checks.get("l0") not in {"passed", "failed"}:
        problems.append("checks.l0 must be passed or failed")
    if checks.get("l1") not in {"passed", "failed"}:
        problems.append("checks.l1 must be passed or failed")
    if not checks.get("report_path"):
        problems.append("checks.report_path is required")
    if not re.fullmatch(r"[0-9a-f]{64}", str(checks.get("report_sha256") or "")):
        problems.append("checks.report_sha256 must be a full SHA-256")
    prior_rollbacks = [
        row
        for row in (ledger or {}).get("attempts") or []
        if row.get("base_behavior_hash") == proposal.get("base_behavior_hash")
        and (row.get("decision") or {}).get("decision") == "rollback"
    ]
    if prior_rollbacks:
        addressed = {
            row.get("proposal_id"): row
            for row in proposal.get("prior_attempts_addressed") or []
            if isinstance(row, dict)
        }
        for attempt in prior_rollbacks:
            proposal_id = attempt.get("proposal_id")
            response = str((addressed.get(proposal_id) or {}).get("response") or "")
            if len(response.split()) < 8:
                problems.append(
                    f"prior rollback {proposal_id} is not substantively addressed"
                )
        current_patch = sorted(
            (row.get("path"), row.get("sha256"))
            for row in (proposal.get("intervention") or {}).get("patch_manifest") or []
        )
        for attempt in prior_rollbacks:
            prior_patch = sorted(
                (row.get("path"), row.get("sha256"))
                for row in (attempt.get("intervention") or {}).get("patch_manifest") or []
            )
            if current_patch and current_patch == prior_patch:
                problems.append(
                    f"proposal exactly repeats rejected patch {attempt.get('proposal_id')}"
                )
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modes", required=True)
    parser.add_argument("--findings", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--proposal", help="validate one Engineer proposal")
    parser.add_argument("--capability-manifest")
    parser.add_argument("--ledger", help="prior attempt ledger for retry validation")
    args = parser.parse_args()

    modes = _load_json(args.modes)
    findings = _load_findings(args.findings)
    queue = build_queue(modes, findings)
    if args.proposal:
        proposal = _load_json(args.proposal)
        capability = (
            _load_json(args.capability_manifest)
            if args.capability_manifest
            else None
        )
        ledger = _load_json(args.ledger) if args.ledger else None
        problems = validate_proposal(proposal, queue, capability, ledger)
        if problems:
            raise SystemExit("Engineer proposal rejected:\n- " + "\n- ".join(problems))
        print(f"Engineer proposal valid: {proposal.get('proposal_id')}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as target:
        json.dump(queue, target, ensure_ascii=False, indent=2)
        target.write("\n")
    manifest = create_stage_manifest(
        os.path.dirname(__file__),
        "engineer_queue",
        [
            args.modes,
            args.findings,
            *([args.capability_manifest] if args.capability_manifest else []),
            *([args.ledger] if args.ledger else []),
        ],
        {"queue_schema": QUEUE_SCHEMA, "proposal_schema": PROPOSAL_SCHEMA},
        "deterministic dual-lane candidate policy",
    )
    manifest["summary"] = {
        key: queue[key]
        for key in ("n_candidates", "n_recurrent", "n_opportunistic")
    }
    write_manifest(args.out + ".manifest.json", manifest)
    print(
        f"wrote {queue['n_recurrent']} recurrent + "
        f"{queue['n_opportunistic']} opportunistic candidates to {args.out}"
    )


if __name__ == "__main__":
    main()
