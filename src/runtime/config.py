"""Configuration model for the hc-driven runtime.

One ``campaign.yaml`` describes the campaign; one ``agents/<Role>.yaml`` per
role describes that agent independently (driver, model, prompt, tools, MCP,
session policy, timeouts, snapshots).

Everything is plain dataclasses so the runtime can be imported without pulling
in the legacy pydantic/OpenAI stack.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

DRIVERS = {"codex", "dsh-tui", "grok", "pi", "generic"}
STATUS_MODES = {"native", "none"}
SNAPSHOT_MODES = {"artifacts", "full", "none"}
SESSION_POLICIES = {"resume", "fresh"}
DELIVERIES = {"first_send", "agents_md", "cli_flag"}


class ConfigError(ValueError):
    """Raised when a campaign/agent definition is unusable."""


def _expand(value: Any, context: dict[str, str]) -> Any:
    """Expand ``${VAR}`` from the context and the process environment."""
    if isinstance(value, str):
        merged = {**os.environ, **context}

        def lookup(match: "re.Match[str]") -> str:
            key = match.group(1)
            return merged.get(key, match.group(0))

        import re

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lookup, value)
    if isinstance(value, list):
        return [_expand(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, context) for key, item in value.items()}
    return value


@dataclass
class PromptSpec:
    source: str = ""
    response_format: bool = True
    guidance_variant: str | None = None
    delivery: str = "first_send"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PromptSpec":
        spec = cls(**{k: v for k, v in data.items() if k in cls.__annotations__})
        if spec.delivery not in DELIVERIES:
            raise ConfigError(f"prompt.delivery must be one of {sorted(DELIVERIES)}")
        return spec


@dataclass
class ToolsSpec:
    include: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> list[str]:
        disabled = set(self.disable)
        return [name for name in self.include if name not in disabled]


@dataclass
class McpSpec:
    transport: str = "stdio"
    server: str = "fuzzagent-mcp"
    role_arg: str | None = None


@dataclass
class SessionSpec:
    policy: str = "resume"
    id_source: list[str] = field(default_factory=lambda: ["index_file", "probe"])
    reset_on: list[str] = field(default_factory=list)
    reset_rounds: int = 0


@dataclass
class DispatchSpec:
    block_seconds: int = 0
    poll_seconds: int = 30


@dataclass
class SnapshotSpec:
    on: str = "done"
    mode: str = "artifacts"


@dataclass
class AgentConfig:
    role: str
    driver: str
    launch: list[str]
    home: str
    cwd: str
    prompt: PromptSpec
    tools: ToolsSpec
    model: str | None = None
    provider: str | None = None
    enabled: bool = True
    description: str = ""
    env: dict[str, str] = field(default_factory=dict)
    env_scrub: list[str] = field(
        default_factory=lambda: [
            "HERDR_ENV",
            "HERDR_BIN_PATH",
            "HERDR_PANE_ID",
            "HERDR_SOCKET_PATH",
            "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID",
        ]
    )
    mcp: McpSpec = field(default_factory=McpSpec)
    session: SessionSpec = field(default_factory=SessionSpec)
    dispatch: DispatchSpec = field(default_factory=DispatchSpec)
    snapshot: SnapshotSpec = field(default_factory=SnapshotSpec)
    status: str = "native"
    terminal_name: str | None = None
    card: str | None = None
    max_parallel: int = 1
    source_path: str | None = None

    def validate(self) -> None:
        if self.driver not in DRIVERS:
            raise ConfigError(f"{self.role}: unknown driver {self.driver!r}")
        if not self.launch:
            raise ConfigError(f"{self.role}: launch must not be empty")
        if self.status not in STATUS_MODES:
            raise ConfigError(f"{self.role}: status must be one of {sorted(STATUS_MODES)}")
        if self.session.policy not in SESSION_POLICIES:
            raise ConfigError(f"{self.role}: session.policy must be one of {sorted(SESSION_POLICIES)}")
        if not self.tools.include:
            raise ConfigError(f"{self.role}: tools.include must not be empty")
        if not self.prompt.source:
            raise ConfigError(f"{self.role}: prompt.source is required")
        if ":" not in self.prompt.source:
            raise ConfigError(f"{self.role}: prompt.source must look like 'module:ATTR'")


@dataclass
class CampaignConfig:
    name: str
    target: str
    src: str
    output: str
    session: str
    duration_hours: float = 24.0
    coverage_guidance: bool = True
    engine: str = "hc"
    manager_role: str = "ManagerAgent"
    agents_dir: str = "config/agents"
    hc: dict[str, Any] = field(default_factory=dict)
    max_rounds: int = 0
    round_prompt_file: str | None = None
    base_dir: str | None = None

    @property
    def campaign_dir(self) -> Path:
        return Path(self.output) / ".campaign"

    def validate(self) -> None:
        if self.engine not in {"hc", "legacy"}:
            raise ConfigError("engine must be 'hc' or 'legacy'")
        if not Path(self.src).exists() or not Path(self.output).exists():
            raise ConfigError(f"src/output must exist: {self.src} / {self.output}")
        if not Path(self.agents_dir).is_dir():
            raise ConfigError(f"agents_dir not found: {self.agents_dir}")


def _agent_from_dict(data: dict[str, Any], source_path: str | None = None) -> AgentConfig:
    known = {f for f in AgentConfig.__dataclass_fields__}
    payload = {
        key: value
        for key, value in data.items()
        if key in known
        and key
        not in {
            "prompt",
            "tools",
            "mcp",
            "session",
            "dispatch",
            "snapshot",
            "agent",
        }
    }
    payload.setdefault("role", data.get("name") or data.get("role") or "")
    payload.setdefault("driver", "generic")
    payload.setdefault("launch", [])
    payload.setdefault("home", "")
    payload.setdefault("cwd", "")
    payload["launch"] = list(payload.get("launch") or [])
    agent = AgentConfig(
        prompt=PromptSpec.from_dict(data.get("prompt") or {}),
        tools=ToolsSpec(
            **{
                k: list(v)
                for k, v in (data.get("tools") or {}).items()
                if k in {"include", "disable"}
            }
        ),
        mcp=McpSpec(
            **{k: v for k, v in (data.get("mcp") or {}).items() if k in McpSpec.__dataclass_fields__}
        ),
        session=SessionSpec(
            **{
                k: v
                for k, v in (data.get("session") or {}).items()
                if k in SessionSpec.__dataclass_fields__
            }
        ),
        dispatch=DispatchSpec(
            **{
                k: v
                for k, v in (data.get("dispatch") or {}).items()
                if k in DispatchSpec.__dataclass_fields__
            }
        ),
        snapshot=SnapshotSpec(
            **{
                k: v
                for k, v in (data.get("snapshot") or {}).items()
                if k in SnapshotSpec.__dataclass_fields__
            }
        ),
        source_path=source_path,
        **payload,
    )
    agent.validate()
    return agent


def load_agent(path: str | Path, context: dict[str, str] | None = None) -> AgentConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data = _expand(data, context or {})
    return _agent_from_dict(data, str(path))


def load_agents(agents_dir: str | Path, context: dict[str, str] | None = None) -> dict[str, AgentConfig]:
    agents: dict[str, AgentConfig] = {}
    for path in sorted(Path(agents_dir).glob("*.yaml")):
        agent = load_agent(path, context)
        if not agent.enabled:
            continue
        if agent.role in agents:
            raise ConfigError(f"duplicate role {agent.role} in {path}")
        agents[agent.role] = agent
    return agents


def load_campaign(path: str | Path) -> CampaignConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    context = {
        "CAMPAIGN_FILE": str(path),
        "REPO_ROOT": str(path.resolve().parent.parent),
    }
    data = _expand(data, context)
    known = {f for f in CampaignConfig.__dataclass_fields__}
    campaign = CampaignConfig(**{k: v for k, v in data.items() if k in known})
    if campaign.base_dir is None:
        campaign.base_dir = str(path.parent.parent)
    campaign.agents_dir = _expand(campaign.agents_dir, {"REPO_ROOT": campaign.base_dir})
    campaign.validate()
    return campaign


def agent_context(campaign: CampaignConfig, agent: AgentConfig) -> dict[str, str]:
    """Variables available to ``${...}`` inside agent definitions."""
    home = agent.home or str(Path(campaign.output) / ".campaign" / "agents" / agent.role)
    cwd = agent.cwd or str(Path(home) / "work")
    return {
        "OUTPUT": campaign.output,
        "SRC": campaign.src,
        "HOME": home,
        "CWD": cwd,
        "CAMPAIGN": campaign.name,
        "SESSION": campaign.session,
        "ROLE": agent.role,
    }


def render_agent(agent: AgentConfig, campaign: CampaignConfig) -> AgentConfig:
    """Apply campaign-scoped path expansion to one agent."""
    context = {
        "OUTPUT": campaign.output,
        "SRC": campaign.src,
        "CAMPAIGN": campaign.name,
        "SESSION": campaign.session,
        "ROLE": agent.role,
    }
    payload = {
        "home": _expand(agent.home, context),
        "cwd": _expand(agent.cwd, context),
        "launch": _expand(agent.launch, context),
        "env": _expand(agent.env, context),
        "role": agent.role,
        "driver": agent.driver,
        "model": agent.model,
        "provider": agent.provider,
        "enabled": agent.enabled,
        "description": agent.description,
        "env_scrub": agent.env_scrub,
        "status": agent.status,
        "terminal_name": agent.terminal_name,
        "card": agent.card,
        "max_parallel": agent.max_parallel,
        "source_path": agent.source_path,
        "prompt": agent.prompt,
        "tools": agent.tools,
        "mcp": agent.mcp,
        "session": agent.session,
        "dispatch": agent.dispatch,
        "snapshot": agent.snapshot,
    }
    return AgentConfig(**payload)
