"""Minimal MCP server (stdio, JSON-RPC 2.0, newline framed).

One process per agent turn is spawned by the coding-agent CLI.  It exposes the
role's tools, records every call for the guards, and turns ``exit`` into a
guard-checked task completion.  Deliberately dependency free: the MCP surface
needed here is ``initialize``, ``tools/list`` and ``tools/call``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import artifacts, guards
from ..executor import run_tool
from ..registry import ToolSpec, mcp_tool, parameters_schema

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "fuzzagent-mcp", "version": "0.1.0"}


@dataclass
class ToolRuntime:
    role: str
    paths: artifacts.ProjectPaths
    tools: dict[str, ToolSpec]
    task_id: str = ""
    dispatcher: Callable[[str, str], str] | None = None
    calls: list[str] = field(default_factory=list)
    guard: guards.GuardContext | None = None

    # --- task bookkeeping -------------------------------------------------
    @property
    def task_dir(self) -> Path:
        root = self.paths.campaign or self.paths.output / ".campaign"
        return root / "tasks" / (self.task_id or "adhoc")

    def ensure_task(self) -> None:
        if not self.task_id:
            marker = (self.paths.campaign or self.paths.output / ".campaign") / "agents" / self.role / "current_task"
            try:
                self.task_id = marker.read_text(encoding="utf-8").strip()
            except OSError:
                self.task_id = f"adhoc-{int(time.time())}"

    def record_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.calls.append(name)
        try:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            with (self.task_dir / "tools.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"at": time.time(), "tool": name, "arguments": arguments},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass

    # --- guard helpers ----------------------------------------------------
    def guard_context(self) -> guards.GuardContext:
        if self.guard is None:
            self.guard = guards.GuardContext(
                role=self.role, paths=self.paths, tool_calls=self.calls, task_id=self.task_id
            )
            guards.pre_task(self.guard)
        return self.guard

    def mark_exit(self, report: str, forced: bool, allowed: bool) -> None:
        try:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            (self.task_dir / "report.md").write_text(report, encoding="utf-8")
            (self.task_dir / "exit.json").write_text(
                json.dumps(
                    {"report": report, "forced": forced, "allowed": allowed, "at": time.time()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(runtime: ToolRuntime, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in runtime.tools:
        return _text_result(f"Unknown tool: {name}", is_error=True)

    spec = runtime.tools[name]

    if name == "exit":
        forced = bool(arguments.get("force", False))
        report = str(arguments.get("exit_reason", ""))
        runtime.record_call(name, arguments)
        ctx = runtime.guard_context()
        if forced:
            final = guards.post_report(ctx, report)
            runtime.mark_exit(final, forced=True, allowed=True)
            return _text_result(
                "Exit accepted (forced). The task is complete; do not call more tools.\n" + final
            )
        allowed, reminder = guards.check_exit(ctx)
        if not allowed:
            return _text_result(
                "Exit rejected. Fix the issue, then call exit again.\n" + (reminder or "")
            )
        final = guards.post_report(ctx, report)
        runtime.mark_exit(final, forced=False, allowed=True)
        return _text_result("Exit accepted. The task is complete; do not call more tools.\n" + final)

    if name == "observe_current_states":
        runtime.record_call(name, arguments)
        output = artifacts.observe_current_states(runtime.paths)
        return _text_result(output)

    if name == "send_agent_task":
        if runtime.dispatcher is None:
            return _text_result("No dispatcher configured for this role.", is_error=True)
        role = str(arguments.get("agent_name", ""))
        task = str(arguments.get("task_description", ""))
        if not role or not task:
            return _text_result("agent_name and task_description are required.", is_error=True)
        runtime.record_call(name, arguments)
        try:
            return _text_result(runtime.dispatcher(role, task))
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return _text_result(f"dispatch failed: {type(exc).__name__}: {exc}", is_error=True)

    runtime.record_call(name, arguments)
    try:
        result = run_tool(spec, arguments, runtime.paths, runtime.role, runtime.task_id)
    except Exception as exc:  # noqa: BLE001 - surfaced to the model
        return _text_result(f"{type(exc).__name__}: {exc}", is_error=True)
    return _text_result(result.as_text(), is_error=result.exit_code not in (0, None))


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


def build_runtime(role: str, campaign_dir: Path | None = None) -> ToolRuntime:
    paths = artifacts.ProjectPaths.from_env()
    from ..config import load_agent

    # campaignctl renders ``home``/``cwd``/``launch`` once and copies the role
    # file into the campaign dir; the MCP process only reads that rendered copy.
    config_path = (campaign_dir or paths.campaign or Path(".campaign")) / "agents" / f"{role}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"rendered role config not found: {config_path}")
    agent = load_agent(config_path)
    return ToolRuntime(role=role, paths=paths, tools=_load_role_tools(agent.tools.enabled))


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
    runtime = build_runtime(args.role)
    return McpServer(runtime).serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
