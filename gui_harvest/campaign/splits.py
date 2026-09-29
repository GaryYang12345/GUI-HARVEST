"""Fixed Search, Validation, and sealed Test collections.

Each split is an OSWorld evaluation suite: a JSON object whose keys are
application domains and whose values are task-id lists. Suites are created
before any rollout. Assignment is stratified by domain: every domain gets a
fixed proportional quota in each split, and tasks inside a domain are drawn
uniformly at random with a public seed. No score, outcome, or task content is
used.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from pathlib import Path

from gui_harvest.campaign.provenance import sha256_file


SCHEMA = "gui-harvest-split-manifest-v1"
SEALED_SCHEMA = "gui-harvest-sealed-test-v1"


def _load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def load_suite(path):
    """Return ``{"domains": {domain: [task_id]}, "task_ids": [...]}``."""
    payload = _load(path)
    if not isinstance(payload, dict) or not payload:
        raise ValueError(
            f"{path} must be an OSWorld evaluation suite: "
            '{{"domain": ["task-id", ...]}}'
        )
    domains = {}
    task_ids = []
    seen = set()
    for domain, ids in payload.items():
        if str(domain).startswith("_"):
            continue
        if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
            raise ValueError(f"{path}: domain {domain!r} must list task ids")
        domains[str(domain)] = list(ids)
        for task_id in ids:
            if task_id in seen:
                raise ValueError(f"{path}: duplicate task id {task_id}")
            seen.add(task_id)
            task_ids.append(task_id)
    if not task_ids:
        raise ValueError(f"{path} contains no task ids")
    return {"domains": domains, "task_ids": task_ids}


def load_tasks(examples_dir, universe_path=None):
    """Load ``[{"task_id", "domain"}]`` from benchmark example files.

    The domain is the ``examples/<domain>/`` directory, which is how OSWorld
    groups and reports tasks. A task's ``snapshot`` field names its starting
    application and differs from the domain for multi-application tasks, so it
    is not used. ``universe_path`` is an OSWorld evaluation suite; when omitted,
    every ``examples/<domain>/<task-id>.json`` file is eligible.
    """
    examples_dir = Path(examples_dir)
    if universe_path:
        pairs = sorted(
            (domain, task_id)
            for domain, task_ids in load_suite(universe_path)["domains"].items()
            for task_id in task_ids
        )
    else:
        pairs = sorted(
            (path.parent.name, path.stem)
            for path in examples_dir.glob("*/*.json")
        )
    tasks = []
    for domain, task_id in pairs:
        path = examples_dir / domain / f"{task_id}.json"
        if not path.is_file():
            raise ValueError(f"task example does not exist: {path}")
        tasks.append({"task_id": task_id, "domain": domain})
    if not tasks:
        raise ValueError(f"no task examples found under {examples_dir}")
    return tasks


def domain_quotas(domain_sizes, total):
    """Largest-remainder apportionment of ``total`` across domains.

    Each domain receives ``floor(n_d * total / N)`` and the leftover units go
    to the largest fractional parts; ties are broken by domain name so the
    result does not depend on input order.
    """
    universe = sum(domain_sizes.values())
    exact = {d: n * total / universe for d, n in domain_sizes.items()}
    quotas = {d: int(value) for d, value in exact.items()}
    leftover = total - sum(quotas.values())
    order = sorted(exact, key=lambda d: (-(exact[d] - quotas[d]), d))
    for domain in order[:leftover]:
        quotas[domain] += 1
    return quotas


def assign_splits(tasks, sizes, seed):
    """Domain-stratified random split.

    ``sizes`` maps ``search`` / ``validation`` / ``test`` to exact counts that
    sum to the number of tasks. Search and Validation quotas per domain are
    apportioned in proportion to domain size; Test takes the rest of every
    domain. Inside a domain, tasks are ordered by id, shuffled with a
    per-domain generator derived from ``seed``, and cut in the order Search,
    Validation, Test.
    """
    expected = {name: int(size) for name, size in sizes.items()}
    if set(expected) != {"search", "validation", "test"}:
        raise ValueError("sizes must name exactly search, validation, and test")
    if sum(expected.values()) != len(tasks):
        raise ValueError(
            f"split sizes total {sum(expected.values())}, but {len(tasks)} tasks were supplied"
        )
    by_domain = {}
    for task in tasks:
        by_domain.setdefault(task["domain"], []).append(task["task_id"])
    domain_sizes = {domain: len(ids) for domain, ids in by_domain.items()}
    search_q = domain_quotas(domain_sizes, expected["search"])
    validation_q = domain_quotas(domain_sizes, expected["validation"])
    assigned = {name: {} for name in expected}
    for domain in sorted(by_domain):
        ids = sorted(by_domain[domain])
        s, v = search_q[domain], validation_q[domain]
        if s + v > len(ids):
            raise ValueError(
                f"{domain} has {len(ids)} tasks, fewer than its Search and "
                f"Validation quotas {s} + {v}"
            )
        random.Random(f"{seed}:{domain}").shuffle(ids)
        assigned["search"][domain] = sorted(ids[:s])
        assigned["validation"][domain] = sorted(ids[s : s + v])
        assigned["test"][domain] = sorted(ids[s + v :])
    return {
        name: {domain: ids for domain, ids in sorted(suite.items()) if ids}
        for name, suite in assigned.items()
    }


def _commitment(task_ids):
    payload = json.dumps(sorted(task_ids), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def install_splits(search_path, validation_path, test_path, expected_sizes):
    """Validate the supplied suites and separate the sealed Test ids."""
    suites = {
        "search": load_suite(search_path),
        "validation": load_suite(validation_path),
        "test": load_suite(test_path),
    }
    problems = []
    for name, expected in expected_sizes.items():
        actual = len(suites[name]["task_ids"])
        if actual != int(expected):
            problems.append(f"{name} has {actual} tasks; expected {int(expected)}")
    assigned = {}
    for name, suite in suites.items():
        for task_id in suite["task_ids"]:
            if task_id in assigned:
                problems.append(
                    f"{task_id} appears in both {assigned[task_id]} and {name}"
                )
            assigned[task_id] = name
    if problems:
        raise ValueError("invalid split collection:\n- " + "\n- ".join(problems))

    test_ids = sorted(suites["test"]["task_ids"])
    commitment = _commitment(test_ids)
    created = dt.datetime.now(dt.timezone.utc).isoformat()
    public = {
        "schema_version": SCHEMA,
        "created_at": created,
        "search": {
            "task_ids": sorted(suites["search"]["task_ids"]),
            "domains": suites["search"]["domains"],
        },
        "validation": {
            "task_ids": sorted(suites["validation"]["task_ids"]),
            "domains": suites["validation"]["domains"],
        },
        "test": {
            "count": len(test_ids),
            "commitment_sha256": commitment,
            "opened": False,
            "domains": sorted(suites["test"]["domains"]),
        },
        "sources": {
            name: {"path": str(Path(path).resolve()), "sha256": sha256_file(path)}
            for name, path in (
                ("search", search_path),
                ("validation", validation_path),
                ("test", test_path),
            )
        },
    }
    sealed = {
        "schema_version": SEALED_SCHEMA,
        "created_at": created,
        "commitment_sha256": commitment,
        "task_ids": test_ids,
        "domains": suites["test"]["domains"],
    }
    return public, sealed


def evaluation_suite(public, names):
    """Merge selected public splits into one OSWorld evaluation file."""
    merged = {}
    for name in names:
        for domain, task_ids in (public.get(name) or {}).get("domains", {}).items():
            merged.setdefault(domain, [])
            merged[domain].extend(task_ids)
    return {domain: ids for domain, ids in sorted(merged.items())}


def open_sealed_test(public, sealed, *, stop_reason, behavior_hash):
    ids = sorted(sealed.get("task_ids") or [])
    commitment = _commitment(ids)
    if commitment != (public.get("test") or {}).get("commitment_sha256"):
        raise ValueError("sealed Test commitment does not match public manifest")
    if public["test"].get("opened"):
        raise ValueError("sealed Test is already open")
    opened_at = dt.datetime.now(dt.timezone.utc).isoformat()
    result = json.loads(json.dumps(public))
    result["test"].update(
        {
            "opened": True,
            "opened_at": opened_at,
            "stop_reason": stop_reason,
            "selected_behavior_hash": behavior_hash,
            "task_ids": ids,
            "domains": sealed.get("domains") or {},
        }
    )
    return result
