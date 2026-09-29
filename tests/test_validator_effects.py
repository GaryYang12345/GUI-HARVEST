import tempfile
import unittest
from pathlib import Path

from PIL import Image

from gui_harvest.evidence.render import NO_CHANGE
from gui_harvest.roles.validator_agent import _preceding_effects, _trigger


def _png(path, shade):
    Image.new("RGB", (320, 180), (shade, shade, shade)).save(path)


class PrecedingEffectTests(unittest.TestCase):
    def test_row_sees_the_change_left_by_the_previous_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _png(root / "step_0.png", 0)
            _png(root / "step_1_a.png", 0)  # action 1 changed nothing
            _png(root / "step_2_b.png", 200)  # action 2 changed the screen
            _png(root / "step_3_c.png", 200)
            rows = [
                {"step_num": 1, "screenshot_file": "step_1_a.png"},
                {"step_num": 2, "screenshot_file": "step_2_b.png"},
                {"step_num": 3, "screenshot_file": "step_3_c.png"},
            ]
            effects = _preceding_effects(root, rows)
        self.assertEqual(effects[1], (None, None))
        self.assertLess(effects[2][0], NO_CHANGE)
        self.assertTrue(_trigger(effects[2][0]))
        self.assertGreaterEqual(effects[3][0], NO_CHANGE)
        self.assertFalse(_trigger(effects[3][0]))


if __name__ == "__main__":
    unittest.main()
