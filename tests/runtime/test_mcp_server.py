"""Protocol-level tests for the stdio MCP server (no coding-agent CLI needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.artifacts import ProjectPaths
from src.runtime.guards import record_harness_stamp
from src.runtime.mcp.server import (
    McpServer,
    ToolRuntime,
    build_runtime,
    call_tool,
    main,
    make_dispatcher,
    read_task_id,
)
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
        tools={name: tools[name] for name in ("bash", "finish_task")},
        task_id="task-1",
    )


def test_initialize_and_tools_list(runtime: ToolRuntime):
    server = McpServer(runtime)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}

    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {"bash", "finish_task"}
    assert listed["result"]["tools"][0]["inputSchema"]["type"] == "object"


def test_bash_tool_runs_and_reports_exit_code(runtime: ToolRuntime):
    result = call_tool(runtime, "bash", {"command": "echo hello-from-mcp"})
    text = result["content"][0]["text"]
    assert "hello-from-mcp" in text
    assert text.startswith("Command executed with exit code 0.")
    assert result["isError"] is False


def test_finish_task_is_vetoed_until_harness_is_compiled(runtime: ToolRuntime):
    rejected = call_tool(runtime, "finish_task", {"exit_reason": "done", "force": False})
    assert "Finish rejected" in rejected["content"][0]["text"]
    assert not (runtime.task_dir / "report.md").exists()
    assert not (runtime.task_dir / "finish_task.json").exists()

    source = runtime.paths.harness_dir / "harness_000.cpp"
    source.write_text("int LLVMFuzzerTestOneInput(){return 0;}\n")
    record_harness_stamp(runtime.paths, runtime.task_id, source)
    runtime.calls.append("compile_and_check_harness")
    accepted = call_tool(runtime, "finish_task", {"exit_reason": "compiled fine", "force": False})
    assert "Finish accepted" in accepted["content"][0]["text"]
    assert (runtime.task_dir / "report.md").read_text() == "compiled fine"
    assert (runtime.task_dir / "finish_task.json").exists()


def test_forced_finish_bypasses_guard(runtime: ToolRuntime):
    result = call_tool(runtime, "finish_task", {"exit_reason": "stuck", "force": True})
    assert "forced" in result["content"][0]["text"].lower()
    assert (runtime.task_dir / "finish_task.json").exists()


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


def test_finish_history_stays_on_its_own_task(runtime: ToolRuntime):
    source = runtime.paths.harness_dir / "harness_000.cpp"
    source.write_text("int LLVMFuzzerTestOneInput(){return 0;}\n")
    record_harness_stamp(runtime.paths, runtime.task_id, source)
    runtime.record_call("compile_and_check_harness", {"harness_id": 0})

    reloaded = ToolRuntime(
        role=runtime.role,
        paths=runtime.paths,
        tools=runtime.tools,
        task_id=runtime.task_id,
    )
    reloaded.load_history()
    accepted = call_tool(reloaded, "finish_task", {"exit_reason": "ok", "force": False})
    assert "Finish accepted" in accepted["content"][0]["text"]

    other = ToolRuntime(
        role=runtime.role,
        paths=runtime.paths,
        tools=runtime.tools,
        task_id="task-2",
    )
    other.load_history()
    rejected = call_tool(other, "finish_task", {"exit_reason": "ok", "force": False})
    assert "Finish rejected" in rejected["content"][0]["text"]
    assert not (other.task_dir / "finish_task.json").exists()


def test_only_manager_reaches_the_dispatcher(runtime: ToolRuntime):
    tools = load_tools()
    selected = {name: tools[name] for name in ("send_agent_task", "wait_agent_task", "finish_task")}
    calls: list[str] = []

    def spy(name: str, arguments: dict) -> str:
        calls.append(name)
        return "should-not-run"

    builder = ToolRuntime(
        role="BuilderAgent",
        paths=runtime.paths,
        tools=selected,
        task_id="builder-1",
        dispatcher=spy,
    )
    refused = call_tool(
        builder,
        "send_agent_task",
        {"agent_name": "SeedAgent", "task_description": "collect seeds"},
    )
    assert refused["isError"] is True
    assert "Only ManagerAgent may call send_agent_task." in refused["content"][0]["text"]
    waiting = call_tool(builder, "wait_agent_task", {"task_id": "seed-1"})
    assert waiting["isError"] is True
    assert calls == []

    manager = ToolRuntime(
        role="ManagerAgent",
        paths=runtime.paths,
        tools=selected,
        task_id="manager-1",
        dispatcher=None,
    )
    missing = call_tool(
        manager,
        "send_agent_task",
        {"agent_name": "BuilderAgent", "task_description": "build"},
    )
    assert "No dispatcher configured for this role." in missing["content"][0]["text"]


def test_manager_dispatcher_enters_orchestrator_dispatch(runtime: ToolRuntime, monkeypatch):
    seen: dict = {}

    class FakeOrchestrator:
        def dispatch(self, role, task, *, wait=True):
            seen.update(role=role, task=task, wait=wait)
            return f"Dispatched {role} as {role}-1 on terminal {role.lower()}."

        def poll_task(self, task_id):
            seen["polled"] = task_id
            return f"Task {task_id} is not finished."

    monkeypatch.setattr(
        "src.runtime.mcp.server.open_orchestrator",
        lambda paths, hc=None: FakeOrchestrator(),
    )
    tools = load_tools()
    manager = ToolRuntime(
        role="ManagerAgent",
        paths=runtime.paths,
        tools={name: tools[name] for name in ("send_agent_task", "wait_agent_task", "finish_task")},
        task_id="manager-1",
        dispatcher=make_dispatcher("ManagerAgent", runtime.paths),
    )
    sent = call_tool(
        manager,
        "send_agent_task",
        {"agent_name": "BuilderAgent", "task_description": "build it"},
    )
    assert seen == {"role": "BuilderAgent", "task": "build it", "wait": False}
    assert "Dispatched BuilderAgent as BuilderAgent-1" in sent["content"][0]["text"]
    assert sent["isError"] is False
    waiting = call_tool(manager, "wait_agent_task", {"task_id": "BuilderAgent-1"})
    assert seen["polled"] == "BuilderAgent-1"
    assert "Task BuilderAgent-1 is not finished." in waiting["content"][0]["text"]


def _write_role_yaml(campaign: Path, role: str) -> None:
    directory = campaign / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{role}.yaml").write_text(
        f"role: {role}\ndriver: generic\nlaunch: [x]\nhome: /tmp/h\ncwd: /tmp/w\n"
        "prompt: {source: 'm:A'}\ntools: {include: [finish_task]}\n",
        encoding="utf-8",
    )


def test_read_task_id_requires_current_task(tmp_path: Path):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    paths = ProjectPaths(src=src, output=out, campaign=out / ".campaign")
    with pytest.raises(SystemExit) as exc:
        read_task_id(paths, "BuilderAgent")
    assert exc.value.code == 1
    assert not (out / ".campaign" / "tasks" / "adhoc").exists()


def test_main_exits_when_current_task_is_missing(tmp_path: Path, monkeypatch, capsys):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    monkeypatch.setenv("SRC", str(src))
    monkeypatch.setenv("OUTPUT", str(out))
    with pytest.raises(SystemExit) as exc:
        main(["--role", "BuilderAgent"])
    assert exc.value.code == 1
    assert "current_task is missing" in capsys.readouterr().err
    assert not (out / ".campaign" / "tasks" / "adhoc").exists()


def test_main_passes_current_task_into_toolruntime(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    campaign = out / ".campaign"
    _write_role_yaml(campaign, "BuilderAgent")
    marker = campaign / "agents" / "BuilderAgent"
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "current_task").write_text("BuilderAgent-7\n")
    monkeypatch.setenv("SRC", str(src))
    monkeypatch.setenv("OUTPUT", str(out))
    captured: dict = {}

    def serve(self, stdin=None, stdout=None):
        captured["runtime"] = self.runtime
        return 0

    monkeypatch.setattr(McpServer, "serve", serve)
    assert main(["--role", "BuilderAgent"]) == 0
    runtime = captured["runtime"]
    assert runtime.task_id == "BuilderAgent-7"
    assert runtime.role == "BuilderAgent"
    assert "finish_task" in runtime.tools
    built = build_runtime("BuilderAgent", "BuilderAgent-7", runtime.dispatcher, paths=runtime.paths)
    assert built.task_id == "BuilderAgent-7"
