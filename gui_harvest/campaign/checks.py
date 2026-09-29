"""Generic L0/L1 checks for an Engineer's before/after harness trees."""

from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys

from gui_harvest.roles.engineer import validate_capability_manifest
from gui_harvest.campaign.provenance import create_stage_manifest, write_manifest

CHECKS_SCHEMA = "gui-harvest-code-checks-v1"


def _load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree(root):
    files = {}
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs if name not in {".git", "__pycache__"}]
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            files[rel] = _hash(path)
    return files


def _matches(path, patterns):
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns or [])


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as source:
            return source.read()
    except (UnicodeDecodeError, OSError):
        return None


def _changed_lines(before, after):
    old = (before or "").splitlines()
    new = (after or "").splitlines()
    total = 0
    for line in difflib.ndiff(old, new):
        if line.startswith(("+ ", "- ")):
            total += 1
    return total


def run_l0(before_root, after_root, capability):
    problems = list(validate_capability_manifest(capability))
    before = _tree(before_root)
    after = _tree(after_root)
    changed = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    editable = capability.get("editable_paths") or []
    create = capability.get("create_paths") or []
    read_only = capability.get("read_only_paths") or []
    limits = capability.get("limits") or {}
    max_files = int(limits.get("max_touched_files") or 0)
    max_lines = int(limits.get("max_changed_lines") or 0)
    if max_files and len(changed) > max_files:
        problems.append(f"{len(changed)} touched files exceeds limit {max_files}")
    line_total = 0
    rows = []
    forbidden = [
        re.compile(pattern)
        for pattern in capability.get("forbidden_content_regex") or []
    ]
    for rel in changed:
        action = (
            "create" if rel not in before else "delete" if rel not in after else "modify"
        )
        allowed_patterns = create if action == "create" else editable
        if _matches(rel, read_only):
            problems.append(f"{rel}: read-only path changed")
        elif not _matches(rel, allowed_patterns):
            problems.append(f"{rel}: {action} is outside capability manifest")
        before_text = _read_text(os.path.join(before_root, rel)) if rel in before else ""
        after_text = _read_text(os.path.join(after_root, rel)) if rel in after else ""
        lines = _changed_lines(before_text, after_text)
        line_total += lines
        if rel.endswith(".py") and after_text is not None:
            try:
                ast.parse(after_text, filename=rel)
            except SyntaxError as error:
                problems.append(
                    f"{rel}: syntax error at line {error.lineno}: {error.msg}"
                )
        for pattern in forbidden:
            if after_text and pattern.search(after_text):
                problems.append(f"{rel}: forbidden content matched {pattern.pattern!r}")
        rows.append(
            {
                "path": rel,
                "action": action,
                "changed_lines": lines,
                "before_sha256": before.get(rel),
                "after_sha256": after.get(rel),
            }
        )
    if not changed:
        problems.append("candidate tree contains no changes")
    if max_lines and line_total > max_lines:
        problems.append(f"{line_total} changed lines exceeds limit {max_lines}")
    return {
        "status": "passed" if not problems else "failed",
        "problems": problems,
        "changed_files": rows,
        "changed_lines": line_total,
    }


def run_l1(after_root, capability):
    rows = []
    problems = []
    for index, command in enumerate(capability.get("l1_commands") or []):
        if not isinstance(command, list) or not command:
            problems.append(f"l1_commands[{index}] must be a non-empty argv list")
            continue
        try:
            argv = [
                sys.executable if value == "{python}" else value
                for value in command
            ]
            result = subprocess.run(
                argv,
                cwd=after_root,
                capture_output=True,
                text=True,
                timeout=int(capability.get("l1_timeout_seconds") or 120),
                check=False,
            )
            row = {
                "argv": argv,
                "return_code": result.returncode,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-2000:],
            }
            rows.append(row)
            if result.returncode != 0:
                problems.append(
                    f"command {index} returned {result.returncode}: {command}"
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            problems.append(f"command {index} could not complete: {error}")
    if not rows:
        problems.append("capability manifest defines no L1 commands")
    return {
        "status": "passed" if not problems else "failed",
        "problems": problems,
        "commands": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before-root", required=True)
    parser.add_argument("--after-root", required=True)
    parser.add_argument("--capability-manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    capability = _load(args.capability_manifest)
    report = {
        "schema_version": CHECKS_SCHEMA,
        "before_root": os.path.abspath(args.before_root),
        "after_root": os.path.abspath(args.after_root),
        "l0": run_l0(args.before_root, args.after_root, capability),
        "l1": run_l1(args.after_root, capability),
    }
    report["passed"] = (
        report["l0"]["status"] == "passed"
        and report["l1"]["status"] == "passed"
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
    manifest = create_stage_manifest(
        os.path.dirname(__file__),
        "code_checks",
        [args.capability_manifest],
        {
            "before_root": os.path.abspath(args.before_root),
            "after_root": os.path.abspath(args.after_root),
            "checks_schema": CHECKS_SCHEMA,
        },
        "deterministic tree diff, syntax, capability, and argv checks",
    )
    manifest["summary"] = {
        "passed": report["passed"],
        "touched_files": len(report["l0"]["changed_files"]),
        "changed_lines": report["l0"]["changed_lines"],
        "l1_commands": len(report["l1"]["commands"]),
    }
    write_manifest(args.out + ".manifest.json", manifest)
    print(
        f"{'passed' if report['passed'] else 'failed'}: "
        f"L0={report['l0']['status']} L1={report['l1']['status']}"
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
