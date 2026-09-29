"""Fixed vocabularies shared by the analyst and the clusterer.

Three layers:

  signature  -- filled by the analyst per finding, from closed vocabularies:
                action (the primitive at the decisive step), outcome (what the
                machine recorded), noticed (what the next step shows).
  outcome category -- derived HERE, deterministically, from the signature.
                Not a model output. Sixteen categories. Modes never cross
                outcome categories.
  mode       -- named by the clusterer inside one outcome category; open
                vocabulary, one round's worth.

Outcome categories organize evidence for clustering. They do not restrict
which editable source files or architectural surfaces the Engineer may change.
"""

ACTIONS = {
    "click", "type", "hotkey", "drag", "scroll", "open", "switch", "wait",
    "code_agent", "done", "fail",
}
OUTCOMES = {
    "no_change", "unintended_dialog", "wrong_window", "landed_off_target",
    "error_reported", "changed_wrong_content", "changed_not_committed",
    "changed_as_intended", "exogenous_block", "unknown",
}
NOTICED = {"changed_approach", "retried_same", "continued_unaware", "declared_done", "run_ended"}

# harness primitive names the analyst may echo instead of the vocabulary word
ACTION_ALIASES = {
    "drag_and_drop": "drag", "set_cell_values": "type", "switch_applications": "switch",
    "call_code_agent": "code_agent", "code": "code_agent", "hotkeys": "hotkey", "key": "hotkey",
    "double_click": "click", "right_click": "click", "open_app": "open", "scroll_down": "scroll", "scroll_up": "scroll",
    # native pyautogui agents name the library primitive directly
    "write": "type", "typewrite": "type", "press": "hotkey", "keydown": "hotkey",
    "keyup": "hotkey", "doubleclick": "click", "rightclick": "click",
    "middleclick": "click", "tripleclick": "click", "mousedown": "click",
    "mouseup": "click", "moveto": "click", "moverel": "click",
    "dragto": "drag", "dragrel": "drag", "hscroll": "scroll", "vscroll": "scroll",
}


def normalize_signature(sig):
    sig = dict(sig or {})
    a = str(sig.get("action") or "").strip().lower()
    sig["action"] = ACTION_ALIASES.get(a, a)
    sig["outcome"] = str(sig.get("outcome") or "").strip().lower()
    sig["noticed"] = str(sig.get("noticed") or "").strip().lower()
    return sig


def cell(signature, feasible=True):
    """The deterministic clustering unit: outcome category x primitive.

    `noticed` is an attribute of a finding, not a partition key. The same
    mechanism remains the same mechanism whether the run retried it or walked on.
    """
    s = normalize_signature(signature)
    return (derive_outcome_category(s, feasible), s["action"])

