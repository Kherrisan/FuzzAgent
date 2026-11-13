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


MCP_COMMAND = "/root/FuzzAgent/bin/fuzzagent-mcp"
DENY_RULES = ("Bash(campaignctl:*)", "Bash(sudo:*)")


def _mcp_command() -> str:
    home = os.environ.get("FUZZAGENT_HOME") or "/root/FuzzAgent"
    path = Path(home) / "bin" / "fuzzagent-mcp"
    try:
        return str(path) if path.exists() else MCP_COMMAND
    except OSError:
        return MCP_COMMAND


def _mcp_args(role: str) -> list[str]:
    return ["--role", role]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bare_key(value: str) -> str:
    if value and all(c.isalnum() or c in "_-" for c in value):
        return value
    return _toml_string(value)


def _mcp_tool_approval_lines(names: list[str]) -> list[str]:
    """Name every enabled tool so each one is auto-approved.

    ``default_tools_approval_mode = "approve"`` already covers tools that
    have no entry. These tables keep a later per-tool override from leaving
    one tool on the prompt path under ``approval_policy = "never"``.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        lines.append(f"[mcp_servers.fuzzagent.tools.{_toml_bare_key(name)}]")
        lines.append('approval_mode = "approve"')
        lines.append("")
    return lines


def _codex_writable_roots(campaign: CampaignConfig) -> list[str]:
    """Campaign output only.

    The source mount is read by ``copy_project_source()``. Leaving it out of
    ``writable_roots`` keeps bubblewrap from creating protected directories
    such as ``.agents`` on that mount.
    """
    text = str(campaign.output)
    return [text] if text else []


# Codex workspace-write mounts these names read-only under every writable root.
# bubblewrap must mkdir a missing one first. On a 0775 directory owned by the
# host user, that mkdir fails inside the user namespace, so create them as root.
_SANDBOX_METADATA_DIRS = (".agents", ".codex", ".aws")


def _ensure_sandbox_metadata(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in _SANDBOX_METADATA_DIRS:
        (root / name).mkdir(exist_ok=True)


def _ensure_output_git(output: Path) -> None:
    """Make the campaign output the nearest git root above each role cwd.

    Codex applies a subdirectory trust decision to the nearest ``.git``.
    A repo at the output directory stops that walk before the FuzzAgent tree.
    """
    output.mkdir(parents=True, exist_ok=True)
    if (output / ".git").exists():
        return
    subprocess.run(
        ["git", "init", "-q"],
        cwd=output,
        check=True,
        capture_output=True,
        text=True,
    )


def _codex_trust_paths(campaign: CampaignConfig, cwd: Path) -> list[str]:
    candidates = [cwd, Path(campaign.output)]
    probe = cwd if cwd.is_absolute() else cwd.absolute()
    for parent in (probe, *probe.parents):
        if (parent / ".git").exists():
            candidates.append(parent)
            break
    paths: list[str] = []
    for path in candidates:
        text = str(path)
        if text and text not in paths:
            paths.append(text)
    return paths


def _campaignctl_prefix_rules(campaign: CampaignConfig) -> str:
    patterns = [
        ["campaignctl"],
        ["/usr/local/bin/campaignctl"],
        ["python3", "/usr/local/bin/campaignctl"],
    ]
    if campaign.base_dir:
        script = str(Path(campaign.base_dir) / "bin" / "campaignctl")
        patterns.extend([[script], ["python3", script]])
    justification = "Do not start or reconfigure the campaign controller."
    blocks = []
    for pattern in patterns:
        rendered = ", ".join(_toml_string(part) for part in pattern)
        blocks.append(
            "prefix_rule(\n"
            f"    pattern = [{rendered}],\n"
            '    decision = "forbidden",\n'
            f"    justification = {_toml_string(justification)},\n"
            ")\n"
        )
    return "\n".join(blocks)


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

    def launch(self, agent: AgentConfig) -> list[str]:
        argv = list(agent.launch)
        # The TUI otherwise stops on "Update now / Skip" and Enter selects
        # Update now, which replaces the pinned CLI mid-campaign.
        if not any("check_for_update_on_startup" in part for part in argv):
            argv.extend(["-c", "check_for_update_on_startup=false"])
        return argv

    def prepare(self, agent, campaign, home, cwd) -> None:
        output = Path(campaign.output)
        _ensure_output_git(output)
        for root in _codex_writable_roots(campaign):
            _ensure_sandbox_metadata(Path(root))
        config = home / ".codex" / "config.toml"
        writable = ", ".join(_toml_string(path) for path in _codex_writable_roots(campaign))
        lines = [
            'approval_policy = "never"',
            'sandbox_mode = "workspace-write"',
            "check_for_update_on_startup = false",
            "",
            "[sandbox_workspace_write]",
            "network_access = true",
            f"writable_roots = [{writable}]",
            "",
            "[mcp_servers.fuzzagent]",
            f'command = "{_mcp_command()}"',
            f"args = {json.dumps(_mcp_args(agent.role))}",
            # approval_policy=never rejects MCP calls that still need a prompt.
            # approve runs this server's tools without a prompt.
            'default_tools_approval_mode = "approve"',
            "",
            # Codex starts this stdio server with a clean environment. The
            # container's SRC and OUTPUT are not visible unless named here.
            "[mcp_servers.fuzzagent.env]",
            f"SRC = {_toml_string(campaign.src)}",
            f"OUTPUT = {_toml_string(campaign.output)}",
            "",
        ]
        lines.extend(_mcp_tool_approval_lines(agent.tools.enabled))
        if agent.model:
            lines.insert(0, f'model = "{agent.model}"')
        # Trust stops at the output repo created above, not the FuzzAgent tree.
        for path in _codex_trust_paths(campaign, cwd):
            lines.append(f"[projects.{_toml_string(path)}]")
            lines.append('trust_level = "trusted"')
            lines.append("")
        _write(config, "\n".join(lines) + "\n")
        _write(home / ".codex" / "rules" / "fuzzagent.rules", _campaignctl_prefix_rules(campaign))
        agents_md = cwd / "AGENTS.md"
        if not agents_md.exists():
            from .prompts import load_system_prompt

            _write(agents_md, load_system_prompt(agent, campaign))


class GrokDriver(Driver):
    def __init__(self) -> None:
        super().__init__("grok")

    def prepare(self, agent, campaign, home, cwd) -> None:
        # Grok's own sandbox cannot be applied inside this container (it needs
        # a bwrap plan the overlay/deny-list cannot satisfy), so run with full
        # access and keep the per-role trust preset.
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
            f"deny_rules = {json.dumps(list(DENY_RULES))}\n\n"
            "[mcp_servers.fuzzagent]\n"
            f'command = "{_mcp_command()}"\n'
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
            f'    command: "{_mcp_command()}"\n'
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
                            "command": _mcp_command(),
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
