"""Harness adapters isolate runtime-specific fingerprints and rollout contracts."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path


def _hash_python_roots(roots):
    digest = hashlib.sha256()
    for root in sorted(str(Path(root).resolve()) for root in roots):
        if os.path.isfile(root):
            digest.update(os.path.basename(root).encode())
            with open(root, "rb") as source:
                digest.update(source.read())
            continue
        if not os.path.isdir(root):
            raise ValueError(f"runtime source root is missing: {root}")
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = sorted(
                name
                for name in subdirs
                if name not in {".git", "__pycache__", ".venv"}
            )
            for name in sorted(names):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                digest.update(os.path.relpath(path, root).encode())
                with open(path, "rb") as source:
                    digest.update(source.read())
    return digest.hexdigest()


class HarnessAdapter:
    name = "generic_python"

    def runtime_behavior_hash(self, harness_root):
        return _hash_python_roots([harness_root])

    def harness_card_path(self, harness_root, configured="HARNESS_CARD.md"):
        path = Path(configured)
        return str(path if path.is_absolute() else Path(harness_root) / path)

    def clone(self, source, destination):
        source = Path(source).resolve()
        destination = Path(destination).resolve()
        if destination.exists():
            raise FileExistsError(destination)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".venv", ".pytest_cache"
            ),
        )
        return str(destination)

    def restore(self, snapshot, destination):
        """Put snapshot source and Harness Card back onto the selected tree."""
        snapshot = Path(snapshot).resolve()
        destination = Path(destination).resolve()
        if snapshot == destination:
            return str(destination)
        if not snapshot.is_dir():
            raise FileNotFoundError(snapshot)
        kept = {".git", "__pycache__", ".venv", ".pytest_cache"}

        def listed(root):
            files = {}
            for directory, subdirs, names in os.walk(root):
                subdirs[:] = sorted(
                    name for name in subdirs if name not in kept
                )
                for name in names:
                    if name.endswith(".pyc"):
                        continue
                    path = Path(directory) / name
                    files[path.relative_to(root).as_posix()] = path
            return files

        original = listed(snapshot)
        current = listed(destination)
        for relative, source_path in original.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
        for relative, path in current.items():
            if relative not in original:
                path.unlink()
        return str(destination)


class AgentS3Adapter(HarnessAdapter):
    name = "agent_s3"

    def runtime_behavior_hash(self, harness_root):
        root = Path(harness_root)
        return _hash_python_roots(
            [
                root / "gui_agents" / "s3",
                root / "osworld_setup" / "s3" / "lib_run_single.py",
            ]
        )


ADAPTERS = {
    "agent_s3": AgentS3Adapter(),
    "generic_python": HarnessAdapter(),
}


def get_adapter(name):
    try:
        return ADAPTERS[name]
    except KeyError as error:
        raise ValueError(
            f"unknown harness adapter {name!r}; available: {sorted(ADAPTERS)}"
        ) from error

