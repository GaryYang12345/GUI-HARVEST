"""Screen and trajectory rendering shared by the evidence bundle.

Everything here is arithmetic over a run's own screenshots and records:

  * screen files: screen 0 is the frame before any action; screen t is the
    frame after step t, so step t was decided on screen t-1;
  * Δscreen: the fraction of downscaled grayscale pixels that changed between
    consecutive screens, and the stretches where the screen stopped changing;
  * Δlocal: the same measure inside the window around a step's landing point,
    so a small control that reacted is not read as an unchanged screen;
  * landing points: the (x, y) of a step's pointing action, drawn as a red
    ring on the full frame the step was decided on and as a red cross on an
    original-resolution crop;
  * text: the code-agent report with each code step's status and stderr, and a
    fixed panel of counts per run.
"""

import base64
import collections
import functools
import io
import os
import re

from gui_harvest.evidence.trajectory import normalize_text

# ---------------------------------------------------------------- constants
# Full-frame width sent to the model. 1920 is downscaled by the providers
# anyway (Claude to 1568 on the long side, GPT to 1365); 1280 keeps menu text
# and dialog labels legible at ~2/3 scale and costs ~1.3k tokens a frame.
SHOT_MAX_WIDTH = 1280
# Original-resolution crop around a pointing action's landing point. What a
# full frame cannot show is whether a click landed on the control it named;
# 480x320 at 1:1 shows the control and its neighbours for ~250 tokens.
CROP_W, CROP_H = 480, 320
MAX_CROPS = 3
# Screen-change thresholds: the fraction of downscaled grayscale pixels whose
# intensity moved by more than DIFF_LEVEL.
BIG_CHANGE = 0.15
# Below NO_CHANGE a step counts as having left the screen unchanged. The
# Analyst and the Validator use the same value.
NO_CHANGE = 0.02
# Below NEGLIGIBLE_CHANGE nothing visible happened at all.
NEGLIGIBLE_CHANGE = 0.001
NO_CHANGE_RUN = 3
DIFF_LEVEL = 24
DIFF_SIZE = (96, 54)
# The landing-point window is compared at the same 5x reduction as the screen.
LOCAL_SIZE = (CROP_W // 5, CROP_H // 5)
# A step changed its landing-point window when at least this share moved.
LOCAL_CHANGE = 0.05
_STDOUT_CHARS = 300
_STDERR_CHARS = 500
_CODE_STEP_CHARS = 500

RUN_LABELS = "ABCDEFGH"

# ---------------------------------------------------------------- helpers
def _ending(traj):
    if traj.fatal_error:
        return "a fatal error"
    if not traj.steps:
        return "no steps were taken"
    if traj.steps[-1].is_done:
        return "the agent called agent.done()"
    if traj.steps[-1].is_fail:
        return "the agent called agent.fail()"
    if traj.budget_exhausted:
        return f"the step budget ({traj.max_steps}) ran out"
    return "the loop stopped without a terminal action"


def _cap(text, n):
    text = normalize_text(text)
    return text if len(text) <= n else text[:n] + "…"


def _cap_raw(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def _cap_machine(text, n):
    """Clip machine-generated text without hiding its terminal result."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    marker = f"\n… [TRUNCATED {len(text) - n} CHARS; HEAD+TAIL] …\n"
    room = max(2, n - len(marker))
    head = (room + 1) // 2
    tail = room - head
    return text[:head] + marker + text[-tail:]


def step_no(s):
    """Harness-aligned step number: action 1..n, matching step_N.png after it."""
    return s.idx + 1


def screen_file(traj, t):
    """The file holding screen t, where action i runs between screens i and i+1."""
    if t == 0:
        return "step_0.png"
    return traj.steps[t - 1].screenshot_file


def screen_path(traj, t):
    name = screen_file(traj, t)
    if not name:
        return None
    path = os.path.join(traj.task_dir, name)
    if os.path.exists(path):
        return path
    # step_0 may carry a timestamp suffix in some runners.
    if t == 0:
        for f in sorted(os.listdir(traj.task_dir)):
            if f.startswith("step_0") and f.endswith(".png"):
                return os.path.join(traj.task_dir, f)
    return None


# ------------------------------------------------------- screen arithmetic
@functools.lru_cache(maxsize=4096)
def _small_gray(path):
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("L").resize(DIFF_SIZE)
        return bytes(im.tobytes())


def screen_change(path_a, path_b):
    """Fraction of the (downscaled, grayscale) picture that changed. None if a
    screen is missing."""
    if not path_a or not path_b:
        return None
    try:
        a, b = _small_gray(path_a), _small_gray(path_b)
    except OSError:
        return None
    n = min(len(a), len(b))
    if n == 0:
        return None
    moved = sum(1 for i in range(n) if abs(a[i] - b[i]) > DIFF_LEVEL)
    return moved / n


def change_fractions(traj):
    """cf[i] for i in 1..n: how much the screen changed when step i ran.
    Index 0 is None (no step ran before screen 0)."""
    out = [None]
    prev = screen_path(traj, 0)
    for t in range(1, len(traj.steps) + 1):
        cur = screen_path(traj, t)
        out.append(screen_change(prev, cur))
        prev = cur if cur else prev
    return out


def no_change_runs(cf):
    """[(first_step, last_step)] for stretches of >= NO_CHANGE_RUN steps with
    cf < NO_CHANGE."""
    runs, start = [], None
    for t in range(1, len(cf)):
        v = cf[t]
        if v is not None and v < NO_CHANGE:
            start = t if start is None else start
        else:
            if start is not None and t - start >= NO_CHANGE_RUN:
                runs.append((start, t - 1))
            start = None
    if start is not None and len(cf) - start >= NO_CHANGE_RUN:
        runs.append((start, len(cf) - 1))
    return runs


_COORD_RE = re.compile(
    r"pyautogui\.(?:click|rightClick|doubleClick|moveTo|dragTo|middleClick|tripleClick)"
    r"\(\s*(?:x\s*=\s*)?(-?\d+(?:\.\d+)?)\s*,\s*(?:y\s*=\s*)?(-?\d+(?:\.\d+)?)"
)


def landing_point(step):
    """(x, y) of the first pointing call in the executed code, or None."""
    m = _COORD_RE.search(step.exec_code or "") or _COORD_RE.search(step.action or "")
    if not m:
        return None
    return int(float(m.group(1))), int(float(m.group(2)))


def _window_bytes(image, x, y, w, h):
    """Grayscale bytes of the crop window around (x, y), reduced to LOCAL_SIZE."""
    w = min(w, image.width)
    h = min(h, image.height)
    if w <= 0 or h <= 0:
        return None
    left = max(0, min(image.width - w, x - w // 2))
    top = max(0, min(image.height - h, y - h // 2))
    box = (left, top, left + w, top + h)
    return image.convert("L").crop(box).resize(LOCAL_SIZE).tobytes()


def local_change(path_a, path_b, point, w=CROP_W, h=CROP_H):
    """Fraction of the region around `point` that changed between two screens.

    The region is the same original-resolution window the crop shows. A menu
    or dropdown changes a large share of this window while changing well under
    one percent of the whole screen. A few replaced characters stay under one
    percent of the window; the crop is what shows them.
    """
    if not (path_a and path_b and point):
        return None
    from PIL import Image

    try:
        with Image.open(path_a) as a, Image.open(path_b) as b:
            x, y = point
            pa = _window_bytes(a, x, y, w, h)
            pb = _window_bytes(b, x, y, w, h)
            if pa is None or pb is None or len(pa) != len(pb):
                return None
    except OSError:
        return None
    moved = sum(1 for i in range(len(pa)) if abs(pa[i] - pb[i]) > DIFF_LEVEL)
    return moved / len(pa)


def local_change_fractions(traj):
    """lcf[i] for i in 1..n: change inside the window around step i's landing
    point. None when step i did not point or a screen is missing."""
    out = [None]
    for t in range(1, len(traj.steps) + 1):
        out.append(
            local_change(
                screen_path(traj, t - 1),
                screen_path(traj, t),
                landing_point(traj.steps[t - 1]),
            )
        )
    return out


def select_crops(traj, cf, lcf=None, limit=MAX_CROPS):
    """[(step, (x, y))] for landing-point crops, in this order:

    the first pointing step of each stretch where the screen stopped changing;
    then any pointing step after which the full screen barely changed while its
    landing-point window did.
    """
    out = []
    for first, last in no_change_runs(cf):
        for t in range(first, last + 1):
            pt = landing_point(traj.steps[t - 1])
            if pt:
                out.append((t, pt))
                break
        if len(out) >= limit:
            return out
    chosen = {t for t, _ in out}
    for t in range(1, len(traj.steps) + 1):
        if len(out) >= limit:
            break
        if t in chosen or not lcf or lcf[t] is None or cf[t] is None:
            continue
        if cf[t] < NO_CHANGE and lcf[t] >= LOCAL_CHANGE:
            out.append((t, landing_point(traj.steps[t - 1])))
    return out


# ------------------------------------------------------------- encoding
# Landing marker drawn on a full frame: a ring large enough to survive the
# downscale, hollow so the control under it stays readable.
MARK_RADIUS = 22
MARK_WIDTH = 4


def encode_shot(path, max_width=SHOT_MAX_WIDTH, mark=None):
    """Downscaled full frame. `mark` is an (x, y) in original pixels; when
    given, a red ring is drawn there before downscaling."""
    from PIL import Image, ImageDraw

    with Image.open(path) as im:
        im = im.convert("RGB")
        if mark:
            x, y = mark
            d = ImageDraw.Draw(im)
            d.ellipse(
                [x - MARK_RADIUS, y - MARK_RADIUS, x + MARK_RADIUS, y + MARK_RADIUS],
                outline=(255, 0, 0),
                width=MARK_WIDTH,
            )
            d.line([(x - 6, y), (x + 6, y)], fill=(255, 0, 0), width=2)
            d.line([(x, y - 6), (x, y + 6)], fill=(255, 0, 0), width=2)
        if im.width > max_width:
            im = im.resize((max_width, int(im.height * max_width / im.width)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def encode_crop(path, point, w=CROP_W, h=CROP_H):
    """Original-resolution crop centred on point, clamped to the frame, with a
    small cross drawn at the landing point so the reader can find it."""
    from PIL import Image, ImageDraw

    x, y = point
    with Image.open(path) as im:
        im = im.convert("RGB")
        left = max(0, min(im.width - w, x - w // 2))
        top = max(0, min(im.height - h, y - h // 2))
        crop = im.crop((left, top, left + w, top + h))
        d = ImageDraw.Draw(crop)
        cx, cy = x - left, y - top
        d.line([(cx - 12, cy), (cx + 12, cy)], fill=(255, 0, 0), width=2)
        d.line([(cx, cy - 12), (cx, cy + 12)], fill=(255, 0, 0), width=2)
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _data_url(b64):
    return f"data:image/jpeg;base64,{b64}"


def next_landing(traj, screen):
    """Landing point of the step decided on `screen` (step screen+1), or None."""
    if 0 <= screen < len(traj.steps):
        return landing_point(traj.steps[screen])
    return None


def load_screens(traj, screens, mark_landing=True):
    """[(screen, data_url, mark)] where mark is the (x, y) drawn on the frame:
    the point the next step's pointing action landed on, or None."""
    out = []
    for t in screens:
        path = screen_path(traj, t)
        if not path:
            continue
        mark = next_landing(traj, t) if mark_landing else None
        try:
            out.append((t, _data_url(encode_shot(path, mark=mark)), mark))
        except OSError:
            continue
    return out


def load_crops(traj, crops):
    """crops: [(step, (x, y))]. The crop is taken from the screen the step was
    decided on (screen step-1), which is where the control the click aimed at
    was shown, and from the screen after (step), so the effect is visible."""
    out = []
    for t, pt in crops:
        for which, screen in (("before", t - 1), ("after", t)):
            path = screen_path(traj, screen)
            if not path:
                continue
            try:
                out.append((t, which, pt, _data_url(encode_crop(path, pt))))
            except OSError:
                continue
    return out


# --------------------------------------------------------- text rendering
def render_code_agent(step):
    """The code-agent report with structure kept: task, ending, then one block
    per code step with its code and, where the harness recorded it, the
    machine's status / return code / stderr. stdout tails are cut."""
    rec = step.code_agent_record
    if not rec:
        text = step.code_agent_output
        return f"     code agent returned: {_cap(text, 1500)}" if text else ""
    lines = [
        f"     code agent was given: {_cap(str(rec.get('task_instruction', '')), 400)}",
        f"     code agent ended: {rec.get('completion_reason')} after "
        f"{rec.get('steps_executed')} of {rec.get('budget')} code steps",
    ]
    # A code session that retries the same command gets the same error back
    # each time; the repeat is the fact worth keeping, the text is not.
    prev = {"code": None, "stderr": None, "stdout": None, "step": None}
    for h in rec.get("execution_history") or []:
        k = h.get("step")
        code = h.get("code") or ""
        if not code:
            action = h.get("action") or ""
            m = re.search(r"```(?:\w+)?\n(.*?)```", action, re.S)
            code = m.group(1) if m else action
        ctype = h.get("code_type") or ("bash" if "```bash" in (h.get("action") or "") else "")
        if code and normalize_text(code) == prev["code"]:
            lines.append(f"       code step {k} ({ctype or 'code'}): same code as code step {prev['step']}")
        else:
            lines.append(f"       code step {k} ({ctype or 'code'}): {_cap_machine(code, _CODE_STEP_CHARS)}")
        ex = h.get("execution")
        if isinstance(ex, dict):
            status = ex.get("status")
            rc = ex.get("return_code", ex.get("returncode"))
            lines.append(f"         machine: status={status} return_code={rc}")
            for key, alt, cap in (("stderr", "error", _STDERR_CHARS), ("stdout", "output", _STDOUT_CHARS)):
                text = str(ex.get(key) or ex.get(alt) or "")
                if not text:
                    prev[key] = None
                    continue
                norm = normalize_text(text)
                if norm == prev[key]:
                    lines.append(f"         {key}: same as code step {prev['step']}")
                else:
                    lines.append(f"         {key}: {_cap_machine(text, cap)}")
                prev[key] = norm
        if h.get("terminal_signal"):
            lines.append(f"         code model signalled: {h['terminal_signal']}")
        prev["code"], prev["step"] = normalize_text(code), k
    history = rec.get("execution_history") or []
    machine_recorded = any(isinstance(h.get("execution"), dict) for h in history)
    if rec.get("summary") and not machine_recorded:
        # Without per-step machine receipts the summary is the only account of
        # what the code did, and it is the code model's own words.
        lines.append(
            f"     code agent's own summary (the code model's words, not a machine "
            f"record): {_cap_machine(str(rec['summary']), 1500)}"
        )
    return "\n".join(lines)


def _pct(v):
    if v is None:
        return "n/a"
    return f"{v * 100:.1f}%" if v < 0.1 else f"{int(round(v * 100))}%"


def is_analysis_target(reward):
    """A run is analysed when it scored nothing. Runs that scored are in the
    bundle for comparison and may not be the subject of a finding."""
    return reward is not None and reward == 0.0


def render_measurements(traj, cf, lcf=None):
    """Fixed panel of counts, the same for every run. Arithmetic only."""
    steps = traj.steps
    n = len(steps)
    prims = collections.Counter(s.primitive or "(unparsed)" for s in steps)
    longest_run, run = 0, 0
    for i, s in enumerate(steps):
        run = run + 1 if i and s.action_norm == steps[i - 1].action_norm else 1
        longest_run = max(longest_run, run)
    small = sum(1 for t in range(1, n + 1) if cf[t] is not None and cf[t] < NO_CHANGE)
    big = sum(1 for t in range(1, n + 1) if cf[t] is not None and cf[t] >= BIG_CHANGE)
    local_only = 0
    if lcf:
        local_only = sum(
            1
            for t in range(1, n + 1)
            if cf[t] is not None
            and lcf[t] is not None
            and cf[t] < NO_CHANGE
            and lcf[t] >= LOCAL_CHANGE
        )
    stretches = no_change_runs(cf)
    return "\n".join(
        [
            f"- steps executed: {n} of a {traj.max_steps} budget",
            f"- distinct action strings: {len({s.action_norm for s in steps})}",
            f"- longest run of consecutive identical actions: {longest_run}",
            f"- steps whose only effect was a sleep: {sum(1 for s in steps if s.is_idle)}",
            f"- steps after which under {int(NO_CHANGE*100)}% of the screen changed: {small}; "
            f"over {int(BIG_CHANGE*100)}%: {big}",
            f"- pointing steps where the screen stayed under {int(NO_CHANGE*100)}% "
            f"while at least {int(LOCAL_CHANGE*100)}% of the landing-point window moved: {local_only}",
            f"- stretches of {NO_CHANGE_RUN}+ consecutive near-unchanged screens: "
            + (", ".join(f"steps {a}-{b}" for a, b in stretches) if stretches else "none"),
            "- primitives used: " + ", ".join(f"{k}×{v}" for k, v in prims.most_common()),
            f"- steps carrying a code-agent report: "
            f"{sum(1 for s in steps if s.code_agent_record or s.code_agent_output)}",
        ]
    )


