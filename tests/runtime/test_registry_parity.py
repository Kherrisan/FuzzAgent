"""The MCP surface must stay byte-identical to the legacy OpenAI schemas."""

from __future__ import annotations

import os

os.environ.setdefault("FUZZAGENT_SKIP_ENV_INIT", "1")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from src.runtime.registry import load_tools, mcp_tool, parameters_schema  # noqa: E402
from src.tools.APIToolCall import API_TOOL_CALLS  # noqa: E402


def _legacy() -> dict[str, dict]:
    return {entry["function"]["name"]: entry["function"] for entry in API_TOOL_CALLS.tool_calls}


def test_registry_covers_every_legacy_tool():
    legacy = _legacy()
    tools = load_tools()
    assert set(tools) == set(legacy), (
        f"missing={sorted(set(legacy) - set(tools))} extra={sorted(set(tools) - set(legacy))}"
    )


def test_schema_and_description_match_legacy():
    legacy = _legacy()
    for name, spec in load_tools().items():
        entry = legacy[name]
        assert entry["description"] == spec.docstring, name
        assert entry["parameters"] == parameters_schema(spec), name
        assert mcp_tool(spec)["inputSchema"] == parameters_schema(spec), name


def test_timeouts_and_output_limits_are_preserved():
    tools = load_tools()
    assert tools["run_and_check_build_script"].timeout == 7200
    assert tools["run_fuzz_target"].timeout == 3900
    assert tools["compile_and_check_harness"].timeout == 1200
