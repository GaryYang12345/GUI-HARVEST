import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from gui_harvest.config import TEMPLATE, load_config
from gui_harvest.campaign.splits import assign_splits, domain_quotas, load_tasks
from gui_harvest.campaign.rollouts import audit_runs

OSWORLD_361 = {
    "chrome": 46,
    "gimp": 26,
    "libreoffice_calc": 47,
    "libreoffice_impress": 47,
    "libreoffice_writer": 23,
    "multi_apps": 93,
    "os": 24,
    "thunderbird": 15,
    "vlc": 17,
    "vs_code": 23,
}
OSWORLD_361_SEARCH = {
    "chrome": 10,
    "gimp": 6,
    "libreoffice_calc": 11,
    "libreoffice_impress": 10,
    "libreoffice_writer": 5,
    "multi_apps": 21,
    "os": 5,
    "thunderbird": 3,
    "vlc": 4,
    "vs_code": 5,
}
SIZES = {"search": 80, "validation": 80, "test": 201}


def synthetic_361():
    return [
        {"task_id": f"{domain}-{index:03d}", "domain": domain}
        for domain, n in OSWORLD_361.items()
        for index in range(n)
    ]


def per_domain(suite):
    return {domain: len(ids) for domain, ids in suite.items()}


class ConfigAndSplitTests(unittest.TestCase):
    def test_defaults_match_official_protocol(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness = root / "harness"
            harness.mkdir()
            (harness / "HARNESS_CARD.md").write_text("# card\n")
            text = TEMPLATE.replace(
                'baseline_root = "../agent-s3"',
                'baseline_root = "harness"',
            )
            (root / "gui-harvest.toml").write_text(text)
            config = load_config(root / "gui-harvest.toml")
        self.assertEqual(config["protocol"]["rollouts_k"], 3)
        self.assertEqual(config["protocol"]["round_budget"], 10)
        self.assertEqual(config["protocol"]["rejection_patience"], 5)
        self.assertEqual(
            (
                config["splits"]["search"],
                config["splits"]["validation"],
                config["splits"]["test"],
            ),
            (80, 80, 201),
        )

    def test_domain_quotas_are_proportional_and_exact(self):
        self.assertEqual(domain_quotas(OSWORLD_361, 80), OSWORLD_361_SEARCH)
        self.assertEqual(sum(domain_quotas(OSWORLD_361, 80).values()), 80)

    def test_split_is_stratified_exact_and_deterministic(self):
        tasks = synthetic_361()
        first = assign_splits(tasks, SIZES, "seed-x")
        self.assertEqual(first, assign_splits(tasks, SIZES, "seed-x"))
        self.assertEqual(per_domain(first["search"]), OSWORLD_361_SEARCH)
        self.assertEqual(per_domain(first["validation"]), OSWORLD_361_SEARCH)
        self.assertEqual(
            per_domain(first["test"]),
            {d: OSWORLD_361[d] - 2 * OSWORLD_361_SEARCH[d] for d in OSWORLD_361},
        )
        seen = [
            task_id
            for suite in first.values()
            for ids in suite.values()
            for task_id in ids
        ]
        self.assertEqual(len(seen), 361)
        self.assertEqual(len(set(seen)), 361)

    def test_seed_changes_members_not_counts(self):
        tasks = synthetic_361()
        a = assign_splits(tasks, SIZES, "seed-a")
        b = assign_splits(tasks, SIZES, "seed-b")
        self.assertNotEqual(a["search"], b["search"])
        for name in SIZES:
            self.assertEqual(per_domain(a[name]), per_domain(b[name]))

    def test_input_order_does_not_change_split(self):
        tasks = synthetic_361()
        shuffled = list(reversed(tasks))
        self.assertEqual(
            assign_splits(tasks, SIZES, "x"),
            assign_splits(shuffled, SIZES, "x"),
        )

    @unittest.skipUnless(
        os.environ.get("GUI_HARVEST_BENCHMARK_ROOT"),
        "set GUI_HARVEST_BENCHMARK_ROOT to an OSWorld checkout",
    )
    def test_real_osworld_361_uses_directory_domains(self):
        root = Path(os.environ["GUI_HARVEST_BENCHMARK_ROOT"]) / "evaluation_examples"
        tasks = load_tasks(root / "examples", root / "test_nogdrive.json")
        self.assertEqual(dict(Counter(t["domain"] for t in tasks)), OSWORLD_361)
        suites = assign_splits(tasks, SIZES, "seed-x")
        self.assertEqual(per_domain(suites["search"]), OSWORLD_361_SEARCH)
        self.assertEqual(per_domain(suites["validation"]), OSWORLD_361_SEARCH)

    def test_rollout_audit_requests_missing_or_invalid_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "run_manifest.json").write_text("{}")
            task = root / "chrome" / "t1"
            task.mkdir(parents=True)
            (task / "result.txt").write_text("nan")
            (task / "traj.jsonl").write_text("{}\n")
            audit = audit_runs({"r1": str(root)}, ["t1", "t2"], 1)
        self.assertFalse(audit["complete"])
        self.assertIn("t1", audit["rerun"]["r1"])
        self.assertIn("t2", audit["rerun"]["r1"])

    def test_rollout_audit_rejects_missing_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task = root / "chrome" / "t1"
            task.mkdir(parents=True)
            (task / "result.txt").write_text("1")
            (task / "traj.jsonl").write_text("{}\n")
            (task / "step_1.png").write_bytes(b"png")
            audit = audit_runs({"r1": str(root)}, ["t1"], 1)
        self.assertFalse(audit["complete"])
        self.assertIn("__run__", audit["rerun"]["r1"])


if __name__ == "__main__":
    unittest.main()

