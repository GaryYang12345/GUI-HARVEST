import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

AGENT_S = """\
class AgentS3:
    def __init__(self, engine_params, grounding_agent, platform="linux", **kwargs):
        self.grounding_agent = grounding_agent

    def reset(self, logger=None):
        pass

    def predict(self, instruction, observation):
        return {}, []
"""

GROUNDING = """\
class OSWorldACI:
    def __init__(self, env, platform, engine_params_for_generation,
                 engine_params_for_grounding, width=1920, height=1080):
        self.env = env
"""


def _tree(root, agent_source=AGENT_S):
    package = Path(root) / "gui_agents" / "s3" / "agents"
    package.mkdir(parents=True)
    for directory in (package.parents[1], package.parent, package):
        (directory / "__init__.py").write_text("")
    (package / "agent_s.py").write_text(agent_source)
    (package / "grounding.py").write_text(GROUNDING)


def _run(root):
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "gui_harvest.campaign.smoke", "--root", "."],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class L1SmokeTests(unittest.TestCase):
    def test_runner_compatible_tree_passes(self):
        with tempfile.TemporaryDirectory() as root:
            _tree(root)
            result = _run(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"smoke": "passed"', result.stdout)

    def test_broken_runner_interface_fails(self):
        with tempfile.TemporaryDirectory() as root:
            _tree(root, AGENT_S.replace("def predict(", "def predict_step("))
            result = _run(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("AgentS3.predict is missing", result.stdout)


if __name__ == "__main__":
    unittest.main()
