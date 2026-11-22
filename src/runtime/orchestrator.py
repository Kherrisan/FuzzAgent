"""Task dispatch between the manager and the role terminals.

This is the piece that replaces ``ManagerAgent.handle_agent_action``: it writes
the task, wakes the target agent through hc, waits for the turn to end, reads
the report the agent submitted through the ``finish_task`` tool and
returns the same ``report + observe_current_states`` string the legacy manager
saw.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artifacts, auth, guards, prompts
from .config import AgentConfig, CampaignConfig
from .drivers import driver_for
from .hc import HC, HCError, Terminal, screen_command


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_FAIL_MARKERS = (
    "Traceback",
    "ModuleNotFoundError",
    "ImportError",
    "No such file",
    "Permission denied",
    "error:",
    "Error:",
    "could not",
)
_CHROME_PREFIXES = ("›", "│", "╭", "╰", "[hc]", "? for shortcuts")
_BOX_CHARS = "╭╰│─╮╯┌└┐┘"


def last_agent_message(screen: str, limit: int = 2000) -> str:
    """Best-effort extraction of the agent's last words from an hc screen dump."""
    text = _ANSI_RE.sub("", screen)
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in _BOX_CHARS:
            continue
        if stripped.startswith(_CHROME_PREFIXES):
            continue
        kept.append(stripped)
    if not kept:
        return "(no message on screen)"
    return "\n".join(kept[-12:])[-limit:]


def looks_failed(screen: str) -> bool:
    text = _ANSI_RE.sub("", screen)
    return any(marker in text for marker in _FAIL_MARKERS)


@dataclass
class TaskRecord:
    task_id: str
    role: str
    started: float
    finished: float | None = None
    status: str = "running"
    report: str = ""
    terminal: str = ""
    terminal_name: str = ""
    started_turn: int = 0


