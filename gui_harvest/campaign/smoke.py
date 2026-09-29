"""L1 checks for an Agent S3 candidate tree: imports, runner interface, smoke.

Run from the candidate root. The checks mirror how the OSWorld runner uses the
harness: it imports ``AgentS3`` and ``OSWorldACI``, constructs them with fixed
keyword arguments, and calls ``reset`` and ``predict``. The smoke step builds
both objects offline (no environment, no model call). Unit tests under
``tests/`` run when the candidate ships them.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import subprocess
import sys

AGENT_MODULE = "gui_agents.s3.agents.agent_s"
ACI_MODULE = "gui_agents.s3.agents.grounding"
ACI_KWARGS = (
    "env",
    "platform",
    "engine_params_for_generation",
    "engine_params_for_grounding",
    "width",
    "height",
)
AGENT_POSITIONAL = 2
AGENT_KWARGS = ("platform",)
AGENT_METHODS = {"reset": 1, "predict": 2}
SMOKE_ENGINE = {
    "engine_type": "openai",
    "model": "l1-smoke",
    "base_url": "http://127.0.0.1:9/v1",
    "api_key": "l1-smoke",
}


def _accepts(signature, positional, keywords):
    params = list(signature.parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    if any(p.kind is p.VAR_KEYWORD for p in params):
        named = set()
    else:
        named = {
            p.name
            for p in params
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }
    missing = [key for key in keywords if named and key not in named]
    positional_slots = [
        p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    has_var_positional = any(p.kind is p.VAR_POSITIONAL for p in params)
    if len(positional_slots) < positional and not has_var_positional:
        missing.append(f"{positional} positional argument(s)")
    return missing


def check_imports():
    modules = {}
    problems = []
    for name in (AGENT_MODULE, ACI_MODULE):
        try:
            modules[name] = importlib.import_module(name)
        except Exception as error:
            problems.append(f"import {name}: {type(error).__name__}: {error}")
    return modules, problems


def check_interface(modules):
    problems = []
    agent_cls = getattr(modules.get(AGENT_MODULE), "AgentS3", None)
    aci_cls = getattr(modules.get(ACI_MODULE), "OSWorldACI", None)
    if agent_cls is None:
        problems.append(f"{AGENT_MODULE}.AgentS3 is missing")
    else:
        for key in _accepts(
            inspect.signature(agent_cls.__init__), AGENT_POSITIONAL, AGENT_KWARGS
        ):
            problems.append(f"AgentS3.__init__ no longer accepts {key}")
        for method, positional in AGENT_METHODS.items():
            func = getattr(agent_cls, method, None)
            if not callable(func):
                problems.append(f"AgentS3.{method} is missing")
                continue
            required = [
                p
                for p in list(inspect.signature(func).parameters.values())[1:]
                if p.default is p.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            if len(required) > positional:
                problems.append(
                    f"AgentS3.{method} requires {len(required)} arguments; "
                    f"the runner passes at most {positional}"
                )
            if method == "reset" and required:
                problems.append("AgentS3.reset must be callable without arguments")
    if aci_cls is None:
        problems.append(f"{ACI_MODULE}.OSWorldACI is missing")
    else:
        for key in _accepts(inspect.signature(aci_cls.__init__), 0, ACI_KWARGS):
            problems.append(f"OSWorldACI.__init__ no longer accepts {key}")
    return problems


def check_smoke(modules):
    try:
        aci = modules[ACI_MODULE].OSWorldACI(
            env=None,
            platform="linux",
            engine_params_for_generation=dict(SMOKE_ENGINE),
            engine_params_for_grounding=dict(
                SMOKE_ENGINE, grounding_width=1920, grounding_height=1080
            ),
            width=1920,
            height=1080,
        )
        agent = modules[AGENT_MODULE].AgentS3(
            dict(SMOKE_ENGINE), aci, platform="linux"
        )
        agent.reset()
    except Exception as error:
        return [f"offline construction: {type(error).__name__}: {error}"]
    return []


def check_unit_tests(root):
    tests = os.path.join(root, "tests")
    if not os.path.isdir(tests):
        return "none", []
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return "failed", [f"unit tests failed: {result.stderr[-1500:]}"]
    return "passed", []


def run(root):
    root = os.path.abspath(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    report = {"root": root}
    modules, problems = check_imports()
    report["imports"] = "failed" if problems else "passed"
    if not problems:
        interface = check_interface(modules)
        report["interface"] = "failed" if interface else "passed"
        problems += interface
    if not problems:
        smoke = check_smoke(modules)
        report["smoke"] = "failed" if smoke else "passed"
        problems += smoke
    status, unit = check_unit_tests(root)
    report["unit_tests"] = status
    problems += unit
    report["problems"] = problems
    report["passed"] = not problems
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    report = run(args.root)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
