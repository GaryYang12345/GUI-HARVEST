import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from gui_harvest.campaign.adapters import get_adapter
from gui_harvest.cli import (
    command_baseline,
    command_test_ingest,
    command_test_open,
    command_test_plan,
)
from gui_harvest.config import load_config
from gui_harvest.campaign.orchestrator import (
    abort_round,
    freeze_round,
    start_round,
    validate_round,
)
from gui_harvest.campaign.flow import stopping_status
from gui_harvest.campaign.project import attempt_dir, initialize, load


def write_json(path, payload):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload) + "\n")


def make_result_root(root, name, harness_sha, scores):
    run = Path(root) / name
    run.mkdir()
    write_json(
        run / "run_manifest.json",
        {
            "code": {"behavior_sha256": harness_sha},
            "model": {"name": "frozen", "temperature": 0, "seed": 7},
            "run": {
                "suite_meta_sha256": "suite",
                "max_steps": 15,
                "screen_size": [1920, 1080],
                "action_space": "pyautogui",
                "observation_type": "screenshot",
            },
        },
    )
    for task_id, score in scores.items():
        task = run / "chrome" / task_id
        task.mkdir(parents=True)
        (task / "result.txt").write_text(str(score))
        (task / "instruction.txt").write_text(f"task {task_id}")
        (task / "step_1.png").write_bytes(b"png")
        (task / "traj.jsonl").write_text(
            json.dumps(
                {
                    "step_num": 1,
                    "plan_code": "agent.wait(1)",
                    "plan": "wait",
                    "reflection": "",
                    "action_effect": None,
                    "screenshot_file": "step_1.png",
                }
            )
            + "\n"
        )
    return str(run)


