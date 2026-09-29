"""Evidence Analyst: one task-round bundle -> locality-verified findings.

  * Inputs: the framework's own trajectory records (rendered by bundle.py), a
    task card (domain, related apps, feasibility, setup, published hint), and
    a HARNESS CARD: a prose description of the framework that
    produced the rollout, maintained alongside the framework and passed in as a
    file. The analyst never sees framework source and never depends on any
    particular telemetry field.
  * The question is anchored: WHERE the failed run parted from a route that
    reached the goal (contrast, when a run of the same task scored) or from
    progress toward the end state the instruction requires (decisive step, always), and WHAT
    the run did there.
  * No class library. Labels are named per round; the clusterer merges them.
  * Findings are verified arithmetically (run scored 0, steps in range, one
    verbatim quote from that run's block, reference run scored) and dropped
    otherwise; drops are recorded, not silently lost. Claims the screen
    arithmetic can refute get one challenge turn.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))

from gui_harvest.evidence.outcome_categories import (
    ACTIONS,
    NOTICED,
    OUTCOMES,
    derive_outcome_category,
    normalize_signature,
)
from gui_harvest.evidence.bundle import (  # noqa: E402
    _pct,
    build_bundle_v2,
    parse_requests,
    shots_for_requests,
    step_no,
)
from gui_harvest.evidence.render import (  # noqa: E402
    LOCAL_CHANGE,
    NEGLIGIBLE_CHANGE,
    NO_CHANGE,
)
from gui_harvest.campaign.provenance import (  # noqa: E402
    create_manifest,
    update_manifest,
    write_dataset_manifest,
    write_manifest,
)

MAX_OUTPUT_TOKENS = 8000
REQUEST_CAP = 6
REQUEST_ROUNDS = 2

FALLBACK_HARNESS_TEXT = """\
(no harness card supplied) The agent is Agent-S3 style: each step a planner model sees the
current screenshot and its own history and emits ONE primitive, executed on an Ubuntu VM;
element descriptions are resolved to coordinates by a separate grounding model, so a click can
land off the control it names. Δscreen is the share of the screenshot that changed; when a step
pointed, Δlocal is the same share inside the window around that landing point.
"""


def load_harness_card(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip() + "\n"
    return FALLBACK_HARNESS_TEXT

PROMPT = """\
You are given every run of ONE task of a GUI agent in the current round -- {n_runs} runs, each
with the benchmark's score. At least one scored 0.

YOUR QUESTION. For each run that scored 0: at which step did it stop getting nearer to the
end state the task instruction requires, and what exactly did it do there. Answer in observations -- what the run
believed, did, what the record shows, whether the run noticed -- not in causes. What a run did
stays true whatever the harness becomes; why the harness allowed it is for a later stage that
holds the source code and every other task showing the same pattern. "The model should have
reasoned better" cannot be acted on by anyone downstream and is not an answer.

WHAT YOU ARE GIVEN. (1) The HARNESS CARD: a description of the framework that produced these
runs -- its loop, primitives and their exact semantics, and what each recorded field means. Read
it first; it is the only account of the harness you get. (2) The TASK CARD: what the task states
-- domain, related apps, whether it is feasible, the setup before the run, and any published
hint. Read the required end state from the instruction; the score says whether a run reached it.
(3) The runs: for every step the primitive, Δscreen (share of the screenshot that changed after
the step) and, when the step pointed, Δlocal (the same share inside the window around the
landing point), the planner's own text, the reflection, the code-agent report, and any other
field the framework wrote. Both deltas are computed from the framework's screenshots. Nothing
in the record is a probe unless the harness card says so.

HOW A RUN IS SCORED, IN GENERAL. After the run ends, the benchmark reads the machine's state, not
the screen: files on disk, application configuration and settings, what a program or the system
reports, the page or document an application has open. A change that is visible on screen but was
not saved, applied, or confirmed into that state does not count, and neither does a correct
result written to a different file or location than the task names. An infeasible task counts
only when the run ends with fail().

PROCEDURE (do it in this order; the fields of your answer follow it)
1. Read the scores and the OVERVIEW. If some run scored, it is the REFERENCE: a route that
   reached the goal under this same harness. If none scored, there is no reference.
