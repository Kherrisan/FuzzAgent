"""Per-role auth materialisation from a TOML config.

Each coding-agent CLI stores its credentials differently (codex
``.codex/auth.json``, grok ``.grok/auth.json``, pi ``.pi/agent/...``, dsh session
files).  The campaign does not want those files baked into the image, so a TOML
file lists them per role and ``campaignctl prepare`` copies them into that
role's isolated HOME with mode 0600.

Format::

    [HarnessAgent.files]
    ".codex/auth.json" = "${HOST_AUTH_DIR}/codex/auth.json"

The key is the destination relative to the role HOME, the value is a source
readable by the controller (normally a read-only mount such as
``/run/fuzzagent-auth``).  ``${VAR}`` and ``~`` are expanded; a missing source
is reported, never fatal, and file contents are never logged.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AuthFile:
    role: str
    dest: str
    source: str


@dataclass
class AuthReport:
    copied: list[AuthFile] = field(default_factory=list)
    missing: list[AuthFile] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[dict[str, str]]]:
        return {
            "copied": [{"role": f.role, "dest": f.dest} for f in self.copied],
            "missing": [{"role": f.role, "dest": f.dest, "source": f.source} for f in self.missing],
        }


def load_auth_spec(path: str | Path | None) -> dict[str, list[AuthFile]]:
    """Read the TOML file into ``role -> [AuthFile]`` (empty if absent)."""
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    with file.open("rb") as handle:
        data = tomllib.load(handle)

    spec: dict[str, list[AuthFile]] = {}
    for role, block in data.items():
        if not isinstance(block, dict):
            continue
        files = block.get("files") or {}
        if not isinstance(files, dict):
            raise ValueError(f"{file}: [{role}.files] must be a table of dest = source")
        entries = [
            AuthFile(role=role, dest=str(dest), source=os.path.expanduser(os.path.expandvars(str(source))))
            for dest, source in files.items()
        ]
        if entries:
            spec[role] = entries
    return spec


def materialize(role: str, home: Path, spec: dict[str, list[AuthFile]]) -> AuthReport:
    """Copy the role's auth files into ``home`` with restrictive modes."""
    report = AuthReport()
    for entry in spec.get(role, []):
        source = Path(entry.source)
        dest = home / entry.dest
        if not source.is_file():
            report.missing.append(entry)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        report.copied.append(entry)
    return report


def all_destinations(spec: dict[str, list[AuthFile]]) -> list[str]:
    """Destinations, useful for the snapshot ignore rules."""
    return sorted({entry.dest for entries in spec.values() for entry in entries})
