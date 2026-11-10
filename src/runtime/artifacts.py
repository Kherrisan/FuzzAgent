"""Read-only helpers for the campaign artifact tree (``$OUTPUT``).

The layout is the one the legacy runtime already produces; keeping the same
paths is what lets new agents and old campaigns share artifacts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HARNESS_RE = re.compile(r"^harness_(\d{3})\.cpp$")
FUZZER_RE = re.compile(r"^fuzzer_(\d{3})$")
GUIDANCE_RE = re.compile(r"^harness_(\d{3})_guidance\.md$")

DEFAULT_DICTIONARY_FILE = "dictionary.dict"

# Mirrors src/utils/project.py:192-211 as a plain mapping.
FUZZER_STATES = {
    "NOT_EXECUTED",
    "CRASHED",
    "HARNESS_BUG",
    "LIBRARY_BUG",
    "PLATEAU",
    "FIXED",
    "INTERRUPTED",
}


@dataclass(frozen=True)
class ProjectPaths:
    src: Path
    output: Path
    campaign: Path | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ProjectPaths":
        env = env or os.environ
        src = env.get("SRC")
        output = env.get("OUTPUT")
        if not src or not output:
            raise RuntimeError("SRC and OUTPUT must be set to locate campaign artifacts")
        return cls(Path(src), Path(output), Path(output) / ".campaign")

    # --- directories -----------------------------------------------------
    @property
    def build_dir(self) -> Path:
        return self.output / "build"

    @property
    def harness_dir(self) -> Path:
        return self.output / "harnesses"

    @property
    def fuzzer_dir(self) -> Path:
        return self.output / "fuzzers"

    @property
    def dictionary_dir(self) -> Path:
        return self.output / "dictionary"

    @property
    def seed_dir(self) -> Path:
        return self.output / "seeds"

    @property
    def guidance_dir(self) -> Path:
        return self.output / "coverage_guidance"

    @property
    def crash_report_dir(self) -> Path:
        return self.output / "crash_reports"

    @property
    def cache_dir(self) -> Path:
        return self.output / ".cache"

    @property
    def fuzzer_stat_file(self) -> Path:
        return self.cache_dir / "fuzzer_stat.json"

    def progress_file(self, kind: str) -> Path:
        return self.cache_dir / f"{kind}_progress.json"

    # --- listings --------------------------------------------------------
    def list_harnesses(self) -> list[str]:
        return sorted(p.name for p in self.harness_dir.glob("harness_*.cpp") if p.is_file())

    def harness_ids(self) -> list[int]:
        return sorted(int(m.group(1)) for p in self.list_harnesses() if (m := HARNESS_RE.match(p)))

    def list_fuzzers(self) -> list[str]:
        return sorted(p.name for p in self.fuzzer_dir.glob("fuzzer_*") if p.is_dir())

    def fuzzer_ids(self) -> list[int]:
        return sorted(int(m.group(1)) for p in self.list_fuzzers() if (m := FUZZER_RE.match(p)))

    def list_guidance(self) -> list[str]:
        return sorted(p.name for p in self.guidance_dir.glob("harness_*_guidance.md"))

    def list_crash_reports(self) -> list[str]:
        return sorted(p.name for p in self.crash_report_dir.iterdir()) if self.crash_report_dir.is_dir() else []

    # --- documents -------------------------------------------------------
    def read_json(self, path: Path, default: Any = None) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return default

    def fuzzer_profile(self) -> dict[str, dict[str, Any]]:
        data = self.read_json(self.fuzzer_stat_file, {}) or {}
        return data.get("fuzzers") or {}

    def fuzzer_states(self) -> dict[int, str]:
        profile = self.fuzzer_profile()
        return {int(key): str(value.get("state", "UNKNOWN")) for key, value in profile.items()}

    def progress(self, kind: str) -> dict[str, str]:
        return self.read_json(self.progress_file(kind), {}) or {}

    def incomplete_projects(self, kind: str) -> list[str]:
        return [name for name, status in self.progress(kind).items() if status == "incomplete"]

    def plateau_fuzzers(self) -> list[int]:
        return [fid for fid, state in self.fuzzer_states().items() if state == "PLATEAU"]

    def coverage(self) -> dict[str, Any] | None:
        return self.read_json(self.cache_dir / "introspector" / "final_coverage.json", None)


def observe_current_states(paths: ProjectPaths, timeout: int = 120) -> str:
    """Run the legacy status oracle so its JSON stays the contract."""
    env = dict(os.environ)
    env.update({"SRC": str(paths.src), "OUTPUT": str(paths.output)})
    proc = subprocess.run(
        ["observe_current_states"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(paths.src),
    )
    return (proc.stdout or "") + (proc.stderr or "")


def snapshot(output: Path, message: str) -> tuple[int, str]:
    """Commit the campaign tree with the legacy VMS helper (git based)."""
    from src.utils.vms import VMS

    return VMS(str(output)).create_snapshot(message)
