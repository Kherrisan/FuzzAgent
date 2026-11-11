"""Task dispatch between the manager and the role terminals.

This is the piece that replaces ``ManagerAgent.handle_agent_action``: it writes
the task, wakes the target agent through hc, waits for the turn to end, reads
the report the agent submitted through the ``finish_task`` tool, snapshots the tree and
returns the same ``report + observe_current_states`` string the legacy manager
saw.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artifacts, auth, guards, prompts
from .config import AgentConfig, CampaignConfig
from .drivers import driver_for
from .hc import HC, HCError, Terminal

os.environ.setdefault("FUZZAGENT_SKIP_ENV_INIT", "1")


@dataclass
class TaskRecord:
    task_id: str
    role: str
    started: float
    finished: float | None = None
    status: str = "running"
    report: str = ""
    terminal: str = ""
    snapshot: str | None = None


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
        self._ensure_snapshot_ignores()
        return home, cwd

    def _ensure_snapshot_ignores(self) -> None:
        """Never commit per-role credential/session stores into VMS snapshots."""
        gitignore = Path(self.campaign.output) / ".gitignore"
        entries = [".campaign/agents/*/home/", ".campaign/agents/*/auth/"]
        try:
            existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            missing = [entry for entry in entries if entry not in existing.splitlines()]
            if missing:
                with gitignore.open("a", encoding="utf-8") as handle:
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    handle.write("\n".join(missing) + "\n")
        except OSError:
            pass

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
        if primed:
            message = (
                f"Read your next task from {context_file} and execute it. "
                "Call the finish_task tool when the task is done."
            )
        else:
            prime_file = self.role_dir(role) / "prime.md"
            prime_file.write_text(
                prompts.compose_first_message(agent, self.campaign, body), encoding="utf-8"
            )
            message = (
                f"Your role instructions are in {prime_file}; read that file completely and follow it. "
                f"Your first task is in {context_file}. Call the finish_task tool when the task is done."
            )
        previous_turn = terminal.turn
        record = TaskRecord(task_id=task_id, role=role, started=time.time(), terminal=terminal.address)
        self.journal({"event": "dispatch", "task": task_id, "role": role, "task_text": task[:400]})

        try:
            sent = self.hc.send(name, message, if_idle=True)
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
            status = "blocked" if terminal.is_blocked else ("failed" if terminal.state_word == "error" else "done")
        except HCError as exc:
            status = "failed"
            record.report = f"dispatch failed: {exc}"
            self._finish(record, directory, status)
            raise

        if terminal.is_blocked:
            record.report = f"Agent is blocked waiting for a person: {terminal.blocked_message}"
            (directory / "screen.log").write_text(self.hc.read(name, 200), encoding="utf-8")
        else:
            record.report = self.collect_report(directory, agent)

        self._finish(record, directory, status)
        return self._legacy_response(role, record.report)

    def poll_task(self, task_id: str) -> str:
        """Return immediately. A missing finish file means the child is not done."""
        directory = self.task_dir(task_id)
        finish = directory / "finish_task.json"
        try:
            data = json.loads(finish.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return f"Task {task_id} is not finished."
        role = task_id.split("-", 1)[0]
        try:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            role = str(meta.get("role") or role)
        except (OSError, json.JSONDecodeError):
            pass
        return self._legacy_response(role, str(data.get("report", "")))

    def collect_report(self, directory: Path, agent: AgentConfig) -> str:
        finish_file = directory / "finish_task.json"
        try:
            data = json.loads(finish_file.read_text(encoding="utf-8"))
            return str(data.get("report", ""))
        except (OSError, json.JSONDecodeError):
            pass
        screen = self.hc.read(self.terminal_name(agent), 200)
        (directory / "screen.log").write_text(screen, encoding="utf-8")
        tail = "\n".join(screen.splitlines()[-40:])
        return f"[no finish_task call recorded; terminal tail]\n{tail}"

    def _write_running(self, record: TaskRecord, directory: Path) -> None:
        record.status = "running"
        meta = {
            "task_id": record.task_id,
            "role": record.role,
            "status": "running",
            "started": record.started,
            "terminal": record.terminal,
        }
        (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _finish(self, record: TaskRecord, directory: Path, status: str) -> None:
        record.status = status
        record.finished = time.time()
        meta = {
            "task_id": record.task_id,
            "role": record.role,
            "status": status,
            "started": record.started,
            "finished": record.finished,
            "terminal": record.terminal,
        }
        (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if status == "done" and self.campaign.snapshot_enabled:
            try:
                _, commit = artifacts.snapshot(Path(self.campaign.output), f"{record.role} {record.task_id}")
                record.snapshot = commit
                meta["snapshot"] = commit
                (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            except Exception as exc:  # pragma: no cover - git identity/missing repo
                self.journal({"event": "snapshot_failed", "task": record.task_id, "error": str(exc)})
        self.journal(
            {
                "event": "finished",
                "task": record.task_id,
                "role": record.role,
                "status": status,
                "report": record.report[:400],
                "snapshot": record.snapshot,
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
