"""Per-role configuration loading, rendering and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.config import (
    CampaignConfig,
    ConfigError,
    load_agent,
    load_agents,
    load_campaign,
    render_agent,
)
from src.runtime.registry import load_tools

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def campaign(tmp_path: Path) -> CampaignConfig:
    src = tmp_path / "src" / "cjson"
    out = tmp_path / "out" / "cjson"
    src.mkdir(parents=True)
    out.mkdir(parents=True)
    return CampaignConfig(
        name="cjson", target="cjson", src=str(src), output=str(out), session="fz-cjson"
    )


BUILTIN_TOOLS = {
    "bash",
    "open",
    "goto",
    "scroll_up",
    "scroll_down",
    "edit",
    "undo_edit",
    "write_file",
    "write_bin_to_file",
    "find_file",
    "search_file",
    "search_dir",
    "search_web",
}


def test_every_role_is_loadable_and_renders(campaign: CampaignConfig):
    registry = load_tools()
    for folder in ("agents", "agents-grok"):
        agents = load_agents(REPO / "config" / folder, {"OUTPUT": campaign.output, "SRC": campaign.src})
        assert len(agents) == 8, sorted(agents)
        for agent in agents.values():
            rendered = render_agent(agent, campaign)
            enabled = set(rendered.tools.enabled)
            assert rendered.home.startswith(campaign.output), rendered.role
            assert rendered.cwd.startswith(campaign.output), rendered.role
            assert "HERDR_ENV" in rendered.env_scrub
            missing = [name for name in rendered.tools.enabled if name not in registry]
            assert not missing, f"{rendered.role}: unknown tools {missing}"
            assert not enabled & BUILTIN_TOOLS, rendered.role
            assert "finish_task" in enabled, rendered.role
            assert "exit" not in enabled, rendered.role
            assert rendered.mcp.transport == "stdio", rendered.role
            assert rendered.mcp.server == "fuzzagent-mcp", rendered.role
            if rendered.role == "ManagerAgent":
                assert "send_agent_task" in enabled
                assert "wait_agent_task" in enabled
            else:
                assert "send_agent_task" not in enabled, rendered.role
                assert "wait_agent_task" not in enabled, rendered.role


def test_manager_prompt_has_no_coverage_variant():
    agents = load_agents(REPO / "config" / "agents")
    manager = agents["ManagerAgent"]
    assert manager.prompt.guidance_variant
    assert manager.prompt.guidance_variant.endswith("NO_COVERAGE_GUIDANCE_SYSTEM_PROMPT")


def test_unknown_driver_is_rejected(tmp_path: Path):
    path = tmp_path / "Bad.yaml"
    path.write_text(
        "role: Bad\ndriver: nope\nlaunch: [x]\nhome: /tmp/h\ncwd: /tmp/w\n"
        "prompt: {source: 'm:A'}\ntools: {include: [bash]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown driver"):
        load_agent(path)


def test_tool_disable_wins(tmp_path: Path):
    path = tmp_path / "A.yaml"
    path.write_text(
        "role: A\ndriver: generic\nlaunch: [x]\nhome: /tmp/h\ncwd: /tmp/w\n"
        "prompt: {source: 'm:A'}\ntools: {include: [bash, finish_task], disable: [bash]}\n",
        encoding="utf-8",
    )
    agent = load_agent(path)
    assert agent.tools.enabled == ["finish_task"]


def test_campaign_file_roundtrip(campaign: CampaignConfig, tmp_path: Path):
    path = tmp_path / "campaign.yaml"
    path.write_text(
        f"name: cjson\ntarget: cjson\nsrc: {campaign.src}\noutput: {campaign.output}\n"
        "session: fz-cjson\nengine: hc\nagents_dir: config/agents\n",
        encoding="utf-8",
    )
    loaded = load_campaign(path)
    assert loaded.session == "fz-cjson"
    assert loaded.engine == "hc"


def test_drivers_still_launch_fuzzagent_mcp_over_stdio(campaign: CampaignConfig, tmp_path: Path):
    from src.runtime.drivers import driver_for

    agent = render_agent(load_agent(REPO / "config" / "agents" / "ManagerAgent.yaml"), campaign)
    agent.home = str(tmp_path / "home")
    agent.cwd = str(tmp_path / "work")
    for driver_name in ("codex", "grok", "pi"):
        agent.driver = driver_name
        home = tmp_path / driver_name
        driver_for(agent).prepare(agent, campaign, home, Path(agent.cwd))
    codex = (tmp_path / "codex" / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'command = "fuzzagent-mcp"' in codex
    assert 'args = ["--role", "ManagerAgent"]' in codex
    grok = (tmp_path / "grok" / ".grok" / "config.toml").read_text(encoding="utf-8")
    assert 'command = "fuzzagent-mcp"' in grok
    assert 'args = ["--role", "ManagerAgent"]' in grok
    pi = json.loads((tmp_path / "pi" / ".pi" / "agent" / "mcp.json").read_text(encoding="utf-8"))
    assert pi["mcpServers"]["fuzzagent"]["command"] == "fuzzagent-mcp"
    assert pi["mcpServers"]["fuzzagent"]["args"] == ["--role", "ManagerAgent"]
