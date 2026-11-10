"""Protocol-level tests for the stdio MCP server (no coding-agent CLI needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.artifacts import ProjectPaths
from src.runtime.mcp.server import McpServer, ToolRuntime, call_tool
from src.runtime.registry import load_tools


@pytest.fixture()
def runtime(tmp_path: Path) -> ToolRuntime:
    src = tmp_path / "src"
    out = tmp_path / "out" / "cjson"
    for directory in ("harnesses", "fuzzers", "dictionary", "seeds", "coverage_guidance", "crash_reports", ".cache"):
        (out / directory).mkdir(parents=True)
    src.mkdir(parents=True)
    tools = load_tools()
    return ToolRuntime(
        role="HarnessAgent",
        paths=ProjectPaths(src=src, output=out, campaign=out / ".campaign"),
        tools={name: tools[name] for name in ("bash", "exit")},
        task_id="task-1",
    )


def test_initialize_and_tools_list(runtime: ToolRuntime):
    server = McpServer(runtime)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}

    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {"bash", "exit"}
    assert listed["result"]["tools"][0]["inputSchema"]["type"] == "object"


def test_bash_tool_runs_and_reports_exit_code(runtime: ToolRuntime):
    result = call_tool(runtime, "bash", {"command": "echo hello-from-mcp"})
    text = result["content"][0]["text"]
    assert "hello-from-mcp" in text
    assert text.startswith("Command executed with exit code 0.")
    assert result["isError"] is False


def test_exit_is_vetoed_until_harness_is_compiled(runtime: ToolRuntime):
    rejected = call_tool(runtime, "exit", {"exit_reason": "done", "force": False})
    assert "Exit rejected" in rejected["content"][0]["text"]
    assert not (runtime.task_dir / "report.md").exists()

    runtime.calls.append("compile_and_check_harness")
    accepted = call_tool(runtime, "exit", {"exit_reason": "compiled fine", "force": False})
    assert "Exit accepted" in accepted["content"][0]["text"]
    assert (runtime.task_dir / "report.md").read_text() == "compiled fine"


def test_forced_exit_bypasses_guard(runtime: ToolRuntime):
    result = call_tool(runtime, "exit", {"exit_reason": "stuck", "force": True})
    assert "forced" in result["content"][0]["text"].lower()


def test_server_streams_newline_delimited_json(runtime: ToolRuntime):
    import io

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
    )
    stdout = io.StringIO()
    assert McpServer(runtime).serve(stdin=stdin, stdout=stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["id"] for line in lines] == [1, 2]