class RoundE2ETests(unittest.TestCase):
    def test_empty_project_to_promote_and_round2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = root / "baseline"
            harness.mkdir()
            (harness / "worker.py").write_text("VALUE = 1\n")
            (harness / "docs").mkdir()
            (harness / "docs" / "CARD.md").write_text("# baseline\n")
            splits = root / "splits"
            splits.mkdir()
            for name, ids in {
                "search": ["t0", "t1", "t2"],
                "validation": ["t3", "t4", "t5"],
                "test": ["t6", "t7", "t8"],
            }.items():
                write_json(splits / f"{name}.json", {"chrome": ids})
            config_path = root / "gui-harvest.toml"
            config_path.write_text(
                f"""
config_version = 1
[project]
name = "e2e"
state_dir = ".state"
seed = "e2e"
[protocol]
rollouts_k = 2
round_budget = 3
rejection_patience = 2
[splits]
search_file = "splits/search.json"
validation_file = "splits/validation.json"
test_file = "splits/test.json"
search = 3
validation = 3
test = 3
[harness]
adapter = "generic_python"
baseline_root = "baseline"
harness_card = "docs/CARD.md"
capability_manifest = "capability.json"
[benchmark]
results_root = "results"
[models.analyst]
backend = "openrouter"
model = "test"
workers = 1
[models.clusterer]
backend = "openrouter"
model = "test"
workers = 1
[models.engineer]
backend = "local"
model = "test"
[models.validator]
backend = "openrouter"
model = "test"
workers = 1
max_tokens = 1000
"""
            )
            config, campaign = initialize(config_path)
            adapter = get_adapter("generic_python")
            baseline_sha = adapter.runtime_behavior_hash(harness)
            baseline_scores = {f"t{i}": 0 for i in range(6)}
            runs = {
                "r1": make_result_root(root, "base-r1", baseline_sha, baseline_scores),
                "r2": make_result_root(root, "base-r2", baseline_sha, baseline_scores),
            }
            command_baseline(
                SimpleNamespace(
                    config=str(config_path),
                    run=[f"{name}={path}" for name, path in runs.items()],
                )
            )
            campaign = load(config["project"]["state_dir"] + "/campaign.json")

            real_run = __import__(
                "gui_harvest.campaign.orchestrator", fromlist=["_run"]
            )._run

            def fake_role(argv, **kwargs):
                values = [str(value) for value in argv]
                if "gui_harvest.roles.analyst" in values:
                    out = Path(values[values.index("--out") + 1])
                    analyst_sample = load(values[values.index("--sample") + 1])
                    target_ids = [
                        task["task_id"] for task in analyst_sample["tasks"][:2]
                    ]
                    rows = []
                    for index, task_id in enumerate(target_ids, 1):
                        rows.append(
                            {
                                "task_id": task_id,
                                "analysis_status": "ok",
                                "findings": [
                                    {
                                        "finding_id": f"f{index}",
                                        "task_id": task_id,
                                        "domain": "chrome",
                                        "outcome_category": "ACTION_NO_EFFECT",
                                        "mechanism": "no progress after an action",
                                        "counterfactual": "verify progress before continuing",
                                        "contrast": None,
                                    }
                                ],
                            }
                        )
                    out.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                    return
                if "gui_harvest.roles.cluster" in values:
                    out = Path(values[values.index("--out") + 1])
                    write_json(
                        out,
                        {
                            "n_modes": 1,
                            "n_residue_findings": 0,
                            "modes": [
                                {
                                    "mode_id": "M01",
                                    "outcome_category": "ACTION_NO_EFFECT",
                                    "label": "NO_PROGRESS",
                                    "mechanism": "no progress after an action",
                                    "membership_test": "action leaves state unchanged",
                                    "behavioral_target": "verify before continuing",
                                    "members": ["f1", "f2"],
                                    "is_mode": True,
                                }
                            ],
                        },
                    )
                    return
                if "gui_harvest.roles.validator_agent" in values:
                    proposal = load(values[values.index("--proposal") + 1])
                    out = Path(values[values.index("--out") + 1])
                    verdicts = []
                    for prediction in proposal["predictions"]:
                        for task_id in prediction["target_task_ids"]:
                            verdicts.append(
                                {
                                    "prediction_id": prediction["prediction_id"],
                                    "task_id": task_id,
                                    "verdict": "supported",
                                    "summary": "predicted recovery occurred",
                                    "citations": [
                                        {"run": "A", "step": 1, "claim": "recovered"}
                                    ],
                                }
                            )
                    write_json(
                        out,
                        {
                            "schema_version": "gui-harvest-validator-verdicts-v1",
                            "jobs_expected": len(verdicts),
                            "jobs_completed": len(verdicts),
                            "verdicts": verdicts,
                        },
                    )
                    return
                return real_run(argv, **kwargs)

            with patch("gui_harvest.campaign.orchestrator._run", side_effect=fake_role):
                meta = start_round(config, campaign)
                self.assertEqual(
                    (meta["round"], meta["attempt"], meta["attempt_in_round"]),
                    (1, 1, 1),
                )
                directory = Path(attempt_dir(config, 1))
                queue = load(directory / "engineer_queue.json")
                candidate = Path(meta["candidate_root"])
                (candidate / "worker.py").write_text("VALUE = 2\n")
                (candidate / "docs" / "CARD.md").write_text("# candidate\n")
                selected = queue["candidates"][0]
                write_json(
                    directory / "proposal.pre.json",
                    {
                        "proposal_id": "P1",
                        "targets": [
                            {
                                "candidate_id": selected["candidate_id"],
                                "lane": selected["lane"],
                                "outcome_category": selected["outcome_category"],
                            }
                        ],
                        "attribution": {
                            "surface": "worker",
                            "mechanism": "missing progress verification",
                            "source_locations": [
                                {
                                    "path": "worker.py",
                                    "symbol": "VALUE",
                                    "anchor": "VALUE = 1",
                                }
                            ],
                        },
                        "intervention": {
                            "change_kind": "control_flow",
                            "summary": "verify progress before the next action",
                            "genericity": (
                                "Applies to unchanged transitions across all tasks "
                                "and applications without literal task knowledge."
                            ),
                        },
                        "predictions": [
                            {
                                "prediction_id": "P1-behavior",
                                "target_candidate_ids": [selected["candidate_id"]],
                                "statement": "the agent verifies progress",
                                "observable_test": "post-edit trace verifies progress",
                                "target_task_ids": selected["target_task_ids"],
                                "required_for_promotion": True,
                            }
                        ],
                        "invariants": ["interfaces remain stable"],
                        "expected_regression_risks": [],
                    },
                )
                freeze_round(config, campaign, 1)
                split = load(config["project"]["state_dir"] + "/splits/public.json")
                evaluated = sorted(
                    set(split["search"]["task_ids"])
                    | set(split["validation"]["task_ids"])
                )
                after_scores = {task_id: 0 for task_id in evaluated}
                after_scores[split["search"]["task_ids"][0]] = 1
                candidate_sha = adapter.runtime_behavior_hash(candidate)
                after_runs = {
                    "r1": make_result_root(
                        root, "after-r1", candidate_sha, after_scores
                    ),
                    "r2": make_result_root(
                        root, "after-r2", candidate_sha, after_scores
                    ),
                }
                decision, _release, _next = validate_round(
                    config, campaign, 1, after_runs
                )
                self.assertEqual(decision["decision"], "promote")
                self.assertNotIn(
                    "task_ids",
                    decision["gates"]["utility"]["profiles"][
                        "validation_after"
                    ],
                )
                self.assertEqual(
                    decision["gates"]["utility"]["task_changes"]["validation"],
                    [],
                )

                current = load(config["project"]["state_dir"] + "/campaign.json")
                self.assertEqual(current["current_harness"]["root"], str(candidate))
                round2 = start_round(config, current)
                self.assertEqual(
                    (round2["round"], round2["attempt"], round2["attempt_in_round"]),
                    (2, 2, 1),
                )
                self.assertEqual(
                    round2["evidence_source"],
                    "fresh_current_harness_analysis",
                )
                directory2 = Path(attempt_dir(config, 2))
                queue2 = load(directory2 / "engineer_queue.json")
                selected2 = queue2["candidates"][0]
                candidate2 = Path(round2["candidate_root"])
                (candidate2 / "worker.py").write_text("VALUE = 3\n")
                (candidate2 / "docs" / "CARD.md").write_text("# rejected\n")
                write_json(
                    directory2 / "proposal.pre.json",
                    {
                        "proposal_id": "P2",
                        "targets": [
                            {
                                "candidate_id": selected2["candidate_id"],
                                "lane": selected2["lane"],
                                "outcome_category": selected2["outcome_category"],
                            }
                        ],
                        "attribution": {
                            "surface": "worker",
                            "mechanism": "second progress strategy",
                            "source_locations": [
                                {
                                    "path": "worker.py",
                                    "symbol": "VALUE",
                                    "anchor": "VALUE = 2",
                                }
                            ],
                        },
                        "intervention": {
                            "change_kind": "control_flow",
                            "summary": "use a materially different progress strategy",
                            "genericity": (
                                "Applies to all unchanged transitions without "
                                "task-specific values, applications, or routes."
                            ),
                        },
                        "predictions": [
                            {
                                "prediction_id": "P2-behavior",
                                "target_candidate_ids": [selected2["candidate_id"]],
                                "statement": "the alternate strategy verifies progress",
                                "observable_test": "post-edit trace verifies progress",
                                "target_task_ids": selected2["target_task_ids"],
                                "required_for_promotion": True,
                            }
                        ],
                        "prior_attempts_addressed": [],
                        "invariants": ["interfaces remain stable"],
                        "expected_regression_risks": [],
                    },
                )
                freeze_round(config, current, 2)
                rejected_scores = {task_id: 0 for task_id in evaluated}
                candidate2_sha = adapter.runtime_behavior_hash(candidate2)
                rejected_runs = {
                    "r1": make_result_root(
                        root, "rejected-r1", candidate2_sha, rejected_scores
                    ),
                    "r2": make_result_root(
                        root, "rejected-r2", candidate2_sha, rejected_scores
                    ),
                }
                rejected, _release2, _next2 = validate_round(
                    config, current, 2, rejected_runs
                )
                self.assertEqual(rejected["decision"], "rollback")
                after_reject = load(
                    config["project"]["state_dir"] + "/campaign.json"
                )
                round3 = start_round(config, after_reject)
                self.assertEqual(
                    (round3["round"], round3["attempt"], round3["attempt_in_round"]),
                    (2, 3, 2),
                )
                self.assertEqual(round3["evidence_source"], "reused_after_rollback")
                self.assertNotEqual(round3["candidate_root"], str(candidate2))
                self.assertEqual(
                    (Path(round3["candidate_root"]) / "worker.py").read_text(),
                    "VALUE = 2\n",
                )
                after_round3 = load(
                    config["project"]["state_dir"] + "/campaign.json"
                )
                archive = abort_round(
                    config, after_round3, 3, "hypothesis changed"
                )
                self.assertTrue(Path(archive).is_dir())
                after_abort = load(
                    config["project"]["state_dir"] + "/campaign.json"
                )
                self.assertEqual(after_abort["attempts_evaluated"], 2)
                self.assertEqual(after_abort["optimization_round"], 2)
                self.assertEqual(after_abort["status"], "attempt_closed")
                command_test_open(
                    SimpleNamespace(
                        config=str(config_path), reason="operator_stop"
                    )
                )
                command_test_plan(SimpleNamespace(config=str(config_path)))
                test_scores = {f"t{i}": 1 for i in range(6, 9)}
                test_runs = {
                    "r1": make_result_root(
                        root, "test-r1", candidate_sha, test_scores
                    ),
                    "r2": make_result_root(
                        root, "test-r2", candidate_sha, test_scores
                    ),
                }
                command_test_ingest(
                    SimpleNamespace(
                        config=str(config_path),
                        run=[
                            f"{name}={path}" for name, path in test_runs.items()
                        ],
                    )
                )
                completed = load(
                    config["project"]["state_dir"] + "/campaign.json"
                )
                self.assertEqual(completed["status"], "completed")
                report = load(config["project"]["state_dir"] + "/final-report.json")
                self.assertEqual(report["test"]["tasks"], 3)
                self.assertEqual(report["test"]["mean_utility"], 1.0)


