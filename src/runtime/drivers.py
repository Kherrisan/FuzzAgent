"""Per-CLI adapters.

Each adapter knows how to launch the interactive agent and where that CLI
wants its MCP server configured.  Everything is best-effort and verified by
the capability probe (``campaignctl doctor``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AgentConfig, CampaignConfig


def _mcp_args(role: str) -> list[str]:
    return ["--role", role]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass
class Driver:
    name: str

    def prepare(self, agent: AgentConfig, campaign: CampaignConfig, home: Path, cwd: Path) -> None:
        """Write MCP config before the terminal is launched."""

    def launch(self, agent: AgentConfig) -> list[str]:
        return list(agent.launch)


class CodexDriver(Driver):
    def __init__(self) -> None:
        super().__init__("codex")

    def prepare(self, agent, campaign, home, cwd) -> None:
        config = home / ".codex" / "config.toml"
        lines = [
            'approval_policy = "never"',
            'sandbox_mode = "danger-full-access"',
            "",
            "[mcp_servers.fuzzagent]",
            'command = "fuzzagent-mcp"',
            f"args = {json.dumps(_mcp_args(agent.role))}",
        ]
        if agent.model:
            lines.insert(0, f'model = "{agent.model}"')
        _write(config, "\n".join(lines) + "\n")
        agents_md = cwd / "AGENTS.md"
        if not agents_md.exists():
            from .prompts import load_system_prompt

            _write(agents_md, load_system_prompt(agent, campaign))


class GrokDriver(Driver):
    def __init__(self) -> None:
        super().__init__("grok")

    def prepare(self, agent, campaign, home, cwd) -> None:
        if "--always-approve" not in agent.launch:
            agent.launch = [*agent.launch, "--always-approve"]
        # Pre-trust the role working directories so the TUI does not stop on
        # "Trust this folder?" before the first task.
        trust = home / ".grok" / "trust.json"
        trusted = []
        if trust.exists():
            try:
                trusted = json.loads(trust.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                trusted = []
        if not isinstance(trusted, list):
            trusted = []
        for path in {str(cwd), str(Path(campaign.output) / ".campaign" / "agents" / agent.role)}:
            if path not in trusted:
                trusted.append(path)
        trust.parent.mkdir(parents=True, exist_ok=True)
        trust.write_text(json.dumps(trusted, indent=2), encoding="utf-8")
        config = home / ".grok" / "config.toml"
        _write(
            config,
            "[mcp_servers.fuzzagent]\n"
            'command = "fuzzagent-mcp"\n'
            f"args = {json.dumps(_mcp_args(agent.role))}\n",
        )


class DshTuiDriver(Driver):
    def __init__(self) -> None:
        super().__init__("dsh-tui")

    def prepare(self, agent, campaign, home, cwd) -> None:
        self._ensure_profile(home)
        patch = home / "dsh-mcp.patch.yml"
        _write(
            patch,
            "- id: fuzzagent-mcp\n"
            '  name: "@deepseek-ai/dsh-mcp-client"\n'
            "  config:\n"
            "    transport: stdio\n"
            "    serverName: fuzzagent\n"
            '    command: "fuzzagent-mcp"\n'
            f"    args: {json.dumps(_mcp_args(agent.role))}\n",
        )
        agent.launch = [*agent.launch, "--patch", str(patch)]

    def _ensure_profile(self, home: Path) -> None:
        """Register the dsh-tui profile in this role's isolated DSH_HOME."""
        if not shutil.which("dsh"):
            return
        dsh_home = home / ".dsh"
        dsh_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, DSH_HOME=str(dsh_home))
        subprocess.run(
            ["dsh", "plugin", "--profile", "dsh-tui", "add", "@deepseek-harness-tui/dsh-tui"],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )


class PiDriver(Driver):
    def __init__(self) -> None:
        super().__init__("pi")

    def prepare(self, agent, campaign, home, cwd) -> None:
        config = home / ".pi" / "agent" / "mcp.json"
        _write(
            config,
            json.dumps(
                {
                    "mcpServers": {
                        "fuzzagent": {
                            "command": "fuzzagent-mcp",
                            "args": _mcp_args(agent.role),
                        }
                    }
                },
                indent=2,
            )
            + "\n",
        )


class GenericDriver(Driver):
    def __init__(self) -> None:
        super().__init__("generic")


DRIVERS: dict[str, Driver] = {
    "codex": CodexDriver(),
    "grok": GrokDriver(),
    "dsh-tui": DshTuiDriver(),
    "pi": PiDriver(),
    "generic": GenericDriver(),
}


def driver_for(agent: AgentConfig) -> Driver:
    try:
        return DRIVERS[agent.driver]
    except KeyError as exc:  # pragma: no cover - validated earlier
        raise KeyError(f"no driver for {agent.driver!r}") from exc


def probe_binaries(agents: dict[str, AgentConfig]) -> dict[str, str]:
    """``campaignctl doctor`` helper: which launcher binaries are installed."""
    result: dict[str, str] = {}
    for role, agent in agents.items():
        binary = shutil.which(agent.launch[0]) if agent.launch else None
        result[role] = binary or f"missing: {agent.launch[0] if agent.launch else '?'}"
    return result
