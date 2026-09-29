"""Landing-point change is a property of the screenshots, not of any harness."""

import os
import tempfile
import unittest

from PIL import Image, ImageDraw

from gui_harvest.evidence.render import (
    LOCAL_CHANGE,
    NO_CHANGE,
    _pct,
    local_change,
    local_change_fractions,
    select_crops,
)


class _Step:
    def __init__(self, code, shot):
        self.idx = 0
        self.exec_code = code
        self.action = ""
        self.screenshot_file = shot


class _Traj:
    def __init__(self, task_dir, steps):
        self.task_dir = task_dir
        self.steps = steps
        for i, step in enumerate(steps):
            step.idx = i


def _frame(path, paint=None):
    im = Image.new("RGB", (960, 540), (240, 240, 240))
    if paint:
        paint(ImageDraw.Draw(im))
    im.save(path)


class LocalChangeTest(unittest.TestCase):
    def test_pct_keeps_a_tenth_below_ten_percent(self):
        self.assertEqual(_pct(0.0017), "0.2%")
        self.assertEqual(_pct(0.15), "15%")
        self.assertEqual(_pct(None), "n/a")

    def test_dropdown_moves_the_window_and_not_the_screen(self):
        with tempfile.TemporaryDirectory() as d:
            before = os.path.join(d, "before.png")
            after = os.path.join(d, "after.png")
            _frame(before)
            _frame(after, lambda draw: draw.rectangle((400, 200, 620, 420), fill=(30, 90, 160)))
            point = (480, 180)
            local = local_change(before, after, point)
            self.assertIsNotNone(local)
            self.assertGreaterEqual(local, LOCAL_CHANGE)

            _frame(os.path.join(d, "step_0.png"))
            _frame(
                os.path.join(d, "step_1.png"),
                lambda draw: draw.rectangle((400, 200, 620, 420), fill=(30, 90, 160)),
            )
            step = _Step("pyautogui.click(480, 180)", "step_1.png")
            traj = _Traj(d, [step])
            lcf = local_change_fractions(traj)
            self.assertGreaterEqual(lcf[1], LOCAL_CHANGE)
            crops = select_crops(traj, [None, 0.001], lcf)
            self.assertEqual(crops, [(1, (480, 180))])

    def test_unchanged_window_stays_under_the_screen_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            before = os.path.join(d, "before.png")
            after = os.path.join(d, "after.png")
            _frame(before)
            _frame(after)
            local = local_change(before, after, (100, 100))
            self.assertLess(local, NO_CHANGE)

    def test_missing_point_is_not_a_number(self):
        self.assertIsNone(local_change("a", "b", None))


if __name__ == "__main__":
    unittest.main()
