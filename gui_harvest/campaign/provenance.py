"""Small, secret-free provenance manifest for an GUI-HARVEST analyst run."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import sys
import collections

SCHEMA_VERSION = "gui-harvest-analyst-v1"
SENSITIVE_CONFIG_KEYS = ("api_key", "secret", "password", "authorization", "access_token")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


def source_fingerprint(source_dir):
    files = {}
    for name in sorted(os.listdir(source_dir)):
        path = os.path.join(source_dir, name)
        if name.endswith(".py") and os.path.isfile(path):
            files[name] = sha256_file(path)
    return {"fingerprint": sha256_bytes(json.dumps(files, sort_keys=True).encode()), "files": files}


def _public_config(config):
    result = dict(config)
    for key in list(result):
        if any(marker in key.lower() for marker in SENSITIVE_CONFIG_KEYS):
            result.pop(key)
    return result


def create_manifest(
    source_dir, sample_path, harness_card_path, config, prompt, result_roots,
    harness_card_text="",
):
    created = dt.datetime.now(dt.timezone.utc).isoformat()
    public_config = _public_config(config)
    seed = json.dumps(
        {"created_at": created, "sample": sha256_file(sample_path), "config": public_config},
        sort_keys=True,
    ).encode()
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": sha256_bytes(seed)[:16],
        "created_at": created,
        "updated_at": created,
        "gui_harvest_source": source_fingerprint(source_dir),
        "harness_card_sha256": (
            sha256_file(harness_card_path)
            or sha256_bytes(str(harness_card_text).encode())
        ),
        "sample_sha256": sha256_file(sample_path),
        "config_sha256": sha256_bytes(json.dumps(public_config, sort_keys=True).encode()),
        "config": public_config,
        "result_roots": [os.path.abspath(p) for p in result_roots],
        "prompt_sha256": sha256_bytes(prompt.encode()),
        "runtime": {"python": sys.version, "platform": platform.platform()},
        "summary": {
            "tasks_written": 0,
            "analysis_status": {"ok": 0, "no_attributable_failure": 0, "dropout": 0},
            "tokens": {"in": 0, "out": 0},
            "retries": 0,
        },
    }


def create_stage_manifest(source_dir, stage, input_paths, config, prompt):
    """Provenance for a non-Analyst stage such as clustering."""
    created = dt.datetime.now(dt.timezone.utc).isoformat()
    config = _public_config(config)
    inputs = {os.path.abspath(path): sha256_file(path) for path in input_paths}
    seed = json.dumps(
        {"created_at": created, "stage": stage, "inputs": inputs, "config": config},
        sort_keys=True,
    ).encode()
    return {
        "schema_version": f"gui-harvest-{stage}-provenance-v1",
        "manifest_id": sha256_bytes(seed)[:16],
        "created_at": created,
        "updated_at": created,
        "stage": stage,
        "gui_harvest_source": source_fingerprint(source_dir),
        "inputs": inputs,
        "config": config,
        "config_sha256": sha256_bytes(json.dumps(config, sort_keys=True).encode()),
        "prompt_sha256": sha256_bytes(prompt.encode()),
        "runtime": {"python": sys.version, "platform": platform.platform()},
    }


def update_manifest(manifest, row):
    summary = manifest["summary"]
    summary["tasks_written"] += 1
    status = row.get("analysis_status") or "dropout"
    summary["analysis_status"][status] = summary["analysis_status"].get(status, 0) + 1
    for key in ("in", "out"):
        summary["tokens"][key] += int((row.get("tokens") or {}).get(key) or 0)
    summary["retries"] += int(row.get("retries") or 0)
    manifest["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()


def write_manifest(path, manifest):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def write_dataset_manifest(findings_path, out_dir):
    """Aggregate invocation manifests for the current, possibly resumed JSONL."""
    rows = []
    with open(findings_path, encoding="utf-8") as source:
        rows = [json.loads(line) for line in source if line.strip()]
    rows_by_manifest = collections.Counter(row.get("manifest_id") or "unrecorded" for row in rows)
    invocations = {}
    for manifest_id in rows_by_manifest:
        if manifest_id == "unrecorded":
            continue
        path = os.path.join(out_dir, f"manifest_{manifest_id}.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as source:
                invocations[manifest_id] = json.load(source)
    attempt_tokens = collections.Counter()
    attempt_tasks = 0
    for manifest in invocations.values():
        summary = manifest.get("summary") or {}
        attempt_tasks += int(summary.get("tasks_written") or 0)
        attempt_tokens.update(summary.get("tokens") or {})
    dataset = {
        "schema_version": "gui-harvest-analyst-dataset-provenance-v1",
        "findings_path": os.path.abspath(findings_path),
        "findings_sha256": sha256_file(findings_path),
        "final_rows": len(rows),
        "final_status": dict(collections.Counter(
            row.get("analysis_status") or "unrecorded" for row in rows
        )),
        "final_findings": sum(len(row.get("findings") or []) for row in rows),
        "final_dropped": sum(len(row.get("dropped") or []) for row in rows),
        "rows_by_manifest": dict(rows_by_manifest),
        "invocation_manifests": {
            manifest_id: {
                "path": os.path.abspath(os.path.join(out_dir, f"manifest_{manifest_id}.json")),
                "sha256": sha256_file(os.path.join(out_dir, f"manifest_{manifest_id}.json")),
                "source_fingerprint": (
                    manifest.get("gui_harvest_source") or {}
                ).get("fingerprint"),
            }
            for manifest_id, manifest in invocations.items()
        },
        "api_attempt_totals": {
            "tasks": attempt_tasks,
            "tokens": dict(attempt_tokens),
        },
    }
    write_manifest(os.path.join(out_dir, "dataset_manifest.json"), dataset)
    return dataset
