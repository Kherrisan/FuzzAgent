"""Tool registry: the same YAML that feeds the legacy runtime feeds MCP.

The legacy code compiled ``src/tools/*/config.yaml`` into OpenAI function
schemas (``APIToolCall.FunctionCall``).  MCP wants the *same* JSON Schema object
under ``inputSchema``; keeping one source of truth is what makes the agent
prompts and tool sets portable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

# Tools implemented as shell functions in src/tools/file/bin/defaults.sh.
SOURCE_TOOL_LIST = {"open", "goto", "find_file", "edit", "scroll_down", "scroll_up"}

# Tools the runtime implements itself (not shell scripts).
ORCHESTRATION_TOOLS = {"send_agent_task", "exit", "observe_current_states"}


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    type: str
    description: str
    required: bool
    redirect: bool = False
    enum: list[str] | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    docstring: str
    arguments: list[ArgumentSpec] = field(default_factory=list)
    timeout: int = 10
    max_output_lines: int = 100
    group: str = ""
    script: str | None = None


def _argument(raw: dict[str, Any]) -> ArgumentSpec:
    return ArgumentSpec(
        name=raw["name"],
        type=raw.get("type", "string"),
        description=raw.get("description", ""),
        required=bool(raw.get("required", False)),
        redirect=bool(raw.get("redirect", False)),
        enum=raw.get("enum"),
    )


def load_tools(tools_dir: Path | str = TOOLS_DIR) -> dict[str, ToolSpec]:
    """Load every ``config.yaml`` under ``tools_dir`` (first name wins)."""
    tools: dict[str, ToolSpec] = {}
    for config in sorted(Path(tools_dir).glob("*/config.yaml")):
        group = config.parent.name
        with config.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        for name, raw in (data.get("tools") or {}).items():
            if name in tools:
                continue
            arguments = [_argument(arg) for arg in (raw.get("arguments") or [])]
            tools[name] = ToolSpec(
                name=name,
                docstring=raw.get("docstring", ""),
                arguments=arguments,
                timeout=int(raw.get("timeout", 10)),
                max_output_lines=int(raw.get("max_output_lines", 100)),
                group=group,
            )
    return tools


def parameters_schema(spec: ToolSpec) -> dict[str, Any]:
    """The exact object the legacy runtime sent as ``function.parameters``."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for argument in spec.arguments:
        prop: dict[str, Any] = {"type": argument.type, "description": argument.description}
        if argument.enum is not None:
            prop["enum"] = list(argument.enum)
        properties[argument.name] = prop
        if argument.required:
            required.append(argument.name)
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": False,
    }


def mcp_tool(spec: ToolSpec) -> dict[str, Any]:
    """MCP ``Tool`` object for one registry entry."""
    return {
        "name": spec.name,
        "description": spec.docstring,
        "inputSchema": parameters_schema(spec),
    }


def select(tools: dict[str, ToolSpec], names: Iterable[str]) -> list[ToolSpec]:
    missing = [name for name in names if name not in tools]
    if missing:
        raise KeyError(f"unknown tools: {', '.join(sorted(missing))}")
    return [tools[name] for name in names]
