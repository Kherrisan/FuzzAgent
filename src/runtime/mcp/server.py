"""Minimal MCP server (stdio, JSON-RPC 2.0, newline framed).

One process per agent is spawned by the coding-agent CLI.  Every tool call
enters ``ToolRuntime``.  ``send_agent_task`` and ``wait_agent_task`` are
manager-only and then go through ``dispatcher`` into the orchestrator.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import artifacts, guards
from ..executor import run_tool
from ..registry import ToolSpec, mcp_tool, parameters_schema

MANAGER_ROLE = "ManagerAgent"
Dispatcher = Callable[[str, dict[str, Any]], str]
_TASK_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "fuzzagent-mcp", "version": "0.1.0"}


def _task_lock(task_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _TASK_LOCKS.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _TASK_LOCKS[task_id] = lock
        return lock


@dataclass
class ToolRuntime:
    role: str
    paths: artifacts.ProjectPaths
    tools: dict[str, ToolSpec]
    task_id: str
    dispatcher: Dispatcher | None = None
    calls: list[str] = field(default_factory=list)
    guard: guards.GuardContext | None = None

    @property
    def task_dir(self) -> Path:
        if not self.task_id:
            raise RuntimeError("task_id is not set")
        root = self.paths.campaign or self.paths.output / ".campaign"
        return root / "tasks" / self.task_id

    def load_history(self) -> None:
        path = self.task_dir / "tools.jsonl"
        names: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            self.calls = names
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            tool = item.get("tool")
            if isinstance(tool, str):
                names.append(tool)
        self.calls = names

    def record_call(self, name: str, arguments: dict[str, Any]) -> None:
        with _task_lock(self.task_id):
            self.calls.append(name)
            self.task_dir.mkdir(parents=True, exist_ok=True)
            with (self.task_dir / "tools.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"at": time.time(), "tool": name, "arguments": arguments},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def guard_context(self) -> guards.GuardContext:
        if self.guard is None or self.guard.tool_calls is not self.calls:
            self.guard = guards.GuardContext(
                role=self.role, paths=self.paths, tool_calls=self.calls, task_id=self.task_id
            )
            guards.pre_task(self.guard)
        return self.guard

    def mark_finish(self, report: str, forced: bool, allowed: bool) -> None:
        with _task_lock(self.task_id):
            self.task_dir.mkdir(parents=True, exist_ok=True)
            (self.task_dir / "report.md").write_text(report, encoding="utf-8")
            (self.task_dir / "finish_task.json").write_text(
                json.dumps(
                    {"report": report, "forced": forced, "allowed": allowed, "at": time.time()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.tools:
            return _text_result(f"Unknown tool: {name}", is_error=True)
        if name in {"send_agent_task", "wait_agent_task"}:
            return self._dispatch_tool(name, arguments)
        if name == "finish_task":
            return self._finish_task(arguments)
        if name == "observe_current_states":
            self.record_call(name, arguments)
            return _text_result(artifacts.observe_current_states(self.paths))
        return self._run_script(name, arguments)

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.role != MANAGER_ROLE:
            return _text_result(f"Only {MANAGER_ROLE} may call {name}.", is_error=True)
        if self.dispatcher is None:
            return _text_result("No dispatcher configured for this role.", is_error=True)
        self.record_call(name, arguments)
        try:
            text = self.dispatcher(name, arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return _text_result(f"dispatch failed: {type(exc).__name__}: {exc}", is_error=True)
        return _text_result(str(text), is_error=str(text).startswith("dispatch failed"))

    def _finish_task(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # The same task lock covers the history append and the guard decision.
        with _task_lock(self.task_id):
            forced = bool(arguments.get("force", False))
            report = str(arguments.get("exit_reason", ""))
            self.record_call("finish_task", arguments)
            ctx = self.guard_context()
            if forced:
                final = guards.post_report(ctx, report)
                self.mark_finish(final, forced=True, allowed=True)
                return _text_result(
                    "Finish accepted (forced). The task is complete; do not call more tools.\n" + final
                )
            allowed, reminder = guards.check_exit(ctx)
            if not allowed:
                return _text_result(
                    "Finish rejected. Fix the issue, then call finish_task again.\n" + (reminder or "")
                )
            final = guards.post_report(ctx, report)
            self.mark_finish(final, forced=False, allowed=True)
            return _text_result("Finish accepted. The task is complete; do not call more tools.\n" + final)

    def _run_script(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.tools[name]
        self.record_call(name, arguments)
        try:
            result = run_tool(spec, arguments, self.paths, self.role, self.task_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return _text_result(f"{type(exc).__name__}: {exc}", is_error=True)
        if result.exit_code == 0:
            self._note_stamp(name, arguments)
        return _text_result(result.as_text(), is_error=result.exit_code not in (0, None))

    def _note_stamp(self, name: str, arguments: dict[str, Any]) -> None:
        if name == "run_and_check_build_script":
            guards.record_build_stamp(self.paths, self.task_id, self.paths.build_dir / "build.sh")
        elif name == "compile_and_check_harness" and "harness_id" in arguments:
            try:
                harness = guards.harness_source(self.paths, arguments["harness_id"])
            except (TypeError, ValueError):
                return
            guards.record_harness_stamp(self.paths, self.task_id, harness)


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(runtime: ToolRuntime, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return runtime.call(name, arguments)


def tool_list(runtime: ToolRuntime) -> list[dict[str, Any]]:
    return [mcp_tool(runtime.tools[name]) for name in runtime.tools]


class McpServer:
    def __init__(self, runtime: ToolRuntime):
        self.runtime = runtime

    # --- protocol ---------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return self._ok(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._ok(msg_id, {})
        if method == "tools/list":
            return self._ok(msg_id, {"tools": tool_list(self.runtime)})
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            return self._ok(msg_id, call_tool(self.runtime, name, arguments))
        return self._error(msg_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def serve(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()
        return 0


def current_task_path(paths: artifacts.ProjectPaths, role: str) -> Path:
    root = paths.campaign or paths.output / ".campaign"
    return root / "agents" / role / "current_task"


def read_task_id(paths: artifacts.ProjectPaths, role: str) -> str:
    marker = current_task_path(paths, role)
    try:
        task_id = marker.read_text(encoding="utf-8").strip()
    except OSError:
        task_id = ""
    if not task_id:
        print(f"current_task is missing for {role}: {marker}", file=sys.stderr)
        raise SystemExit(1)
    return task_id


def load_campaign_record(paths: artifacts.ProjectPaths):
    from ..config import CampaignConfig

    record = paths.campaign or paths.output / ".campaign"
    data = json.loads((record / "campaign.json").read_text(encoding="utf-8"))
    return CampaignConfig(
        name=data["name"],
        target=data["target"],
        src=data["src"],
        output=data["output"],
        session=data["session"],
        agents_dir=data["agents_dir"],
        base_dir=data.get("base_dir"),
        snapshot_enabled=bool(data.get("snapshot_enabled", False)),
    )


def open_orchestrator(paths: artifacts.ProjectPaths, hc=None):
    from ..config import load_agents, render_agent
    from ..orchestrator import Orchestrator

    campaign = load_campaign_record(paths)
    context = {"OUTPUT": campaign.output, "SRC": campaign.src}
    agents = {
        role: render_agent(agent, campaign)
        for role, agent in load_agents(campaign.agents_dir, context).items()
    }
    return Orchestrator(campaign, agents, hc=hc)


def make_dispatcher(role: str, paths: artifacts.ProjectPaths) -> Dispatcher:
    if role != MANAGER_ROLE:
        def reject(tool_name: str, arguments: dict[str, Any]) -> str:
            del arguments
            return f"Only {MANAGER_ROLE} may call {tool_name}."

        return reject

    def forward(tool_name: str, arguments: dict[str, Any]) -> str:
        orchestrator = open_orchestrator(paths)
        if tool_name == "send_agent_task":
            return orchestrator.dispatch(
                str(arguments.get("agent_name", "")),
                str(arguments.get("task_description", "")),
                wait=False,
            )
        if tool_name == "wait_agent_task":
            return orchestrator.poll_task(str(arguments.get("task_id", "")))
        raise ValueError(f"dispatcher cannot run {tool_name}")

    return forward


def build_runtime(
    role: str,
    task_id: str,
    dispatcher: Dispatcher | None,
    campaign_dir: Path | None = None,
    paths: artifacts.ProjectPaths | None = None,
) -> ToolRuntime:
    paths = paths or artifacts.ProjectPaths.from_env()
    from ..config import load_agent

    config_path = (campaign_dir or paths.campaign or Path(".campaign")) / "agents" / f"{role}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"rendered role config not found: {config_path}")
    agent = load_agent(config_path)
    runtime = ToolRuntime(
        role=role,
        paths=paths,
        tools=_load_role_tools(agent.tools.enabled),
        task_id=task_id,
        dispatcher=dispatcher,
    )
    runtime.load_history()
    return runtime


def _load_role_tools(names: list[str]) -> dict[str, ToolSpec]:
    from ..registry import TOOLS_DIR, load_tools

    registry = load_tools(TOOLS_DIR)
    missing = [name for name in names if name not in registry]
    if missing:
        raise KeyError(f"unknown tools for role: {', '.join(sorted(missing))}")
    return {name: registry[name] for name in names}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="fuzzagent-mcp", description="MCP server for one agent role")
    parser.add_argument("--role", required=True)
    args = parser.parse_args(argv)
    paths = artifacts.ProjectPaths.from_env()
    task_id = read_task_id(paths, args.role)
    runtime = build_runtime(args.role, task_id, make_dispatcher(args.role, paths), paths=paths)
    return McpServer(runtime).serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