# outcome category id -> (one-line definition, harness surfaces the category
# usually points at). Roles read the definition; the surfaces are descriptive.
OUTCOME_CATEGORIES = {
    "ACTION_NO_EFFECT": (
        "a GUI primitive produced no change on the machine and the run went on (retried or continued)",
        ["grounding_primitive", "route_control", "observation"],
    ),
    "GROUNDING_MISS": (
        "the pointer landed on a control other than the one the description named",
        ["grounding_primitive", "observation"],
    ),
    "INPUT_TO_WRONG_WINDOW": (
        "keys or clicks went to a window other than the one the action named",
        ["grounding_primitive", "observation", "route_control"],
    ),
    "UNINTENDED_DIALOG": (
        "the primitive opened a dialog the intent did not name",
        ["grounding_primitive", "route_control", "observation"],
    ),
    "EDIT_NOT_COMMITTED": (
        "the right change was on screen but not committed to the file or setting the task names (no Enter, picker not registered, in-cell edit, unsaved)",
        ["grounding_primitive", "worker_prompt", "termination_check", "observation"],
    ),
    "APP_SEMANTICS_MISMATCH": (
        "the primitive did something the application defines differently from what the run assumed (append vs replace, save vs export, paste path vs object, drag geometry)",
        ["grounding_primitive", "worker_prompt", "observation"],
    ),
    "ERROR_SHOWN_NOT_HANDLED": (
        "the application or a harness check reported an error and the run did not act on it",
        ["worker_prompt", "termination_check", "route_control", "observation"],
    ),
    "TARGET_OR_ROUTE_CHOICE": (
        "every primitive landed as intended; what was chosen (object, scope, route) cannot reach the end state the task requires",
        ["worker_prompt", "observation", "model_capability"],
    ),
    "CODE_RESULT_NOT_REFLECTED": (
        "the code agent changed the file on disk, the open GUI still shows or later saves the old state",
        ["code_agent", "observation", "worker_prompt"],
    ),
    "CODE_OUTPUT_WRONG": (
        "the code agent completed but wrote content the task does not want",
        ["code_agent", "worker_prompt", "model_capability"],
    ),
    "CODE_ERROR_LOOP": (
        "the code agent hit an error and repeated it, or returned without executing",
        ["code_agent", "route_control"],
    ),
    "CODE_OUTPUT_UNVERIFIED": (
        "the code agent's report was taken as done without reading back the result",
        ["code_agent", "termination_check", "worker_prompt"],
    ),
    "PREMATURE_TERMINATION": (
        "the run called done() or fail() on a state it had not checked against the task",
        ["termination_check", "worker_prompt", "observation"],
    ),
    "TERMINATION_WITHHELD": (
        "the run's done()/fail() was refused by a harness check and the run acted on the refusal",
        ["termination_check", "worker_prompt", "observation"],
    ),
    "INFEASIBILITY_MISSED": (
        "the task is infeasible (the correct ending is fail()); the run kept operating or called done()",
        ["worker_prompt", "termination_check", "observation"],
    ),
    "INFEASIBILITY_MISJUDGED": (
        "the task is feasible; the run called fail()",
        ["worker_prompt", "termination_check", "observation"],
    ),
    "EXOGENOUS": (
        "something the action does not explain intervened (captcha, crash, recovery, loading)",
        ["infra", "route_control", "worker_prompt"],
    ),
}


def derive_outcome_category(signature, feasible=True):
    """signature: {"action", "outcome", "noticed"} + the task card's feasibility
    -> outcome category id. Total function."""
    a = (signature or {}).get("action") or "unknown"
    o = (signature or {}).get("outcome") or "unknown"
    if o == "exogenous_block":
        return "EXOGENOUS"
    if not feasible and a != "fail":
        return "INFEASIBILITY_MISSED"
    if a == "fail" and feasible and o != "error_reported":
        return "INFEASIBILITY_MISJUDGED"
    if a in ("done", "fail"):
        return "TERMINATION_WITHHELD" if o == "error_reported" else "PREMATURE_TERMINATION"
    if a == "code_agent":
        return {
            "changed_not_committed": "CODE_RESULT_NOT_REFLECTED",
            "changed_wrong_content": "CODE_OUTPUT_WRONG",
            "changed_as_intended": "CODE_OUTPUT_UNVERIFIED",
            "error_reported": "CODE_ERROR_LOOP",
            "no_change": "CODE_ERROR_LOOP",
        }.get(o, "CODE_OUTPUT_UNVERIFIED")
    return {
        "no_change": "ACTION_NO_EFFECT",
        "landed_off_target": "GROUNDING_MISS",
        "wrong_window": "INPUT_TO_WRONG_WINDOW",
        "unintended_dialog": "UNINTENDED_DIALOG",
        "changed_not_committed": "EDIT_NOT_COMMITTED",
        "changed_wrong_content": "APP_SEMANTICS_MISMATCH",
        "error_reported": "ERROR_SHOWN_NOT_HANDLED",
        "changed_as_intended": "TARGET_OR_ROUTE_CHOICE",
    }.get(o, "TARGET_OR_ROUTE_CHOICE")
