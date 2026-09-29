"""Public `gui-harvest` command-line interface."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import os
import subprocess
from pathlib import Path

from gui_harvest.campaign.baseline import ingest_baseline
from gui_harvest.campaign.adapters import get_adapter
from gui_harvest.campaign.rollouts import audit_runs, write_audit
from gui_harvest.campaign.samples import build_sample
from gui_harvest.campaign.provenance import sha256_file
from gui_harvest.config import load_config, write_template
from gui_harvest.campaign.orchestrator import (
    abort_round,
    freeze_round,
    run_engineer,
    start_round,
    validate_round,
)
from gui_harvest.campaign.flow import stopping_status
from gui_harvest.campaign.project import (
    ledger_path,
    initialize,
    load,
    load_project,
    attempt_dir,
    save_campaign,
    state_path,
    write,
)
from gui_harvest.campaign.splits import (
    assign_splits,
    load_tasks,
    open_sealed_test,
)


def _runs(values):
    result = {}
    for value in values or []:
        name, separator, path = value.partition("=")
        if not separator or not name or not path:
            raise ValueError(f"invalid run {value!r}; expected NAME=RESULT_DIR")
        result[name] = str(Path(path).resolve())
    return result


def _number(campaign, supplied):
    number = int(supplied or campaign.get("current_attempt") or 0)
    if not number:
        raise ValueError("no attempt is active; run `gui-harvest round start`")
    return number


def command_config_create(args):
    path = write_template(args.output)
    print(f"wrote {path}\nEdit it, then run: gui-harvest init --config {path}")


def command_init(args):
    config, campaign = initialize(args.config)
    print(
        f"initialized {campaign['project_name']} at "
        f"{config['project']['state_dir']}\n"
        f"next: gui-harvest baseline plan --config {args.config}"
    )


def command_splits_make(args):
    tasks = load_tasks(args.examples, args.universe)
    suites = assign_splits(
        tasks,
        {
            "search": args.search,
            "validation": args.validation,
            "test": args.test,
        },
        args.seed,
    )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, suite in suites.items():
        (out / f"{name}.json").write_text(
            json.dumps(suite, indent=2) + "\n", encoding="utf-8"
        )
    counts = {name: sum(len(ids) for ids in suite.values()) for name, suite in suites.items()}
    print(
        f"wrote Search/Validation/Test={counts['search']}/{counts['validation']}/{counts['test']} "
        f"to {out.resolve()}\n"
        "Use these files before the first rollout."
    )


def command_baseline_plan(args):
    config, campaign = load_project(args.config)
    public = load(state_path(config, "splits", "public.json"))
    suite = {}
    for split_name in ("search", "validation"):
        for domain, task_ids in public[split_name]["domains"].items():
            suite.setdefault(domain, []).extend(task_ids)
    task_file = state_path(config, "samples", "baseline-suite.json")
    write(task_file, {domain: sorted(set(ids)) for domain, ids in sorted(suite.items())})
    task_ids = sorted(task_id for ids in suite.values() for task_id in ids)
    template = config["rollout"].get("command") or []
    runs = []
    for repeat in range(1, config["protocol"]["rollouts_k"] + 1):
        replacements = {
            "{harness}": campaign["current_harness"]["root"],
            "{split_file}": str(Path(task_file).resolve()),
            "{repeat}": str(repeat),
            "{tag}": f"gui-harvest-baseline-k{repeat}",
            "{results_root}": config["benchmark"]["results_root"],
        }
        runs.append(
            {
                "name": f"r{repeat}",
                "tag": replacements["{tag}"],
                "command": [
                    replacements.get(str(value), str(value))
                    for value in template
                ],
                "result_dir": None,
            }
        )
    plan = {
        "schema_version": "gui-harvest-rollout-plan-v1",
        "kind": "full_baseline",
        "harness_root": campaign["current_harness"]["root"],
        "task_ids": task_ids,
        "task_file": str(Path(task_file).resolve()),
        "k": config["protocol"]["rollouts_k"],
        "runs": runs,
        "external_execution_required": not bool(template),
    }
    plan_path = state_path(config, "baseline-rollout-plan.json")
    write(plan_path, plan)
    print(
        f"baseline plan: {plan_path}\n"
        f"tasks={len(task_ids)}, K={plan['k']}"
    )
    if template:
        print(f"next: gui-harvest baseline run --config {args.config}")
    else:
        print(
            "Run the plan with your evaluator, then call "
            "`gui-harvest baseline ingest --run r1=...`."
        )


def command_baseline_run(args):
    config, _campaign = load_project(args.config)
    plan = load(state_path(config, "baseline-rollout-plan.json"))
    for run in plan["runs"]:
        command = run.get("command") or []
        if not command:
            raise ValueError("baseline plan has no executable rollout.command")
        print("+", " ".join(command), flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise RuntimeError(
                f"baseline rollout {run['name']} returned {result.returncode}"
            )
    print(
        "baseline commands completed; ingest resolved result directories with "
        "`gui-harvest baseline ingest --run r1=...`"
    )


def command_baseline(args):
    config, campaign = load_project(args.config)
    sample, campaign = ingest_baseline(
        config, campaign, _runs(args.run)
    )
    sizes = campaign["splits"]["sizes"]
    print(
        f"baseline accepted: {campaign['baseline_sample']['tasks']} tasks, "
        f"K={campaign['baseline_sample']['k']}, "
        f"mean={campaign['baseline_sample']['mean_utility']:.4f}\n"
        f"froze S/V/T={sizes['search']}/{sizes['validation']}/{sizes['test']}\n"
        f"sample: {sample}\n"
        f"public split: {campaign['splits']['public']['path']}\n"
        f"sealed Test: {campaign['splits']['sealed']['path']}\n"
        f"next: gui-harvest round start --config {args.config}"
    )


def command_round_start(args):
    config, campaign = load_project(args.config)
    meta = start_round(config, campaign, run_roles=not args.no_roles)
    print(
        f"round {meta['round']}, attempt {meta['attempt_in_round']} prepared "
        f"({meta['evidence_source']})\n"
        f"candidate: {meta['candidate_root']}\n"
        f"Engineer brief: {attempt_dir(config, meta['attempt'])}/ENGINEER_BRIEF.md\n"
        f"next: gui-harvest engineer run --config {args.config} "
        f"--attempt {meta['attempt']}"
    )


def command_engineer(args):
    config, campaign = load_project(args.config)
    number = _number(campaign, args.attempt)
    result = run_engineer(config, campaign, number)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "manual":
        print(
            "\nAfter the local agent finishes:\n"
            f"gui-harvest round freeze --config {args.config} --attempt {number}"
        )


def command_engineer_skill(args):
    resource = importlib.resources.files("gui_harvest").joinpath(
        "resources/GUI_HARVEST_ENGINEER_SKILL.md"
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote portable Engineer skill to {output.resolve()}")


def command_round_freeze(args):
    config, campaign = load_project(args.config)
    number = _number(campaign, args.attempt)
    plan = freeze_round(config, campaign, number)
    path = Path(attempt_dir(config, number)) / "rollout-plan.json"
    print(
        f"attempt {number} proposal frozen; rollout plan: {path}\n"
        f"tasks={len(plan['task_ids'])}, K={plan['k']}, "
        f"splits={','.join(plan['splits'])}"
    )
    if plan["external_execution_required"]:
        print(
            "No rollout.command is configured. Run your evaluator using the "
            "task_file and candidate_root in rollout-plan.json, then call "
            "`gui-harvest round validate --run r1=...`."
        )
    else:
        print(
            f"next: gui-harvest rollout run --config {args.config} --attempt {number}"
        )


def command_rollout_plan(args):
    config, campaign = load_project(args.config)
    number = _number(campaign, args.attempt)
    path = Path(attempt_dir(config, number)) / "rollout-plan.json"
    if not path.is_file():
        raise ValueError("attempt is not frozen; no rollout plan exists")
    print(path.read_text(encoding="utf-8"))


def command_rollout_run(args):
    config, campaign = load_project(args.config)
    number = _number(campaign, args.attempt)
    path = Path(attempt_dir(config, number)) / "rollout-plan.json"
    plan = load(path)
    for run in plan["runs"]:
        command = run.get("command") or []
        if not command:
            raise ValueError("rollout plan has no executable command")
        print("+", " ".join(command), flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise RuntimeError(
                f"rollout {run['name']} returned {result.returncode}"
            )
    print(
        "rollout commands completed. Resolve each result directory and run "
        "`gui-harvest round validate --run r1=...`."
    )


def command_round_validate(args):
    config, campaign = load_project(args.config)
    number = _number(campaign, args.attempt)
    decision, _release, next_action = validate_round(
        config, campaign, number, _runs(args.run)
    )
    print(
        f"{decision['decision']}: {decision['reason']}\n"
        f"next: {next_action['next_command']}"
    )


def command_round_next(args):
    config, campaign = load_project(args.config)
    if campaign["status"] != "attempt_closed":
        raise ValueError("the current attempt is not closed")
    meta = start_round(config, campaign, run_roles=True)
    print(
        f"round {meta['round']}, attempt {meta['attempt_in_round']} prepared; "
        f"next: gui-harvest engineer run --config {args.config} "
        f"--attempt {meta['attempt']}"
    )


def command_round_abort(args):
    config, campaign = load_project(args.config)
    number = _number(campaign, args.attempt)
    archive = abort_round(config, campaign, number, args.reason)
    print(
        f"attempt {number} aborted before GUI evaluation; archived at {archive}\n"
        f"next: gui-harvest round start --config {args.config}"
    )


def command_status(args):
    config, campaign = load_project(args.config)
    result = {"campaign": campaign}
    commands = {
        "awaiting_baseline": f"gui-harvest baseline plan --config {args.config}",
        "ready_for_round": f"gui-harvest round start --config {args.config}",
        "awaiting_engineer": f"gui-harvest engineer run --config {args.config}",
        "awaiting_rollouts": f"gui-harvest rollout plan --config {args.config}",
        "attempt_closed": f"gui-harvest round next --config {args.config}",
        "test_opened": f"gui-harvest test plan --config {args.config}",
        "awaiting_test_results": (
            f"gui-harvest test ingest --config {args.config} --run r1=..."
        ),
        "completed": "inspect .gui-harvest/final-report.json",
    }
    result["recommended_command"] = commands.get(campaign.get("status"))
    if campaign.get("current_attempt"):
        meta_path = (
            Path(attempt_dir(config, campaign["current_attempt"])) / "round-meta.json"
        )
        if meta_path.is_file():
            result["attempt"] = load(meta_path)
        next_path = meta_path.parent / "next-action.json"
        if next_path.is_file():
            result["next_action"] = load(next_path)
            result["recommended_command"] = result["next_action"].get(
                "next_command"
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_test_open(args):
    config, campaign = load_project(args.config)
    if campaign["status"] not in {"ready_for_round", "attempt_closed"}:
        raise ValueError(
            "finish or abort the active attempt before opening sealed Test"
        )
    if args.reason != "operator_stop":
        status = stopping_status(
            load(ledger_path(config)),
            campaign["current_harness"]["behavior_hash"],
            config["protocol"]["round_budget"],
            config["protocol"]["rejection_patience"],
        )
        required = "round_cap" if args.reason == "round_budget" else "patience"
        if required not in status["reasons"]:
            raise ValueError(
                f"ledger does not establish stop reason {args.reason}"
            )
    public_path = state_path(config, "splits", "public.json")
    sealed_path = state_path(config, "splits", "sealed-test.json")
    opened = open_sealed_test(
        load(public_path),
        load(sealed_path),
        stop_reason=args.reason,
        behavior_hash=campaign["current_harness"]["behavior_hash"],
    )
    opened_path = state_path(config, "splits", "opened-test.json")
    write(opened_path, opened)
    campaign["status"] = "test_opened"
    save_campaign(config, campaign)
    print(
        f"sealed Test opened after {args.reason}: "
        f"{opened['test']['count']} tasks\n{opened_path}\n"
        f"next: gui-harvest test plan --config {args.config}"
    )


def _test_plan(config, campaign):
    if campaign["status"] not in {"test_opened", "awaiting_test_results"}:
        raise ValueError("open sealed Test before creating its rollout plan")
    opened_path = state_path(config, "splits", "opened-test.json")
    opened = load(opened_path)
    test = opened.get("test") or {}
    task_ids = sorted(test.get("task_ids") or [])
    domains = test.get("domains") or {}
    if not task_ids or {task_id for ids in domains.values() for task_id in ids} != set(task_ids):
        raise ValueError("opened Test manifest is incomplete")
    task_file = state_path(config, "samples", "test-suite.json")
    write(task_file, domains)
    template = config["rollout"].get("command") or []
    runs = []
    for repeat in range(1, config["protocol"]["rollouts_k"] + 1):
        replacements = {
            "{harness}": campaign["current_harness"]["root"],
            "{split_file}": str(Path(task_file).resolve()),
            "{repeat}": str(repeat),
            "{tag}": f"gui-harvest-test-k{repeat}",
            "{results_root}": config["benchmark"]["results_root"],
        }
        runs.append(
            {
                "name": f"r{repeat}",
                "tag": replacements["{tag}"],
                "command": [
                    replacements.get(str(value), str(value))
                    for value in template
                ],
                "result_dir": None,
            }
        )
    return {
        "schema_version": "gui-harvest-rollout-plan-v1",
        "kind": "sealed_test",
        "harness_root": campaign["current_harness"]["root"],
        "harness_behavior_sha256": campaign["current_harness"][
            "runtime_behavior_sha256"
        ],
        "selected_behavior_hash": test.get("selected_behavior_hash"),
        "task_ids": task_ids,
        "task_file": str(Path(task_file).resolve()),
        "k": config["protocol"]["rollouts_k"],
        "runs": runs,
        "external_execution_required": not bool(template),
    }


def command_test_plan(args):
    config, campaign = load_project(args.config)
    plan = _test_plan(config, campaign)
    path = state_path(config, "test-rollout-plan.json")
    write(path, plan)
    campaign["status"] = "awaiting_test_results"
    save_campaign(config, campaign)
    print(
        f"Test plan: {path}\n"
        f"tasks={len(plan['task_ids'])}, K={plan['k']}"
    )
    if plan["external_execution_required"]:
        print(
            "Run the plan with your evaluator, then call "
            "`gui-harvest test ingest --run r1=...`."
        )
    else:
        print(f"next: gui-harvest test run --config {args.config}")


def command_test_run(args):
    config, campaign = load_project(args.config)
    if campaign["status"] != "awaiting_test_results":
        raise ValueError("create the Test rollout plan first")
    plan = load(state_path(config, "test-rollout-plan.json"))
    for run in plan["runs"]:
        command = run.get("command") or []
        if not command:
            raise ValueError("Test rollout plan has no executable command")
        print("+", " ".join(command), flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise RuntimeError(
                f"Test rollout {run['name']} returned {result.returncode}"
            )
    print(
        "Test commands completed. Resolve each result directory and run "
        "`gui-harvest test ingest --run r1=...`."
    )


def command_test_ingest(args):
    config, campaign = load_project(args.config)
    if campaign["status"] != "awaiting_test_results":
        raise ValueError("no Test rollout plan is awaiting results")
    plan = load(state_path(config, "test-rollout-plan.json"))
    runs = _runs(args.run)
    audit = audit_runs(
        runs, plan["task_ids"], config["protocol"]["rollouts_k"]
    )
    audit_path = state_path(config, "test-rollout-audit.json")
    write_audit(audit_path, audit)
    if not audit["complete"]:
        raise ValueError(
            f"Test rollouts are incomplete; rerun plan written to {audit_path}"
        )
    sample = build_sample(
        runs,
        task_ids=plan["task_ids"],
        harness_card=campaign["current_harness"]["harness_card"]["path"],
        expected_k=config["protocol"]["rollouts_k"],
    )
    expected_sha = get_adapter(
        campaign["current_harness"]["adapter"]
    ).runtime_behavior_hash(campaign["current_harness"]["root"])
    observed_sha = sample["compatibility"].get("behavior_sha256")
    if not observed_sha or observed_sha != expected_sha:
        raise ValueError(
            "Test rollout fingerprint does not match the frozen selected harness"
        )
    sample_path = state_path(config, "samples", "test.json")
    write(sample_path, sample)
    task_rows = []
    domain_values = {}
    all_values = []
    for task in sample["tasks"]:
        values = list((task.get("rewards") or {}).values())
        mean = sum(values) / len(values)
        task_rows.append(
            {
                "task_id": task["task_id"],
                "domain": task["domain"],
                "mean": mean,
                "rewards": task["rewards"],
            }
        )
        domain_values.setdefault(task["domain"], []).extend(values)
        all_values.extend(values)
    campaign["accounting"]["gui_task_executions"] += (
        len(task_rows) * config["protocol"]["rollouts_k"]
    )
    report = {
        "schema_version": "gui-harvest-final-report-v1",
        "selected_harness": campaign["current_harness"],
        "test": {
            "tasks": len(task_rows),
            "k": config["protocol"]["rollouts_k"],
            "mean_utility": sum(all_values) / len(all_values),
            "domain_mean_utility": {
                domain: sum(values) / len(values)
                for domain, values in sorted(domain_values.items())
            },
            "task_results": task_rows,
            "sample": {
                "path": str(Path(sample_path).resolve()),
                "sha256": sha256_file(sample_path),
            },
        },
        "accounting": campaign["accounting"],
    }
    report_path = state_path(config, "final-report.json")
    write(report_path, report)
    campaign["status"] = "completed"
    campaign["test_evaluation"] = {
        "report": str(Path(report_path).resolve()),
        "sha256": sha256_file(report_path),
        "mean_utility": report["test"]["mean_utility"],
        "tasks": len(task_rows),
        "k": config["protocol"]["rollouts_k"],
    }
    save_campaign(config, campaign)
    print(
        f"Test accepted: mean={report['test']['mean_utility']:.4f}, "
        f"tasks={len(task_rows)}, K={config['protocol']['rollouts_k']}\n"
        f"final report: {report_path}"
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gui-harvest",
        description="GUI-HARVEST automatic GUI-agent harness optimization",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="create project configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    create = config_sub.add_parser("create")
    create.add_argument("--output", default="gui-harvest.toml")
    create.set_defaults(function=command_config_create)

    init = sub.add_parser("init", help="initialize campaign state")
    init.add_argument("--config", default="gui-harvest.toml")
    init.set_defaults(function=command_init)

    splits = sub.add_parser("splits", help="create the fixed task splits")
    splits_sub = splits.add_subparsers(dest="splits_command", required=True)
    make = splits_sub.add_parser("make")
    make.add_argument("--examples", required=True, help="benchmark examples/<domain> directory")
    make.add_argument("--universe", help="OSWorld suite limiting the eligible tasks")
    make.add_argument("--seed", required=True, help="split seed (any string)")
    make.add_argument("--search", type=int, default=80, help="Search split size")
    make.add_argument("--validation", type=int, default=80, help="Validation split size")
    make.add_argument("--test", type=int, default=201, help="Test split size")
    make.add_argument("--out-dir", default="splits")
    make.set_defaults(function=command_splits_make)

    baseline = sub.add_parser("baseline", help="ingest full K-run baseline")
    baseline_sub = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_plan = baseline_sub.add_parser("plan")
    baseline_plan.add_argument("--config", default="gui-harvest.toml")
    baseline_plan.set_defaults(function=command_baseline_plan)
    baseline_run = baseline_sub.add_parser("run")
    baseline_run.add_argument("--config", default="gui-harvest.toml")
    baseline_run.set_defaults(function=command_baseline_run)
    ingest = baseline_sub.add_parser("ingest")
    ingest.add_argument("--config", default="gui-harvest.toml")
    ingest.add_argument("--run", action="append", required=True)
    ingest.set_defaults(function=command_baseline)

    round_parser = sub.add_parser(
        "round", help="candidate attempts within the current round"
    )
    round_sub = round_parser.add_subparsers(dest="round_command", required=True)
    start = round_sub.add_parser("start", help="open the first attempt")
    start.add_argument("--config", default="gui-harvest.toml")
    start.add_argument("--no-roles", action="store_true", help=argparse.SUPPRESS)
    start.set_defaults(function=command_round_start)
    freeze = round_sub.add_parser("freeze")
    freeze.add_argument("--config", default="gui-harvest.toml")
    freeze.add_argument("--attempt", type=int)
    freeze.set_defaults(function=command_round_freeze)
    validate = round_sub.add_parser("validate")
    validate.add_argument("--config", default="gui-harvest.toml")
    validate.add_argument("--attempt", type=int)
    validate.add_argument("--run", action="append", required=True)
    validate.set_defaults(function=command_round_validate)
    next_round = round_sub.add_parser(
        "next",
        help="open the next attempt: in the next round after a promotion, "
        "in the same round after a rollback",
    )
    next_round.add_argument("--config", default="gui-harvest.toml")
    next_round.set_defaults(function=command_round_next)
    abort = round_sub.add_parser(
        "abort", help="archive an unevaluated candidate without consuming an attempt"
    )
    abort.add_argument("--config", default="gui-harvest.toml")
    abort.add_argument("--attempt", type=int)
    abort.add_argument("--reason", required=True)
    abort.set_defaults(function=command_round_abort)

    engineer = sub.add_parser("engineer", help="run or hand off Engineer")
    engineer_sub = engineer.add_subparsers(dest="engineer_command", required=True)
    engineer_run = engineer_sub.add_parser("run")
    engineer_run.add_argument("--config", default="gui-harvest.toml")
    engineer_run.add_argument("--attempt", type=int)
    engineer_run.set_defaults(function=command_engineer)
    engineer_skill = engineer_sub.add_parser(
        "skill", help="export the portable local-agent skill"
    )
    engineer_skill.add_argument(
        "--output", default="GUI_HARVEST_ENGINEER_SKILL.md"
    )
    engineer_skill.set_defaults(function=command_engineer_skill)

    rollout = sub.add_parser("rollout", help="inspect or run candidate rollout plan")
    rollout_sub = rollout.add_subparsers(dest="rollout_command", required=True)
    plan = rollout_sub.add_parser("plan")
    plan.add_argument("--config", default="gui-harvest.toml")
    plan.add_argument("--attempt", type=int)
    plan.set_defaults(function=command_rollout_plan)
    run = rollout_sub.add_parser("run")
    run.add_argument("--config", default="gui-harvest.toml")
    run.add_argument("--attempt", type=int)
    run.set_defaults(function=command_rollout_run)

    status = sub.add_parser("status", help="show state and next safe command")
    status.add_argument("--config", default="gui-harvest.toml")
    status.set_defaults(function=command_status)

    test = sub.add_parser("test", help="sealed Test lifecycle")
    test_sub = test.add_subparsers(dest="test_command", required=True)
    opened = test_sub.add_parser("open")
    opened.add_argument("--config", default="gui-harvest.toml")
    opened.add_argument(
        "--reason",
        choices=["round_budget", "patience", "operator_stop"],
        default="operator_stop",
    )
    opened.set_defaults(function=command_test_open)
    test_plan = test_sub.add_parser("plan")
    test_plan.add_argument("--config", default="gui-harvest.toml")
    test_plan.set_defaults(function=command_test_plan)
    test_run = test_sub.add_parser("run")
    test_run.add_argument("--config", default="gui-harvest.toml")
    test_run.set_defaults(function=command_test_run)
    test_ingest = test_sub.add_parser("ingest")
    test_ingest.add_argument("--config", default="gui-harvest.toml")
    test_ingest.add_argument("--run", action="append", required=True)
    test_ingest.set_defaults(function=command_test_ingest)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
    except (ValueError, FileExistsError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()

