#!/usr/bin/env python3
"""Summarise a finished FuzzAgent campaign into a comparable JSON manifest.

The manifest is the frozen reference used by the hc-runtime migration: it
captures the *structure* and the *pacing* of a legacy run (which is what can
still be compared after the agent runtime is replaced), plus the final coverage
numbers when an introspector export exists.

Usage:
    python3 tools/baseline_report.py output/openssl output/gnutls-24h-20261108 \
        --out-dir tests/fixtures/baseline

Only the standard library is used so the script runs with the system python as
well as the project venv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

HARNESS_RE = re.compile(r"^harness_(\d{3})\.cpp$")
FUZZER_RE = re.compile(r"^fuzzer_(\d{3})$")
GUIDANCE_RE = re.compile(r"^harness_(\d{3})_guidance\.md$")
SNAPSHOT_RE = re.compile(r"^(?P<agent>\w+?)(?:\s+\S+)?\s+Snapshot at (?P<at>.+)$")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def list_dir(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def iso(value: str | None) -> str | None:
    if not value:
        return None
    return value


def parse_iso(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def campaign_window(root: Path) -> dict[str, Any]:
    """Derive the wall-clock window from the VMS snapshot index."""
    index = read_json(root / ".vms" / "index.json", {}) or {}
    snapshots = index.get("snapshots") or []
    stamps = [parse_iso(s.get("timestamp", "")) for s in snapshots]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return {"start": None, "end": None, "duration_hours": None}
    start, end = min(stamps), max(stamps)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_hours": round((end - start).total_seconds() / 3600.0, 2),
    }


def snapshot_stats(root: Path) -> dict[str, Any]:
    index = read_json(root / ".vms" / "index.json", {}) or {}
    snapshots = index.get("snapshots") or []
    agents: Counter[str] = Counter()
    for snap in snapshots:
        message = str(snap.get("message", ""))
        match = SNAPSHOT_RE.match(message)
        if match:
            agents[match.group("agent")] += 1
        else:
            agents[message.split(" Snapshot at ")[0] or "unknown"] += 1
    stamps = [parse_iso(s.get("timestamp", "")) for s in snapshots]
    stamps = [s for s in stamps if s is not None]
    return {
        "count": len(snapshots),
        "first": min(stamps).isoformat() if stamps else None,
        "last": max(stamps).isoformat() if stamps else None,
        "by_agent": dict(agents),
    }


def harness_timeline(root: Path) -> list[dict[str, str]]:
    """When each harness first appeared, from the campaign's git snapshots."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root.resolve()}",
                "-C",
                str(root),
                "log",
                "--reverse",
                "--diff-filter=A",
                "--name-only",
                "--pretty=format:C %ad",
                "--date=iso",
                "--",
                "harnesses/",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    timeline: list[dict[str, str]] = []
    seen: set[str] = set()
    stamp: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("C "):
            stamp = line[2:].strip()
            continue
        name = os.path.basename(line.strip())
        match = HARNESS_RE.match(name)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        timeline.append({"id": match.group(1), "at": stamp or ""})
    return timeline


def fallback_timeline(root: Path) -> list[dict[str, str]]:
    harness_dir = root / "harnesses"
    entries: list[tuple[float, str]] = []
    for name in list_dir(harness_dir):
        match = HARNESS_RE.match(name)
        if not match:
            continue
        try:
            stamp = (harness_dir / name).stat().st_mtime
        except OSError:
            continue
        entries.append((stamp, match.group(1)))
    entries.sort()
    return [
        {"id": hid, "at": datetime.fromtimestamp(stamp).isoformat()}
        for stamp, hid in entries
    ]


def collect(root: Path) -> dict[str, Any]:
    harness_ids = [m.group(1) for n in list_dir(root / "harnesses") if (m := HARNESS_RE.match(n))]
    fuzzer_ids = [m.group(1) for n in list_dir(root / "fuzzers") if (m := FUZZER_RE.match(n))]
    guidance_ids = [
        m.group(1) for n in list_dir(root / "coverage_guidance") if (m := GUIDANCE_RE.match(n))
    ]
    crash_reports = list_dir(root / "crash_reports")

    stat = read_json(root / ".cache" / "fuzzer_stat.json", {}) or {}
    fuzzer_stat = stat.get("fuzzers") or {}
    states = Counter(str(item.get("state", "UNKNOWN")) for item in fuzzer_stat.values())

    coverage = read_json(root / ".cache" / "introspector" / "final_coverage.json")

    logs_dir = root / ".cache" / "logs"
    log_files = list_dir(logs_dir)
    log_bytes = 0
    token_lines = 0
    for name in log_files:
        try:
            path = logs_dir / name
            log_bytes += path.stat().st_size
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if "Token" in line:
                        token_lines += 1
        except OSError:
            continue

    timeline = harness_timeline(root) or fallback_timeline(root)
    window = campaign_window(root)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": str(root),
        "campaign": root.name,
        "window": window,
        "harnesses": {
            "count": len(harness_ids),
            "ids": harness_ids,
            "timeline": timeline,
        },
        "fuzzers": {
            "count": len(fuzzer_ids),
            "ids": fuzzer_ids,
            "stat_count": len(fuzzer_stat),
            "states": dict(sorted(states.items())),
        },
        "dictionary": {"files": list_dir(root / "dictionary")},
        "seeds": {"count": len(list_dir(root / "seeds"))},
        "guidance": {"count": len(guidance_ids), "ids": guidance_ids},
        "crash_reports": {"count": len(crash_reports), "ids": crash_reports},
        "coverage": coverage,
        "snapshots": snapshot_stats(root),
        "logs": {"files": len(log_files), "bytes": log_bytes, "token_lines": token_lines},
    }


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="+", help="campaign output directories")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="write one JSON manifest per campaign here (default: stdout)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for raw in args.dirs:
        root = Path(raw)
        if not root.is_dir():
            print(f"skip {raw}: not a directory", file=sys.stderr)
            continue
        manifest = collect(root)
        results.append(manifest)
        if out_dir:
            path = out_dir / f"{slug(root.name)}.json"
            path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"wrote {path}", file=sys.stderr)
        else:
            print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
