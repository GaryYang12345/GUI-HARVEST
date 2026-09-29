"""OpenAI-compatible tool-using Harness Engineer."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SYSTEM = """\
You are the GUI-HARVEST Harness Engineer. Follow ENGINEER_BRIEF.md exactly.
You may inspect the baseline, candidate, and frozen round evidence. Write only
the candidate harness plus proposal.pre.json and ENGINEER_REPORT.md in the
round workspace. Never inspect evaluator source, expected answers, sealed Test,
or unrelated result trees. Do not run GUI evaluation. Before editing, freeze
the intervention and observable predictions in proposal.pre.json. Finish only
after the candidate Harness Card and local tests are updated.
"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List readable workspace files matching a glob.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 file. Prefix round/ or baseline/ when needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "max_lines": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Regex-search readable source and evidence files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write candidate source or round/proposal.pre.json / round/ENGINEER_REPORT.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the candidate harness.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run one predeclared capability L1 command by index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command_index": {"type": "integer", "minimum": 0},
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["command_index"],
            },
        },
    },
]


class EngineerWorkspace:
    def __init__(self, candidate_root, baseline_root, round_root):
        self.candidate = Path(candidate_root).resolve()
        self.baseline = Path(baseline_root).resolve()
        self.round = Path(round_root).resolve()
        self.frozen_round_files = {
            "SESSION.json",
            "ENGINEER_BRIEF.md",
            "engineer_queue.json",
            "findings.jsonl",
            "modes.json",
            "capability.json",
            "ledger.json",
            "round.json",
            "round-meta.json",
            "search-sample.json",
        }
        capability_path = self.round / "capability.json"
        self.capability = json.loads(capability_path.read_text(encoding="utf-8"))

    def resolve(self, raw, *, write=False):
        raw = str(raw or "").replace("\\", "/").lstrip("/")
        if raw.startswith("round/"):
            path = (self.round / raw[6:]).resolve()
            root = self.round
            if write and path.name not in {
                "proposal.pre.json",
                "ENGINEER_REPORT.md",
            }:
                raise PermissionError("only Engineer output files are writable in round/")
        elif raw.startswith("baseline/"):
            path = (self.baseline / raw[9:]).resolve()
            root = self.baseline
            if write:
                raise PermissionError("baseline is read-only")
        else:
            path = (self.candidate / raw).resolve()
            root = self.candidate
        if path != root and root not in path.parents:
            raise PermissionError("path escapes an allowed workspace root")
        return path

    def list_files(self, pattern):
        rows = []
        scopes = (
            ("candidate", self.candidate),
            ("baseline", self.baseline),
            ("round", self.round),
        )
        for prefix, root in scopes:
            for path in root.rglob("*"):
                if not path.is_file() or any(
                    part in {".git", "__pycache__", ".venv"} for part in path.parts
                ):
                    continue
                rel = str(path.relative_to(root))
                exposed = rel if prefix == "candidate" else f"{prefix}/{rel}"
                if fnmatch.fnmatch(exposed, pattern) or fnmatch.fnmatch(rel, pattern):
                    rows.append(exposed)
        return "\n".join(sorted(rows)[:2000])

    def read_file(self, path, start_line=1, max_lines=400):
        resolved = self.resolve(path)
        lines = resolved.read_text(encoding="utf-8").splitlines()
        start = max(int(start_line), 1) - 1
        return "\n".join(lines[start : start + min(int(max_lines), 2000)])

    def search(self, pattern, path="**/*"):
        regex = re.compile(pattern)
        output = []
        for name in self.list_files(path).splitlines():
            try:
                text = self.read_file(name, max_lines=100000)
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    output.append(f"{name}:{number}:{line}")
                    if len(output) >= 500:
                        return "\n".join(output)
        return "\n".join(output)

    def write_file(self, path, content):
        resolved = self.resolve(path, write=True)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"wrote {resolved}"

    def delete_file(self, path):
        resolved = self.resolve(path, write=True)
        if self.round in resolved.parents:
            raise PermissionError("round outputs cannot be deleted")
        resolved.unlink()
        return f"deleted {resolved}"

    def run_command(self, command_index, timeout_seconds=120):
        commands = self.capability.get("l1_commands") or []
        index = int(command_index)
        if index < 0 or index >= len(commands):
            raise ValueError(f"unknown capability command index {index}")
        argv = [
            sys.executable if value == "{python}" else value
            for value in commands[index]
        ]
        result = subprocess.run(
            [str(value) for value in argv],
            cwd=self.candidate,
            capture_output=True,
            text=True,
            timeout=min(int(timeout_seconds), 600),
            check=False,
        )
        return json.dumps(
            {
                "return_code": result.returncode,
                "stdout": result.stdout[-12000:],
                "stderr": result.stderr[-12000:],
            }
        )

    def call(self, name, arguments):
        function = getattr(self, name)
        return function(**arguments)


def run_api_engineer(
    *,
    backend,
    model,
    temperature,
    candidate_root,
    baseline_root,
    round_root,
    max_turns=80,
    max_tokens=8192,
):
    from gui_harvest.llm import make_client

    workspace = EngineerWorkspace(candidate_root, baseline_root, round_root)
    brief = workspace.read_file("round/ENGINEER_BRIEF.md", max_lines=2000)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": brief},
    ]
    client = make_client(backend)
    usage = {"input_tokens": 0, "output_tokens": 0}
    for turn in range(max_turns):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=float(temperature),
            max_tokens=int(max_tokens),
        )
        response_usage = getattr(response, "usage", None)
        usage["input_tokens"] += int(
            getattr(response_usage, "prompt_tokens", 0) or 0
        )
        usage["output_tokens"] += int(
            getattr(response_usage, "completion_tokens", 0) or 0
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        tool_calls = message.tool_calls or []
        if not tool_calls:
            proposal = Path(round_root) / "proposal.pre.json"
            if not proposal.is_file():
                raise RuntimeError(
                    "API Engineer stopped without writing proposal.pre.json"
                )
            return {
                "status": "completed",
                "turns": turn + 1,
                "final_message": message.content,
                "usage": usage,
            }
        for tool_call in tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
                output = workspace.call(tool_call.function.name, arguments)
            except Exception as error:  # tool error is recoverable by the model
                output = f"{type(error).__name__}: {error}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                }
            )
    raise RuntimeError(f"API Engineer exceeded max_turns={max_turns}")

