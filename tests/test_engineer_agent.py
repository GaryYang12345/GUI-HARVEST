import json
import tempfile
import unittest
from pathlib import Path

from gui_harvest.roles.engineer_agent import EngineerWorkspace


class EngineerWorkspaceTests(unittest.TestCase):
    def test_api_workspace_enforces_read_write_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = root / "candidate"
            baseline = root / "baseline"
            round_root = root / "round"
            for path in (candidate, baseline, round_root):
                path.mkdir()
            (baseline / "worker.py").write_text("BASE = 1\n")
            (candidate / "worker.py").write_text("VALUE = 1\n")
            (round_root / "ENGINEER_BRIEF.md").write_text("brief\n")
            (round_root / "capability.json").write_text(
                json.dumps({"l1_commands": [["{python}", "-m", "compileall", "."]]})
            )
            workspace = EngineerWorkspace(candidate, baseline, round_root)
            self.assertEqual(workspace.read_file("worker.py"), "VALUE = 1")
            self.assertEqual(
                workspace.read_file("baseline/worker.py"), "BASE = 1"
            )
            workspace.write_file("worker.py", "VALUE = 2\n")
            workspace.write_file("round/proposal.pre.json", "{}\n")
            self.assertEqual((candidate / "worker.py").read_text(), "VALUE = 2\n")
            with self.assertRaises(PermissionError):
                workspace.write_file("baseline/worker.py", "bad")
            with self.assertRaises(PermissionError):
                workspace.write_file("round/ledger.json", "bad")
            with self.assertRaises(PermissionError):
                workspace.read_file("../outside")
            with self.assertRaises(ValueError):
                workspace.run_command(99)


if __name__ == "__main__":
    unittest.main()

