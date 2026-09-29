"""Offline decisive-step quote audit for GUI-HARVEST findings."""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
from gui_harvest.roles.analyst import _canon
from gui_harvest.evidence.trajectory import load_trajectory
from gui_harvest.evidence.bundle import raw_step_text, step_no


def rendered_step_regions(evidence):
    """Recover detailed RUN/STEP blocks from the exact text shown previously."""
    regions = []
    run = step = None
    lines = []

    def flush():
        if run is not None and step is not None and lines:
            regions.append({"run": run, "step": step, "region": "step", "layer": "rendered", "text": "\n".join(lines)})

    for line in str(evidence or "").splitlines():
        m_run = re.match(r"=+\s+RUN\s+([A-Z0-9]+)\s+=+", line)
        if m_run:
            flush()
            run, step, lines = m_run.group(1), None, []
            continue
        if run is None:
            continue
        m_step = re.match(r"\[(\d+)\]\s", line)
        if m_step:
            flush()
            step, lines = int(m_step.group(1)), [line]
            continue
        if line.startswith("COUNTS OVER RUN") or line.startswith("================"):
            flush()
            run, step, lines = None, None, []
            continue
        if step is not None:
            lines.append(line)
    flush()
    return regions


def _matches(quote, regions):
    cq = _canon(quote).rstrip("…").strip()
    if len(cq) < 8:
        return []
    out = []
    for region in regions:
        hay = _canon(region.get("text"))
        if cq in hay:
            out.append({
                "run": region.get("run"), "step": region.get("step"),
                "region": region.get("region"), "layer": region.get("layer"),
                "shown_to_analyst": region.get("layer") == "rendered",
                "match": "canonical_exact",
            })
    return out


def audit_finding(finding, regions, resolvable=True):
    run, ds = str(finding.get("run") or "").upper(), finding.get("decisive_step")
    quotes = []
    shown_local = raw_local = False
    for quote in finding.get("quotes") or []:
        locations = _matches(quote, regions)
        shown_local = shown_local or any(
            x["run"] == run and x["step"] == ds and x["layer"] == "rendered"
            for x in locations
        )
        raw_local = raw_local or any(
            x["run"] == run and x["step"] == ds and x["layer"] == "raw"
            for x in locations
        )
        quotes.append({"quote": quote, "locations": locations})
    shown_resolvable = any(x.get("layer") == "rendered" for x in regions)
    if shown_local:
        verdict = "pass"
    elif shown_resolvable and raw_local:
        verdict = "raw_only"
    elif shown_resolvable:
        verdict = "fail"
    else:
        verdict = "unresolvable"
    return {
        "finding_id": finding.get("finding_id"),
        "task_id": finding.get("task_id"),
        "domain": finding.get("domain"),
        "outcome_category": finding.get("outcome_category"),
        "label": finding.get("label"),
        "run": run,
        "decisive_step": ds,
        "verdict": verdict,
        "quotes": quotes,
    }


def raw_regions_for_task(task, run_labels, repo_root):
    """Read trajectory text only; offline auditing must not require Pillow."""
    regions = []
    for run_name, label in run_labels.items():
        task_dir = (task.get("task_dirs") or {}).get(run_name)
        if not task_dir:
            continue
        if not os.path.isabs(task_dir):
            task_dir = os.path.join(repo_root, task_dir)
        traj = load_trajectory(task_dir, run_name, None, "")
        for step in traj.steps:
            regions.append({
                "run": label, "step": step_no(step), "region": "step",
                "layer": "raw", "text": raw_step_text(step),
            })
    return regions


def audit_files(findings_path, sample_path, repo_root, raw_dir=None):
    with open(sample_path, encoding="utf-8") as fh:
        sample = json.load(fh)
    tasks = {t["task_id"]: t for t in sample.get("tasks") or []}
    labels = sample["run_labels"]
    results = []
    with open(findings_path, encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            task = tasks.get(row.get("task_id"))
            regions, resolvable = [], False
            if task:
                try:
                    raw_regions = raw_regions_for_task(task, labels, repo_root)
                    regions.extend(raw_regions)
                    resolvable = bool(raw_regions)
                except (OSError, ValueError, IndexError, KeyError):
                    pass
            raw_path = os.path.join(raw_dir, f"raw_{row['task_id'][:8]}.json") if raw_dir else ""
            if raw_path and os.path.isfile(raw_path):
                try:
                    with open(raw_path, encoding="utf-8") as fh:
                        raw = json.load(fh)
                    shown_regions = rendered_step_regions(raw.get("evidence"))
                    regions.extend(shown_regions)
                    resolvable = resolvable or bool(shown_regions)
                except (OSError, json.JSONDecodeError):
                    pass
            for finding in row.get("findings") or []:
                results.append(audit_finding(finding, regions, resolvable))
    counts = dict(collections.Counter(x["verdict"] for x in results))
    by_outcome_category = {}
    by_domain = {}
    for field, target in (("outcome_category", by_outcome_category), ("domain", by_domain)):
        for value in sorted({str(x.get(field) or "unknown") for x in results}):
            target[value] = dict(collections.Counter(
                x["verdict"] for x in results if str(x.get(field) or "unknown") == value
            ))
    return {"schema_version": "gui-harvest-quote-audit-v1", "findings_path": os.path.abspath(findings_path),
            "sample_path": os.path.abspath(sample_path), "counts": counts,
            "by_outcome_category": by_outcome_category, "by_domain": by_domain, "findings": results}


def markdown_report(report):
    c = report["counts"]
    lines = [
        "# Quote locality audit",
        "",
        f"- total: {len(report['findings'])}",
        f"- pass: {c.get('pass', 0)}",
        f"- raw-only (present in source but not shown at decisive step): {c.get('raw_only', 0)}",
        f"- fail: {c.get('fail', 0)}",
        f"- unresolvable: {c.get('unresolvable', 0)}",
        "",
        "## Breakdown by outcome category",
        "",
        "| outcome category | pass | raw-only | fail | unresolvable |",
        "|---|---:|---:|---:|---:|",
    ]
    for outcome_category, counts in sorted(report["by_outcome_category"].items()):
        lines.append(
            f"| {outcome_category} | {counts.get('pass', 0)} | {counts.get('raw_only', 0)} | "
            f"{counts.get('fail', 0)} | {counts.get('unresolvable', 0)} |"
        )
    lines += [
        "",
        "## Findings",
        "",
        "| finding | task | run | decisive step | verdict |",
        "|---|---|---:|---:|---|",
    ]
    for item in report["findings"]:
        lines.append(
            f"| {item.get('finding_id') or '?'} | {(item.get('task_id') or '?')[:8]} | "
            f"{item['run']} | {item['decisive_step']} | {item['verdict']} |"
        )
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--findings", required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--raw-dir", default="")
    ap.add_argument("--json-out", required=True)
    ap.add_argument("--md-out", required=True)
    args = ap.parse_args()
    report = audit_files(args.findings, args.sample, args.repo_root, args.raw_dir or None)
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(args.md_out, "w", encoding="utf-8") as fh:
        fh.write(markdown_report(report))
    print(f"audited {len(report['findings'])} findings: {report['counts']}")


if __name__ == "__main__":
    main()