class Orchestrator:
    def __init__(self, campaign: CampaignConfig, agents: dict[str, AgentConfig], hc: HC | None = None, observer=None):
        self.campaign = campaign
        self.agents = agents
        self.hc = hc or HC(str(campaign.hc.get("binary", "hc")), campaign.session)
        self.campaign_dir = Path(campaign.output) / ".campaign"
        self.paths = artifacts.ProjectPaths(Path(campaign.src), Path(campaign.output), self.campaign_dir)
        self._counter = 0
        self.observer = observer or (lambda: artifacts.observe_current_states(self.paths))
        self.auth_spec = auth.load_auth_spec(campaign.auth_config)

    # --- layout -----------------------------------------------------------
    def role_dir(self, role: str) -> Path:
        return self.campaign_dir / "agents" / role

    def state_path(self, role: str) -> Path:
        return self.role_dir(role) / "state.json"

    def task_dir(self, task_id: str) -> Path:
        return self.campaign_dir / "tasks" / task_id

    def read_state(self, role: str) -> dict[str, Any]:
        try:
            return json.loads(self.state_path(role).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def write_state(self, role: str, **updates: Any) -> None:
        state = self.read_state(role)
        state.update(updates)
        path = self.state_path(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)

    def journal(self, event: dict[str, Any]) -> None:
        self.campaign_dir.mkdir(parents=True, exist_ok=True)
        event = {"at": time.time(), **event}
        with (self.campaign_dir / "journal.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    # --- terminals --------------------------------------------------------
    def terminal_name(self, agent: AgentConfig) -> str:
        return agent.terminal_name or agent.role.lower()

    def observe_agent_states(self) -> str:
        """JSON report of every agent's herdr-core terminal and its screen command."""
        terminals = self.hc.list_all()
        by_role: dict[str, list[Terminal]] = {}
        for terminal in terminals:
            labels = terminal.raw.get("labels") or {}
            role = labels.get("fuzzagent.role")
            if role:
                by_role.setdefault(str(role), []).append(terminal)

        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for role, agent in self.agents.items():
            expected = self.terminal_name(agent)
            matches = list(by_role.get(role, []))
            if not matches:
                matches = [
                    terminal
                    for terminal in terminals
                    if terminal.name == expected or terminal.address == expected
                ]
            if not matches:
                rows.append(self._agent_state_row(role, expected, None))
                continue
            for terminal in matches:
                seen.add(terminal.address or terminal.name or "")
                rows.append(self._agent_state_row(role, terminal.name or expected, terminal))
        for role, group in by_role.items():
            if role in self.agents:
                continue
            for terminal in group:
                key = terminal.address or terminal.name or ""
                if key in seen:
                    continue
                rows.append(self._agent_state_row(role, terminal.name or role.lower(), terminal))
        return json.dumps(
            {"session": getattr(self.hc, "session", None), "agents": rows},
            ensure_ascii=False,
            indent=2,
        )

    def _agent_state_row(self, role: str, name: str, terminal: Terminal | None) -> dict[str, Any]:
        binary = str(getattr(self.hc, "binary", "hc"))
        env = dict(getattr(self.hc, "env", {}) or {})
        command = screen_command(binary, name, env)
        if terminal is None:
            return {
                "role": role,
                "name": name,
                "launched": False,
                "state": "absent",
                "status": "absent",
                "turn": 0,
                "screen_command": command,
            }
        return {
            "role": role,
            "name": terminal.name or name,
            "address": terminal.address,
            "launched": True,
            "state": terminal.state,
            "status": terminal.state_word,
            "turn": terminal.turn,
            "reported": terminal.reported,
            "exit_code": terminal.exit_code,
            "blocked_message": terminal.blocked_message,
            "cwd": terminal.cwd,
            "screen_command": command,
        }

    def prepare_role(self, agent: AgentConfig) -> tuple[Path, Path]:
        home = Path(agent.home)
        cwd = Path(agent.cwd)
        home.mkdir(parents=True, exist_ok=True)
        cwd.mkdir(parents=True, exist_ok=True)
        rendered = self.campaign_dir / "agents" / f"{agent.role}.yaml"
        rendered.parent.mkdir(parents=True, exist_ok=True)
        if agent.source_path and Path(agent.source_path).exists():
            shutil.copyfile(agent.source_path, rendered)
        driver_for(agent).prepare(agent, self.campaign, home, cwd)
        report = auth.materialize(agent.role, home, self.auth_spec)
        if report.copied or report.missing:
            self.journal({"event": "auth_materialized", **report.as_dict()})
        return home, cwd


    def ensure_terminal(self, agent: AgentConfig) -> Terminal:
        name = self.terminal_name(agent)
        terminal = self.hc.get(name)
        expected = Path(agent.launch[0]).name
        if terminal and terminal.is_live:
            running = str(terminal.raw.get("agent") or "")
            if running and running != expected:
                # ``--keep`` substitutes a shell when the program is lost;
                # treat that as "needs a fresh terminal".
                self.hc.kill(name)
                self.hc.remove(name)
                terminal = None
        if terminal and terminal.is_live:
            if terminal.state_word == "working":
                terminal = self.hc.wait_idle(name, timeout=int(agent.dispatch.poll_seconds) * 4)
            return terminal
        if terminal:
            self.hc.remove(name)

        home, cwd = self.prepare_role(agent)
        env = dict(agent.env)
        env["HOME"] = str(home)
        if agent.driver == "dsh-tui":
            env.setdefault("DSH_HOME", str(home / ".dsh"))
        terminal = self.hc.run_bg(
            name=name,
            argv=driver_for(agent).launch(agent),
            cwd=str(cwd),
            env=env,
            labels={
                "fuzzagent.campaign": self.campaign.name,
                "fuzzagent.role": agent.role,
                "fuzzagent.kind": "agent" if agent.role != self.campaign.manager_role else "manager",
                "herdr.space": agent.role,
            },
            keep=bool(self.campaign.hc.get("keep", True)),
            outlive_parent=bool(self.campaign.hc.get("outlive_parent", True)),
            scrub_env=agent.env_scrub,
        )
        self.write_state(agent.role, terminal=terminal.address, name=name)
        for _ in range(3):
            if not terminal.is_blocked:
                break
            self.journal({"event": "dialog_auto_ok", "role": agent.role, "msg": terminal.blocked_message})
            self.hc.keys(name, "enter")
            time.sleep(2)
            terminal = self.hc.wait_idle(name, timeout=60)
        self.journal({"event": "terminal_launched", "role": agent.role, "terminal": terminal.address})
        return self.hc.wait_idle(name, timeout=180)

    # --- dispatch ---------------------------------------------------------
    def new_task_id(self, role: str) -> str:
        self._counter += 1
        return f"{role}-{int(time.time())}-{self._counter}"

    def dispatch(self, role: str, task: str, *, wait: bool = True) -> str:
        agent = self.agents.get(role)
        if agent is None or not agent.enabled:
            raise ValueError(f"unknown or disabled agent {role!r}")

        task_id = self.new_task_id(role)
        directory = self.task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)

        ctx = guards.GuardContext(role=role, paths=self.paths, task_id=task_id)
        pre = guards.pre_task(ctx)
        body = (pre.get("note", "") + "\n\n" + task).strip()
        task_file = directory / "task.md"
        task_file.write_text(task, encoding="utf-8")
        (directory / "task_with_context.md").write_text(body, encoding="utf-8")
        marker = self.role_dir(role) / "current_task"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(task_id, encoding="utf-8")

        name = self.terminal_name(agent)
        if not wait:
            existing = self.hc.get(name)
            if existing is not None:
                self.hc.kill(name)
                self.hc.remove(name)

        terminal = self.ensure_terminal(agent)
        state = self.read_state(role)
        primed = bool(state.get("primed"))
        context_file = directory / "task_with_context.md"
        done = " Call the finish_task tool when the task is done."
        if primed:
            message = f"Read your next task from {context_file} and execute it.{done}"
        else:
            prime_file = self.role_dir(role) / "prime.md"
            prime_file.write_text(
                prompts.compose_first_message(agent, self.campaign, body), encoding="utf-8"
            )
            message = (
                f"Your role instructions are in {prime_file}; read that file completely and follow it. "
                f"Your first task is in {context_file}.{done}"
            )
        previous_turn = terminal.turn
        record = TaskRecord(
            task_id=task_id,
            role=role,
            started=time.time(),
            terminal=terminal.address,
            terminal_name=name,
            started_turn=previous_turn,
        )
        self.journal({"event": "dispatch", "task": task_id, "role": role, "task_text": task[:400]})

        try:
            sent = self.hc.send(name, message, if_not_working=True)
            if sent.get("started") is False:
                raise HCError(f"send to {role} did not start: {sent}")
            self.write_state(role, primed=True)
            if not wait:
                self._write_running(record, directory)
                return f"Dispatched {role} as {task_id} on terminal {name}."
            terminal = self.hc.wait_turn(
                name,
                previous_turn=previous_turn,
                timeout=int(agent.dispatch.block_seconds or 24 * 3600),
            )
        except HCError as exc:
            record.status = "failed"
            record.report = f"dispatch failed: {exc}"
            self._finish(record, directory, "failed", report=record.report)
            raise

        status, report = self.collect_result(directory, agent, terminal=terminal, name=name)
        self._finish(record, directory, status, report=report)
        if status == "done":
            return self._legacy_response(role, report)
        return f"Task {task_id} finished with status {status}:\n{report}"

    # --- task outcome -----------------------------------------------------
    def _read_finish(self, directory: Path) -> dict[str, Any] | None:
        try:
            data = json.loads((directory / "finish_task.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _read_meta(self, directory: Path) -> dict[str, Any]:
        try:
            data = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def collect_result(
        self,
        directory: Path,
        agent: AgentConfig,
        terminal: Terminal | None = None,
        name: str | None = None,
    ) -> tuple[str, str]:
        """Return ``(status, report)``.

        A ``finish_task.json`` wins; otherwise the exit word, blocked message or
        the last thing the agent said on screen decides -- a turn that ended
        without finish_task is ``blocked`` (or ``failed`` when the screen shows
        an error), never a silent "done".
        """
        finish = self._read_finish(directory)
        if finish is not None:
            return str(finish.get("status") or "done"), str(finish.get("report", ""))
        screen = self.hc.read(name or self.terminal_name(agent), 200)
        (directory / "screen.log").write_text(screen, encoding="utf-8")
        message = last_agent_message(screen)
        if terminal is not None:
            if terminal.is_blocked:
                return "blocked", (terminal.blocked_message or message or "blocked")
            if terminal.state_word == "error" or not terminal.is_live:
                return "failed", (message or f"terminal ended (exit_code={terminal.exit_code})")
        if looks_failed(screen):
            return "failed", message
        return "blocked", f"[no finish_task call recorded] last agent message:\n{message}"

    def collect_report(self, directory: Path, agent: AgentConfig) -> str:
        """Backwards-compatible view of :meth:`collect_result`."""
        return self.collect_result(directory, agent)[1]

    def poll_task(self, task_id: str) -> str:
        directory = self.task_dir(task_id)
        meta = self._read_meta(directory)
        if not meta and self._read_finish(directory) is None:
            # Nothing recorded for this task yet: it was just dispatched from
            # another process, so there is nothing to judge.
            return f"Task {task_id} is not finished."
        role = str(meta.get("role") or task_id.split("-", 1)[0])

        finish = self._read_finish(directory)
        if finish is not None:
            return self._finished_response(
                task_id, role, str(finish.get("status") or "done"), str(finish.get("report", ""))
            )
        if meta.get("status") in {"done", "blocked", "failed"}:
            return self._task_status_text(task_id, str(meta["status"]), str(meta.get("report", "")))

        agent = self.agents.get(role)
        address = str(meta.get("terminal") or "")
        name = str(meta.get("terminal_name") or (self.terminal_name(agent) if agent else role.lower()))
        terminal = None
        for target in (address, name):
            if not target:
                continue
            try:
                terminal = self.hc.get(target)
            except Exception:  # noqa: BLE001 - hc may be down
                terminal = None
            if terminal is not None:
                break

        if terminal is None:
            return self._finalize(task_id, role, "failed", "terminal is gone before the task finished")
        if not terminal.is_live:
            return self._finalize(
                task_id, role, "failed", f"terminal finished (exit_code={terminal.exit_code})"
            )

        word = terminal.state_word
        started = float(meta.get("started") or 0.0)
        elapsed = time.time() - started if started else 0.0
        timeout = int(getattr(agent.dispatch, "stuck_seconds", 0) or 0) if agent else 0
        timeout = timeout or 7200

        if word == "working":
            if elapsed > timeout:
                try:
                    self.hc.interrupt(name)
                except Exception:  # noqa: BLE001
                    pass
                return self._finalize(
                    task_id, role, "failed", f"stuck for {int(elapsed)}s; interrupted"
                )
            return f"Task {task_id} is not finished."
        if word == "blocked":
            return self._finalize(
                task_id, role, "blocked", terminal.blocked_message or "blocked waiting for a person"
            )
        if word == "error":
            screen = self.hc.read(name, 200)
            (directory / "screen.log").write_text(screen, encoding="utf-8")
            return self._finalize(task_id, role, "failed", last_agent_message(screen))

        started_turn = meta.get("started_turn")
        ended = terminal.is_idle and (started_turn is None or terminal.turn > int(started_turn))
        if not ended:
            return f"Task {task_id} is not finished."

        screen = self.hc.read(name, 200)
        (directory / "screen.log").write_text(screen, encoding="utf-8")
        message = last_agent_message(screen)
        status = "failed" if looks_failed(screen) else "blocked"
        return self._finalize(task_id, role, status, message)

    def _finalize(self, task_id: str, role: str, status: str, report: str) -> str:
        """Close a task detected by polling. Idempotent; never fakes finish_task.json."""
        directory = self.task_dir(task_id)
        meta = self._read_meta(directory)
        if meta.get("status") not in {"done", "blocked", "failed"}:
            meta.update(
                task_id=task_id,
                role=role,
                status=status,
                finished=time.time(),
                report=report,
                detected_by="poll_task",
            )
            (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            (directory / "report.md").write_text(report, encoding="utf-8")
            self.journal(
                {
                    "event": "finished",
                    "task": task_id,
                    "role": role,
                    "status": status,
                    "report": report[:400],
                    "detected_by": "poll_task",
                }
            )
        return self._task_status_text(task_id, status, report)

    def _finished_response(self, task_id: str, role: str, status: str, report: str) -> str:
        if status == "done":
            return self._legacy_response(role, report)
        return self._task_status_text(task_id, status, report)

    def _task_status_text(self, task_id: str, status: str, report: str) -> str:
        if status == "done":
            return report
        return f"Task {task_id} is {status}:\n{report}"

    def _write_running(self, record: TaskRecord, directory: Path) -> None:
        record.status = "running"
        meta = {
            "task_id": record.task_id,
            "role": record.role,
            "status": "running",
            "started": record.started,
            "terminal": record.terminal,
            "terminal_name": record.terminal_name,
            "started_turn": record.started_turn,
        }
        (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _finish(self, record: TaskRecord, directory: Path, status: str, report: str | None = None) -> None:
        record.status = status
        if report is not None:
            record.report = report
        record.finished = time.time()
        meta = {
            "task_id": record.task_id,
            "role": record.role,
            "status": status,
            "started": record.started,
            "finished": record.finished,
            "terminal": record.terminal,
            "terminal_name": record.terminal_name,
            "started_turn": record.started_turn,
            "report": record.report,
            "detected_by": "dispatch",
        }
        (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.journal(
            {
                "event": "finished",
                "task": record.task_id,
                "role": record.role,
                "status": status,
                "report": record.report[:400],
                "detected_by": "dispatch",
            }
        )

    def _legacy_response(self, role: str, report: str) -> str:
        observation = self.observer()
        message = (
            f"This is the current states of the fuzzing tasks after the agent {role} "
            f"has completed its task:\n{observation}"
        )
        return report + "\n" + message

    # --- resume -----------------------------------------------------------
    def interrupted_tasks(self) -> list[dict[str, Any]]:
        tasks = []
        for meta in sorted((self.campaign_dir / "tasks").glob("*/meta.json")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("status") == "running":
                tasks.append(data)
        return tasks

    def resume_brief(self, limit: int = 10) -> str:
        lines = [f"# Campaign resume brief: {self.campaign.name}", ""]
        lines.append(f"- Output: {self.campaign.output}")
        lines.append(f"- Harnesses: {len(self.paths.harness_ids())} {self.paths.harness_ids()}")
        states = self.paths.fuzzer_states()
        summary: dict[str, int] = {}
        for state in states.values():
            summary[state] = summary.get(state, 0) + 1
        lines.append(f"- Fuzzers: {len(states)} {summary}")
        lines.append(f"- Guidance files: {len(self.paths.list_guidance())}")
        lines.append(f"- Crash reports: {len(self.paths.list_crash_reports())}")

        interrupted = self.interrupted_tasks()
        if interrupted:
            lines.append("")
            lines.append("## Tasks interrupted by the restart (re-dispatch if still needed)")
            for task in interrupted:
                lines.append(f"- {task.get('task_id')} ({task.get('role')}), started {task.get('started')}")

        journal = self.campaign_dir / "journal.jsonl"
        if journal.exists():
            recent = []
            for line in journal.read_text(encoding="utf-8").splitlines()[-limit:]:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "finished":
                    recent.append(f"- {event.get('task')} ({event.get('role')}): {event.get('status')}")
            if recent:
                lines.append("")
                lines.append("## Recent tasks")
                lines.extend(recent)

        lines.append("")
        lines.append("Continue the campaign from this state; re-observe before dispatching.")
        return "\n".join(lines)