2. With a reference: walk the failed run and the reference side by side and find the FIRST
   step where the failed run parts from the reference in a way that matters for the outcome
   (not wording differences, not a different order of equivalent steps). Record it as
   `contrast`, naming the scored reference run, the step, and what that run did. The pipeline
   derives whether the two records used the same primitive; do not classify the contrast.
   What both runs recorded alike cannot be what set them apart. Look on to the first step
   where the records or the actions differ.
   Without a reference, or when the divergence is not where the run was lost, find the
   DECISIVE STEP instead: the EARLIEST step that introduced a new error which was not fully
   repaired later and which entered the final failure chain. An earlier mistake that was fully
   corrected, with no residual state, is not the root cause. It remains causal only when its
   residue reaches the final state, or when the spent steps create `budget_debt` and the run
   later exhausts its budget while otherwise making progress. `decisive_step` is always
   required; it equals the divergence step when the divergence is where the run was lost.
   If the task card says the task is INFEASIBLE, the only correct ending is fail(): the decisive
   step is the step at which the run had seen what makes the task impossible and did something
   other than fail() (kept working, or called done()).
3. At the decisive step write TWO OBSERVATIONS, each grounded in the evidence:
     did     -- what the run took the state or target to be (quote its intent or own check) and
                the action(s) it issued there and in the steps that repeat it;
     machine -- what the record shows, as opposed to what the run claims: Δscreen, Δlocal, what the
                screens show, any recorded field the harness card explains, the code agent's
                report, and how that compares with what the instruction asks for. Nothing inferred.
   Whether the run registered the outcome is the `noticed` column of the signature (step 5).
4. A limit is a cause only when what it limited was producing. Before naming the step budget
   or the code budget, state what the run would have done with more of it and hold that against
   what it was actually doing as it approached the limit. If the closing steps were not getting
   nearer, the limit is not why the run failed. If the closing steps WERE getting nearer, the
   run was lost earlier -- where it spent steps the reference did not need (a longer route, a
   detour, a repeated action): put `decisive_step` at the start of that spend and label what the
   run did there. "BUDGET_EXHAUSTED" is never a label; it is an ending, and every run that ends
   that way is already marked so.
5. Fill the SIGNATURE of the decisive step from fixed vocabularies, so that findings across
   tasks can be laid side by side without reading prose:
     outcome -- what the machine recorded it produced:
                no_change (Δscreen≈0 and, where a landing point was recorded, Δlocal≈0) |
                unintended_dialog (a dialog the intent did not name appeared) |
                wrong_window (the keys/clicks went to a window other than the one named) |
                landed_off_target (the pointer hit a different control in the right window) |
                error_reported (an error the record shows: app error message, code error, a refusal by the harness) |
                changed_wrong_content (the state changed, but not to what the task needs) |
                changed_not_committed (the right change is on screen but not saved / not applied to the file or setting the task names) |
                changed_as_intended (it did what it meant; the choice itself was the problem) |
                exogenous_block (something the action does not explain intervened: captcha,
                                 crash, recovery dialog, page never loaded) |
                unknown
     noticed -- what the next step shows: changed_approach | retried_same | continued_unaware |
                declared_done | run_ended
   The outcome is read from Δscreen, Δlocal, the screens and the recorded fields, not from the
   run's own claim. Δlocal is the share of the same 480×320 window as the crop. Several percent
   is a menu or dialog. Under one percent can still be a few characters or a small icon; settle
   that from the crop, and do not treat it as no_change. A run that says "successful" over a
   step where the screen, the landing window, and the crop are all unchanged has outcome no_change.
6. Write `mechanism`: one sentence in the form <primitive on a kind of target/state> ->
   <recorded outcome> -> <what the run did next>. Replace task nouns, application names, file
   names and literal values by their roles. Then write `counterfactual`: the smallest different
   behavior at the decisive state that the evidence supports. Prefer the scored reference run;
   for a never-positive task use null unless the task card and record make the alternative
   unambiguous. A plausible guess is worse than null. One finding per failed run per mechanism;
   do not report a symptom that exists only because of something already reported.

