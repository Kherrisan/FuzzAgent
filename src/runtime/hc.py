"""Thin, typed wrapper around the ``hc`` (herdr-core) CLI.

Only the surface the runtime needs is wrapped: launch a terminal, look at its
program-reported status, send text, wait for a turn, read the screen, stop it.
JSON output is used everywhere; the CLI's short ``run`` JSON is normalised into
:class:`Terminal`.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any


class HCError(RuntimeError):
    """An hc invocation failed or returned an unexpected payload."""


@dataclass
class Terminal:
    address: str
    name: str | None = None
    state: str = "unknown"
    status: dict[str, Any] | None = None
    reported: bool = False
    turn: int = 0
    cwd: str | None = None
    exit_code: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def state_word(self) -> str:
        """``status.state`` (idle/working/blocked/error) or the terminal state."""
        if isinstance(self.status, dict) and self.status.get("state"):
            return str(self.status["state"])
        return self.state

    @property
    def is_live(self) -> bool:
        return self.state == "live"

    @property
    def is_idle(self) -> bool:
        return self.state_word in {"idle", "done"}

    @property
    def is_blocked(self) -> bool:
        return self.state_word == "blocked"

    @property
    def blocked_message(self) -> str:
        if isinstance(self.status, dict):
            return str(self.status.get("msg") or "")
        return ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Terminal":
        status = data.get("status")
        return cls(
            address=str(data.get("address", "")),
            name=data.get("name"),
            state=str(data.get("state", "unknown")),
            status=status if isinstance(status, dict) else None,
            reported=bool(data.get("reported", False)),
            turn=int(data.get("turn", 0) or 0),
            cwd=data.get("cwd"),
            exit_code=data.get("exit_code"),
            raw=data,
        )


class HC:
    def __init__(self, binary: str = "hc", session: str | None = None, env: dict[str, str] | None = None):
        self.binary = binary
        self.session = session or os.environ.get("HC_SESSION")
        self.env = dict(os.environ)
        if env:
            self.env.update(env)

    # --- plumbing ---------------------------------------------------------
    def _invoke(self, args: list[str], timeout: int = 60) -> Any:
        argv = [self.binary, *args]
        if self.session and "--session" not in args:
            argv = [self.binary, "--session", self.session, *args]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self.env)
        if proc.returncode != 0:
            raise HCError(
                f"{shlex.join(argv)} failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )
        out = (proc.stdout or "").strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise HCError(f"not JSON from {shlex.join(argv)}: {out[:300]}") from exc

    # --- queries ----------------------------------------------------------
    def list_all(self) -> list[Terminal]:
        data = self._invoke(["ls", "--all", "--json"])
        return [Terminal.from_json(item) for item in data.get("terminals", [])]

    def get(self, name: str) -> Terminal | None:
        for terminal in self.list_all():
            if terminal.name == name or terminal.address == name:
                return terminal
        return None

    def find_by_label(self, **labels: str) -> list[Terminal]:
        wanted = {f"fuzzagent.{k}" if not k.startswith("fuzzagent.") else k: v for k, v in labels.items()}
        matches = []
        for terminal in self.list_all():
            terminal_labels = (terminal.raw.get("labels") or {})
            if all(terminal_labels.get(key) == value for key, value in wanted.items()):
                matches.append(terminal)
        return matches

    # --- lifecycle --------------------------------------------------------
    def run_bg(
        self,
        name: str,
        argv: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        parent: str | None = None,
        keep: bool = True,
        outlive_parent: bool = True,
        scrub_env: list[str] | None = None,
    ) -> Terminal:
        command = list(argv)
        if scrub_env:
            # ``env -u`` keeps the legacy HERDR_* channel from waking up.
            command = ["env", *[f"-u{key}" for key in scrub_env], *command]
        args = ["run", "--bg", "--json", "--name", name]
        if cwd:
            args += ["--cwd", cwd]
        if parent:
            args += ["--parent", parent]
        if keep:
            args.append("--keep")
        if outlive_parent:
            args.append("--outlive-parent")
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        if env:
            command = ["env", *[f"{key}={value}" for key, value in env.items()], *command]
        args += ["--", *command]
        data = self._invoke(args, timeout=60)
        return Terminal.from_json(data)

    def send(self, name: str, text: str, if_idle: bool = True, force: bool = False) -> dict[str, Any]:
        args = ["send"]
        if if_idle:
            args += ["--if", "idle"]
        if force:
            args.append("--force")
        args += [name, text]
        return self._invoke(args, timeout=120)

    def wait(self, name: str, timeout: int = 300) -> dict[str, Any]:
        return self._invoke(["wait", name, "--json", "--timeout", str(timeout)], timeout=timeout + 30)

    def read(self, name: str, lines: int = 200) -> str:
        proc = subprocess.run(
            [self.binary, "--session", self.session, "read", name, "--lines", str(lines)]
            if self.session
            else [self.binary, "read", name, "--lines", str(lines)],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )
        return proc.stdout or proc.stderr or ""

    def kill(self, name: str) -> None:
        try:
            self._invoke(["kill", name], timeout=30)
        except HCError:
            pass

    def remove(self, name: str) -> None:
        try:
            self._invoke(["rm", name], timeout=30)
        except HCError:
            pass

    def label(self, name: str, **labels: str) -> None:
        args = ["label", name]
        args += [f"{key}={value}" for key, value in labels.items()]
        self._invoke(args, timeout=30)

    # --- higher level -----------------------------------------------------
    def wait_idle(self, name: str, timeout: int = 180, poll: int = 5) -> Terminal:
        deadline = time.time() + timeout
        last: Terminal | None = None
        while time.time() < deadline:
            terminal = self.get(name)
            if terminal is None:
                raise HCError(f"terminal {name!r} disappeared")
            last = terminal
            if not terminal.is_live:
                return terminal
            if terminal.state_word in {"idle", "done", "blocked", "error"}:
                return terminal
            time.sleep(poll)
        if last is None:
            raise HCError(f"terminal {name!r} never appeared")
        return last

    def wait_turn(self, name: str, previous_turn: int, timeout: int = 7200, poll: int = 10) -> Terminal:
        """Wait until a sent task leaves ``working`` again."""
        deadline = time.time() + timeout
        seen_working = False
        while time.time() < deadline:
            terminal = self.get(name)
            if terminal is None:
                raise HCError(f"terminal {name!r} disappeared")
            word = terminal.state_word
            if word == "working" or terminal.turn > previous_turn:
                seen_working = True
            if seen_working and word in {"idle", "done", "blocked", "error"}:
                return terminal
            if not terminal.is_live:
                return terminal
            time.sleep(poll)
        raise HCError(f"terminal {name!r} did not finish the turn within {timeout}s")