def _ledger(*decisions):
    """Ledger rows: each decision is (verdict, base harness hash)."""
    return {
        "attempts": [
            {
                "base_behavior_hash": base,
                "decision": {"decision": verdict, "causal_intervention_test": True},
            }
            for verdict, base in decisions
        ]
    }


class StoppingRuleTests(unittest.TestCase):
    def test_rollbacks_stay_in_the_round(self):
        ledger = _ledger(("promote", "h0"), ("rollback", "h1"), ("rollback", "h1"))
        status = stopping_status(ledger, "h1", round_cap=10, patience=5)
        self.assertFalse(status["stop"])
        self.assertEqual(status["current_round"], 2)
        self.assertEqual(status["attempts_in_current_round"], 2)
        self.assertEqual(status["gui_evaluated_attempts"], 3)

    def test_patience_counts_attempts_in_one_round(self):
        ledger = _ledger(("promote", "h0"), *[("rollback", "h1")] * 5)
        status = stopping_status(ledger, "h1", round_cap=10, patience=5)
        self.assertEqual(status["reasons"], ["patience"])
        self.assertEqual(status["completed_rounds"], 2)

    def test_round_cap_counts_promotions_not_attempts(self):
        rows = []
        for index in range(10):
            rows += [("rollback", f"h{index}")] * 4 + [("promote", f"h{index}")]
        status = stopping_status(_ledger(*rows[:-1]), "h9", round_cap=10, patience=5)
        self.assertFalse(status["stop"])
        self.assertEqual(status["gui_evaluated_attempts"], 49)
        status = stopping_status(_ledger(*rows), "h10", round_cap=10, patience=5)
        self.assertEqual(status["reasons"], ["round_cap"])


if __name__ == "__main__":
    unittest.main()