SCREENS. The text carries Δscreen, and Δlocal wherever the step pointed, plus the record for every
step. Attached are a few frames per failed run chosen by rule (first, last, first frame of each
stretch where the screen stopped changing, largest jumps) and only the first and last frame of a
reference run. On each attached frame a RED RING marks where the NEXT step's pointing action
landed (the label names that step and the pixel); a frame with no ring means the next step did
not point. An original-resolution crop before and after is also attached, with a red cross at the
landing point, for the first pointing step of each stretch where the screen stopped changing, and
for a pointing step whose whole screen barely changed while its landing-point window did. Read
the ring, the crop, and Δlocal together. The pointer landed OFF the control the description named
(outcome `landed_off_target`); it landed ON that control and neither the window nor the screen
changed (outcome `no_change`); or the window changed while Δscreen stayed near zero (a menu, a
dialog, or edited text is a real change). Do not write `no_change` for a click whose ring sits
on a different control, or whose crop or Δlocal shows the landing window changed. Ask for more in
`screens_wanted` ONLY when the text is contradictory or silent on something you need to settle:
the action reads right but both deltas say it did not land (see what was on screen); the same
description was clicked three or more times (crop to see if the control is there, small, or
covered); you need a value or label visible on screen; you need the reference's and the failed
run's frame before the contrast step side by side. Each item is {{"run": "A", "step": N,
"kind": "before" | "after" | "crop"}} -- "before" is the frame the step was decided on, "after"
the frame it produced, "crop" an original-resolution crop around the click point. At most
{cap} per request turn; you may request in at most {rounds} turns; the turn after your last
request must carry the findings. Answering without requests is fine and not penalised.

VERIFICATION. `quotes` must be copied verbatim. At least one quote proving what the failed run
DID must come from that run's decisive-step block; a quote found only elsewhere in the run does
not support the attribution. Additional reference quotes may come from the task card, an earlier
step, or a scored reference run. Do not quote text that appears only after the blamed step as
evidence that the earlier decision was wrong. Steps must exist in that run.
`contrast.reference_run` must be a run that scored. Only runs that scored 0 may be the `run`
of a finding.

============================ HARNESS CARD ===========================
{harness}
=========================== EVIDENCE: THE RUNS ======================
{evidence}
=====================================================================

