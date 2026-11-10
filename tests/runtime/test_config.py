"""Per-role configuration loading, rendering and validation."""

from __future__ import annotations

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


def test_every_role_is_loadable_and_renders(campaign: CampaignConfig):
    agents = load_agents(REPO / "config" / "agents", {"OUTPUT": campaign.output, "SRC": campaign.src})
    assert len(agents) == 8, sorted(agents)
    registry = load_tools()
    for agent in agents.values():
        rendered = render_agent(agent, campaign)
        assert rendered.home.startswith(campaign.output), rendered.role
        assert rendered.cwd.startswith(campaign.output), rendered.role
        assert "HERDR_ENV" in rendered.env_scrub
        missing = [name for name in rendered.tools.enabled if name not in registry]
        assert not missing, f"{rendered.role}: unknown tools {missing}"
        assert "exit" in rendered.tools.enabled, rendered.role


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
        "prompt: {source: 'm:A'}\ntools: {include: [bash, exit], disable: [bash]}\n",
        encoding="utf-8",
    )
    agent = load_agent(path)
    assert agent.tools.enabled == ["exit"]


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