Answer with ONE JSON object and nothing else:
{{
  "screens_wanted": [ {{"run": "C", "step": 7, "kind": "before"}} ... ] or [],
  "findings": [
    {{
      "run": "C",
      "decisive_step": N,
      "contrast": {{"reference_run": "A", "step": N,
                    "reference_did": "one clause"}} or null,
      "signature": {{"outcome": "…", "noticed": "…"}},
      "did": "…",
      "machine": "…",
      "mechanism": "one abstract, observable mechanism sentence",
      "counterfactual": "smallest evidence-backed replacement behavior" or null,
      "quotes": ["VERBATIM from this run's block"]
    }}
  ] or null
}}
`findings` may be null only in a turn that requests screens. Your final turn must give findings
(an empty list is allowed only if every failed run is an infrastructure failure, and then say so
in a top-level "note").
"""


# ------------------------------------------------------------ helpers
def _canon(text):
    t = str(text or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("'", '"')
    return re.sub(r"\s+", " ", t).strip().lower()


def locate_quote(quote, bundle):
    """Exact canonical substring matches with explicit source/rendered locality."""
    cq = _canon(quote).rstrip("…").strip()
    if len(cq) < 8:
        return []
    found = []
    for region in bundle.get("evidence_regions") or []:
        hay = _canon(region.get("text"))
        if cq in hay:
            found.append({
                "run": region.get("run"),
                "step": region.get("step"),
                "region": region.get("region"),
                "layer": region.get("layer"),
                "shown_to_analyst": region.get("layer") == "rendered",
                "match": "canonical_exact",
                "span": [hay.index(cq), hay.index(cq) + len(cq)],
            })
    return found


def classify_analysis_status(findings, findings_raw, note):
    if findings:
        return "ok", None
    if isinstance(findings_raw, list) and not findings_raw and str(note or "").strip():
        return "no_attributable_failure", str(note)
    return "dropout", "response did not yield any locality-verified finding"


def prepare_resume(path, retry_statuses=()):
    """Return completed task ids; atomically remove rows explicitly retried."""
    done = set()
    if not os.path.exists(path):
        return done
    retry_statuses = set(retry_statuses)
    retained = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            if (row.get("analysis_status") or "ok") in retry_statuses:
                continue
            retained.append(row)
            done.add(row["task_id"])
    if retry_statuses:
        tmp = path + ".resume.tmp"
        with open(tmp, "w", encoding="utf-8") as target:
            for row in retained:
                target.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return done


def _json_from(text):
    decoder = json.JSONDecoder()
    objects = []
    text = str(text or "")
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        cursor = start + max(end, 1)
    return objects[-1] if objects else None


def shot_label(label, t, n_steps, mark=None):
    if t == 0:
        head = f"RUN {label} -- screen 0 (before any action)"
    else:
        tail = " (the last screen; what the evaluator scored)" if t == n_steps else ""
        head = f"RUN {label} -- screen after step {t}{tail}"
    if mark and t < n_steps:
        head += f"; red ring = where step {t + 1}'s pointing action landed {mark}"
    return head


def opening_media(bundle):
    content = []
    for r in bundle["runs"]:
        for shot in r["shots"]:
            t, url = shot[0], shot[1]
            mark = shot[2] if len(shot) > 2 else None
            content.append({"type": "text", "text": shot_label(r["label"], t, r["n_steps"], mark)})
            content.append({"type": "image_url", "image_url": {"url": url}})
        for t, which, pt, url in r.get("crops") or []:
            content.append({
                "type": "text",
                "text": (
                    f"RUN {r['label']} -- crop {which} step {t} at original resolution "
                    f"around the landing point {pt} (red cross)"
                ),
            })
            content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def followup_message(bundle, media, requested, turns_left):
    content = []
    if not media:
        content.append({"type": "text", "text": "None of the requested frames exist (bad run/step or no click point)."})
    for label, kind, t, pt, url in media:
        n = bundle["runs_by_label"][label]["n_steps"]
        if kind == "screen":
            text = shot_label(label, t, n, pt)
        else:
            which = kind.split("-", 1)[1]
            text = f"RUN {label} -- crop {which} step {t} around click point {pt} (red cross)"
        content.append({"type": "text", "text": text})
        content.append({"type": "image_url", "image_url": {"url": url}})
    if turns_left > 0:
        tail = (
            f"Requested frames attached. You may request once more ({turns_left} turn left) "
            "or answer now with the same JSON shape (findings required if not requesting)."
        )
    else:
        tail = "Requested frames attached. This is the final turn: answer with findings now; `screens_wanted` is ignored."
    content.append({"type": "text", "text": tail})
    return {"role": "user", "content": content}


def ask(client, model, messages, temperature=0.0):
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=model, temperature=temperature, max_tokens=MAX_OUTPUT_TOKENS, messages=messages
            )
            ch = resp.choices[0]
            usage = getattr(resp, "usage", None)
            text = ch.message.content or ""
            fin = getattr(ch, "finish_reason", None)
            # relay hiccups: an empty body, or a preface with no JSON object, are retried like errors
            if not text.strip() or ("{" not in text and fin != "length"):
                print(f"    ! empty/prefatory response (finish={fin}), retry")
                time.sleep(5 * (attempt + 1))
                continue
            return text, (usage.prompt_tokens if usage else None), (usage.completion_tokens if usage else None), fin, attempt
        except Exception as e:  # noqa: BLE001
            wait = 5 * (attempt + 1)
            print(f"    ! api error ({e.__class__.__name__}: {str(e)[:120]}), retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError("api failed 4 times")


# ------------------------------------------------------------ verify
SIGNATURE = {"action": ACTIONS, "outcome": OUTCOMES, "noticed": NOTICED}


def _step(r, t):
    for s in r["trajectory"].steps:
        if step_no(s) == t:
            return s
    return None


def _prim(s):
    m = re.search(r"(?:agent\.)?(\w+)\(", s.plan_code_norm or "")
    return m.group(1) if m else ""


def _derived_action(r, t):
    s = _step(r, t)
    if not s:
        return ""
    return normalize_signature({"action": _prim(s)}).get("action") or ""


def verify_finding(f, bundle):
    """Verify a finding and attach quote locality under audit metadata."""
    reasons = []
    run = str(f.get("run") or "").strip().upper()
    r = bundle["runs_by_label"].get(run)
    if not r:
        return False, ["run label unknown"]
    if not r["analysis_target"]:
        return False, ["run scored; cannot be the subject"]
    n = r["n_steps"]
    ds = f.get("decisive_step")
    if not isinstance(ds, int) or not (1 <= ds <= n):
        reasons.append("decisive_step out of range")
    mechanism = str(f.get("mechanism") or "").strip()
    if not mechanism:
        reasons.append("mechanism empty")
    kept, struck, quote_audit = [], [], []
    decisive_quote = False
    for q in f.get("quotes") or []:
        locations = locate_quote(q, bundle)
        allowed = []
        for loc in locations:
            # The analyst only received the rendered bundle. Raw-only matches are
            # useful provenance, but cannot support a claim the model could not
            # have copied from its actual input.
            if not loc["shown_to_analyst"]:
                continue
            if loc["region"] in ("task_card", "instruction"):
                allowed.append(loc)
                continue
            if loc["run"] == run:
                if loc["step"] == ds or (
                    isinstance(ds, int)
                    and isinstance(loc["step"], int)
                    and loc["step"] < ds
                ):
                    allowed.append(loc)
                continue
            other = bundle["runs_by_label"].get(loc["run"])
            if other and not other["analysis_target"]:
                allowed.append(loc)
        if allowed:
            kept.append(q)
            quote_audit.append({
                "quote": q,
                "locations": locations,
                "accepted_locations": allowed,
            })
            decisive_quote = decisive_quote or any(
                loc["run"] == run and loc["step"] == ds and loc["region"] == "step"
                for loc in allowed
            )
        else:
            struck.append(q)
            quote_audit.append({
                "quote": q,
                "locations": locations,
                "accepted_locations": [],
                "rejected": (
                    "not shown to analyst, only after decisive_step, or from "
                    "a non-reference run"
                ),
            })
    if not decisive_quote:
        reasons.append("no decisive-step quote from subject run")
    f["quotes"] = kept
    f.setdefault("audit", {})["quote_locality"] = quote_audit
    if struck:
        f["quotes_struck"] = struck
    contrast = f.get("contrast")
    if contrast is None and isinstance(f.get("divergence"), dict):
        # Tolerate a stale schema response, but normalize it immediately.
        old = f["divergence"]
        contrast = {
            "reference_run": old.get("reference_run"),
            "step": old.get("step"),
            "reference_did": old.get("reference_did"),
        }
        f["contrast"] = contrast
    if contrast is not None:
        if not isinstance(contrast, dict):
            f["contrast"] = None
            reasons.append("contrast not an object (dropped)")
        else:
            ref = str(contrast.get("reference_run") or "").strip().upper()
            rr = bundle["runs_by_label"].get(ref)
            bad = []
            if not rr or rr["analysis_target"]:
                bad.append("reference_run did not score")
            st = contrast.get("step")
            if not isinstance(st, int) or not (1 <= st <= n) or (rr and st > rr["n_steps"]):
                bad.append("contrast step out of range")
            if bad:
                f["contrast_struck"] = {"value": contrast, "why": bad}
                f["contrast"] = None
            else:
                contrast["reference_run"] = ref
                subject_action = _derived_action(r, st)
                reference_action = _derived_action(rr, st)
                if not subject_action or not reference_action:
                    relation = "unknown"
                elif subject_action == reference_action:
                    relation = "same_primitive"
                else:
                    relation = "different_primitive"
                contrast["primitive_relation"] = relation
                f["contrast"] = contrast
    sig = normalize_signature(f.get("signature") if isinstance(f.get("signature"), dict) else {})
    clean = {}
    for key, allowed in SIGNATURE.items():
        v = _derived_action(r, ds) if key == "action" and isinstance(ds, int) else (sig.get(key) or "")
        if v not in allowed:
            reasons.append(
                f"{'decisive action not recognized' if key == 'action' else f'signature.{key} invalid'} ({v!r})"
            )
            v = "unknown" if key == "outcome" else (v or "unknown")
        clean[key] = v
    f["signature"] = clean
    f["task_feasible"] = bundle.get("feasible", True)
    f["outcome_category"] = derive_outcome_category(clean, f["task_feasible"])
    for key in ("did", "machine"):
        if not str(f.get(key) or "").strip():
            reasons.append(f"{key} empty")
    if f.get("counterfactual") is not None and not isinstance(f.get("counterfactual"), str):
        f["counterfactual"] = None
        reasons.append("counterfactual was not text or null (dropped)")
    hard = [x for x in reasons if x.startswith((
        "run ", "no decisive-step", "decisive_step", "decisive action",
        "mechanism", "did empty", "machine empty",
    ))]
    return not hard, reasons


# ------------------------------------------------------------ cross-run checks
def _local_at(run, step):
    lcf = run.get("lcf") or []
    if not isinstance(step, int) or step < 0 or step >= len(lcf):
        return None
    return lcf[step]


def check_against_machine(f, bundle):
    """Claims the machine lines can refute. Returns a list of challenge strings.

    outcome=no_change is refuted when Δscreen reaches NO_CHANGE or Δlocal
    reaches LOCAL_CHANGE at the decisive step (the thresholds the Validator
    also uses). outcome=changed_not_committed is refuted when both are below
    NEGLIGIBLE_CHANGE.
    """
    out = []
    r = bundle["runs_by_label"].get(str(f.get("run") or "").upper())
    if not r:
        return out
    sig = f.get("signature") or {}
    ds = f.get("decisive_step")
    if isinstance(ds, int) and sig.get("outcome") == "no_change" and 0 <= ds < len(r["cf"]):
        v = r["cf"][ds]
        lv = _local_at(r, ds)
        parts = []
        if v is not None and v >= NO_CHANGE:
            parts.append(f"Δscreen={_pct(v)}")
        if lv is not None and lv >= LOCAL_CHANGE:
            parts.append(f"Δlocal={_pct(lv)}")
        if parts:
            out.append(
                f"run {f['run']} decisive_step {ds}: outcome=no_change but the machine recorded "
                + " and ".join(parts) + "."
            )
    if isinstance(ds, int) and sig.get("outcome") == "changed_not_committed" and 0 <= ds < len(r["cf"]):
        v = r["cf"][ds]
        lv = _local_at(r, ds)
        if v is not None and v < NEGLIGIBLE_CHANGE and (lv is None or lv < NEGLIGIBLE_CHANGE):
            shown = f"Δscreen={_pct(v)}"
            if lv is not None:
                shown += f" and Δlocal={_pct(lv)}"
            out.append(
                f"run {f['run']} decisive_step {ds}: outcome=changed_not_committed requires evidence that "
                f"the requested change appeared on screen before commit/save, but {shown}. "
                "Name the visible or recorded evidence for that change; otherwise use no_change."
            )
    return out


CHALLENGE_TEMPLATE = """\
The verifier checked your findings against the machine lines and found these claims unsupported:
{items}
Revise the affected findings (change the decisive step, signature, or drop the finding) \
and return the COMPLETE findings list in the same JSON shape. `screens_wanted` is ignored in this turn."""


# ------------------------------------------------------------ one bundle

def analyse_bundle(
    task, k_runs, run_labels, client, model, out_dir=None,
    harness_text=FALLBACK_HARNESS_TEXT, manifest_id=None,
    temperature=0.0, evidence_text=None,
):
    bundle = build_bundle_v2(task, k_runs, run_labels)
    if evidence_text:
        bundle["evidence_text"] = evidence_text
    prompt = PROMPT.format(
        n_runs=bundle["n_runs"], cap=REQUEST_CAP, rounds=REQUEST_ROUNDS,
        harness=harness_text, evidence=bundle["evidence_text"],
    )
    media0 = opening_media(bundle)
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}] + media0}]
    turns, requests_log, raw_log = 0, [], []
    tok_in = tok_out = retries = 0
    analysis = None
    while True:
        text, pi, po, fin, retried = ask(
            client, model, messages, temperature=temperature
        )
        retries += retried
        tok_in += pi or 0
        tok_out += po or 0
        raw_log.append(text)
        turns += 1
        analysis = _json_from(text) or {}
        wanted = parse_requests(analysis.get("screens_wanted"), bundle, REQUEST_CAP)
        requests_used = len([x for x in requests_log])
        if wanted and requests_used < REQUEST_ROUNDS:
            media = shots_for_requests(bundle, wanted)
            requests_log.append(wanted)
            messages.append({"role": "assistant", "content": text})
            messages.append(followup_message(bundle, media, wanted, REQUEST_ROUNDS - len(requests_log)))
            continue
        if not analysis.get("findings") and not analysis.get("note") and turns < REQUEST_ROUNDS + 2:
            # prose instead of the JSON object: one nudge, then take what comes
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "Return the ONE JSON object now (findings required); no prose before or after it."})
            continue
        break
    findings_raw = analysis.get("findings") if isinstance(analysis, dict) else None
    challenges = []
    for f in findings_raw or []:
        if isinstance(f, dict) and verify_finding(dict(f), bundle)[0]:
            challenges += check_against_machine(f, bundle)
    if challenges:  # one challenge turn: the model may revise or drop, not argue
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": CHALLENGE_TEMPLATE.format(items="\n".join(f"- {c}" for c in challenges))})
        text, pi, po, fin, retried = ask(
            client, model, messages, temperature=temperature
        )
        retries += retried
        tok_in += pi or 0
        tok_out += po or 0
        raw_log.append(text)
        turns += 1
        revised = _json_from(text) or {}
        if isinstance(revised.get("findings"), list):
            analysis = revised
            findings_raw = revised["findings"]
    findings, dropped = [], []
    for f in findings_raw or []:
        if not isinstance(f, dict):
            continue
        ok, reasons = verify_finding(f, bundle)
        if ok:
            still = check_against_machine(f, bundle)
            if still:
                f["machine_conflict"] = still
        f["verify_notes"] = reasons
        f["task_id"] = bundle["task_id"]
        f["domain"] = bundle["domain"]
        f["task_stratum"] = bundle["stratum"]
        f["run_reward"] = bundle["runs_by_label"][str(f.get("run") or "").upper()]["reward"] if ok else None
        f["finding_id"] = hashlib.sha1(
            f"{bundle['task_id']}|{f.get('run')}|{f.get('mechanism')}|{f.get('decisive_step')}".encode()
        ).hexdigest()[:10]
        if manifest_id:
            f["manifest_id"] = manifest_id
        (findings if ok else dropped).append(f)
    analysis_status, status_reason = classify_analysis_status(
        findings, findings_raw, analysis.get("note") if isinstance(analysis, dict) else None
    )
    row = {
        "task_id": bundle["task_id"],
        "domain": bundle["domain"],
        "instruction": bundle["instruction"],
        "stratum": bundle["stratum"],
        "feasible": bundle["feasible"],
        "rewards": {r["label"]: r["reward"] for r in bundle["runs"]},
        "n_steps": {r["label"]: r["n_steps"] for r in bundle["runs"]},
        "opening_screens": {r["label"]: r["screens"] for r in bundle["runs"]},
        "screen_requests": requests_log,
        "challenges": challenges,
        "turns": turns,
        "retries": retries,
        "tokens": {"in": tok_in, "out": tok_out},
        "analysis_status": analysis_status,
        "analysis_status_reason": status_reason,
        "manifest_id": manifest_id,
        "note": analysis.get("note") if isinstance(analysis, dict) else None,
        "findings": findings,
        "dropped": dropped,
        "evidence_chars": len(bundle["evidence_text"]),
    }
    if out_dir:
        with open(os.path.join(out_dir, f"raw_{bundle['task_id'][:8]}.json"), "w") as fh:
            json.dump({"raw_responses": raw_log, "evidence": bundle["evidence_text"]}, fh, ensure_ascii=False, indent=1)
    return row


def main():
    from gui_harvest.llm import client_config, make_client

    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument(
        "--gaft",
        default="",
        help="GAFT index.json produced from this Search sample",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--backend",
        default=os.environ.get("GUI_HARVEST_ANALYST_BACKEND")
        or os.environ.get("GUI_HARVEST_LLM_BACKEND", "openrouter"),
    )
    ap.add_argument(
        "--model",
        default=os.environ.get(
            "GUI_HARVEST_ANALYST_MODEL", "anthropic/claude-sonnet-5"
        ),
    )
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma-separated task_id prefixes")
    ap.add_argument(
        "--retry-status",
        default="",
        help="comma-separated analysis statuses to remove and rerun, e.g. dropout",
    )
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--harness-card", default="", help="path to the framework's HARNESS_CARD.md (default: sample['harness_card'])")
    args = ap.parse_args()

    with open(args.sample, encoding="utf-8") as fh:
        sample = json.load(fh)
    gaft_text = {}
    if args.gaft:
        with open(args.gaft, encoding="utf-8") as fh:
            gaft_index = json.load(fh)
        for task in gaft_index.get("tasks") or []:
            with open(task["evidence_path"], encoding="utf-8") as fh:
                gaft_text[task["task_id"]] = fh.read()
        missing = [
            task["task_id"]
            for task in sample["tasks"]
            if task["task_id"] not in gaft_text
        ]
        if missing:
            raise SystemExit(
                "GAFT index is missing Search tasks: "
                + ", ".join(missing[:5])
            )
    card_path = args.harness_card or sample.get("harness_card") or ""
    if card_path and not os.path.isabs(card_path):
        card_path = os.path.join(args.repo_root, card_path)
    harness_text = load_harness_card(card_path)
    print(f"harness card: {card_path or '(none; fallback text)'} ({len(harness_text)} chars)")
    k_runs = {k: os.path.join(args.repo_root, v) for k, v in sample["K_runs"].items()}
    run_labels = sample["run_labels"]
    tasks = sample["tasks"]
    # A task is diagnosed only when at least one kept run scored nothing.
    tasks = [
        t for t in tasks
        if any((t.get("rewards") or {}).get(k) == 0 for k in run_labels)
    ]
    if args.only:
        pre = [p.strip() for p in args.only.split(",") if p.strip()]
        tasks = [t for t in tasks if any(t["task_id"].startswith(p) for p in pre)]
    if args.limit:
        tasks = tasks[: args.limit]
    retry_statuses = {x.strip() for x in args.retry_status.split(",") if x.strip()}
    done = prepare_resume(args.out, retry_statuses)
    tasks = [t for t in tasks if t["task_id"] not in done]
    print(f"analyst: {len(tasks)} bundles to do ({len(done)} already in {args.out}); model={args.model}")
    out_dir = os.path.dirname(os.path.abspath(args.out))
    manifest = create_manifest(
        HERE,
        args.sample,
        card_path,
        {
            "role": "analyst",
            "model": args.model,
            **client_config(args.backend),
            "workers": args.workers,
            "temperature": args.temperature,
            "limit": args.limit,
            "only": args.only,
            "retry_status": sorted(retry_statuses),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        },
        PROMPT + "\n" + harness_text,
        list(k_runs.values()),
        harness_card_text=harness_text,
    )
    manifest_paths = [
        os.path.join(out_dir, "manifest.json"),
        os.path.join(out_dir, f"manifest_{manifest['manifest_id']}.json"),
    ]
    for path in manifest_paths:
        write_manifest(path, manifest)
    client = make_client(args.backend)
    raw_dir = os.path.join(out_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    def one(t):
        try:
            return analyse_bundle(
                t, k_runs, run_labels, client, args.model, raw_dir, harness_text,
                manifest_id=manifest["manifest_id"],
                temperature=args.temperature,
                evidence_text=gaft_text.get(t["task_id"]),
            )
        except Exception as e:  # noqa: BLE001
            return {
                "task_id": t["task_id"], "domain": t["domain"],
                "error": f"{e.__class__.__name__}: {e}",
                "analysis_status": "dropout",
                "analysis_status_reason": "analyst exception or retry exhaustion",
                "manifest_id": manifest["manifest_id"],
                "tokens": {"in": 0, "out": 0},
                "retries": 4,
            }

    with open(args.out, "a") as fh, cf.ThreadPoolExecutor(args.workers) as ex:
        for row in ex.map(one, tasks):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            update_manifest(manifest, row)
            for path in manifest_paths:
                write_manifest(path, manifest)
            if "error" in row:
                print(f"  {row['task_id'][:8]} {row['domain']:18s} ERROR {row['error'][:100]}")
            else:
                print(
                    f"  {row['task_id'][:8]} {row['domain']:18s} {row['stratum']:11s} "
                    f"turns={row['turns']} req={sum(len(x) for x in row['screen_requests'])} "
                    f"findings={len(row['findings'])} dropped={len(row['dropped'])} "
                    f"tok={row['tokens']['in']}/{row['tokens']['out']}"
                )
    write_dataset_manifest(args.out, out_dir)


if __name__ == "__main__":
    main()
