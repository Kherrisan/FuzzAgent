"""The all-pi campaign profile: ark endpoint, pinned model, auth and MCP."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime import drivers
from src.runtime.auth import load_auth_spec
from src.runtime.config import (
    AgentConfig,
    CampaignConfig,
    PromptSpec,
    ToolsSpec,
    load_agents,
    load_campaign,
    render_agent,
)

REPO = Path(__file__).resolve().parents[2]
ROLES = {
    "ManagerAgent",
    "BuilderAgent",
    "DictionaryAgent",
    "SeedAgent",
    "HarnessAgent",
    "FuzzerExecutorAgent",
    "CrashAnalyzerAgent",
    "CoverageAnalyzerAgent",
}
ROUTE = ("ark", "deepseek-v4-1-flash-261017")


@pytest.fixture()
def campaign(tmp_path: Path) -> CampaignConfig:
    src = tmp_path / "src" / "cjson"
    out = tmp_path / "out" / "cjson"
    src.mkdir(parents=True)
    out.mkdir(parents=True)
    return CampaignConfig(
        name="cjson",
        target="cjson",
        src=str(src),
        output=str(out),
        session="fz-cjson",
        agents_dir="config/agents-pi",
        base_dir=str(tmp_path),
    )


def test_every_role_runs_pi_on_the_ark_route():
    agents = load_agents(REPO / "config" / "agents-pi")
    assert set(agents) == ROLES
    for role, agent in agents.items():
        assert agent.driver == "pi", role
        assert (agent.provider, agent.model) == (
            ROUTE[0], f"{ROUTE[0]}/{ROUTE[1]}"), role
        launch = agent.launch
        assert launch[0] == "pi", role
        assert launch[launch.index("--provider") + 1] == ROUTE[0], role
        assert launch[launch.index("--model") + 1] == ROUTE[1], role


def test_auth_config_maps_every_role_to_the_host_pi_auth(monkeypatch):
    monkeypatch.setenv("HOST_AUTH_DIR", "/run/fuzzagent-auth")
    spec = load_auth_spec(REPO / "config" / "agent_auth-pi.toml")
    assert set(spec) == ROLES
    for role, entries in spec.items():
        assert len(entries) == 1, role
        assert entries[0].dest == ".pi/agent/auth.json", role
        assert entries[0].source == "/run/fuzzagent-auth/pi/auth.json", role


def test_campaign_profile_points_at_the_pi_agents_and_auth(tmp_path: Path, monkeypatch):
    src = tmp_path / "src" / "cjson"
    out = tmp_path / "out" / "cjson"
    src.mkdir(parents=True)
    out.mkdir(parents=True)
    monkeypatch.setenv("SRC", str(src))
    monkeypatch.setenv("OUTPUT", str(out))
    monkeypatch.setenv("REPO_BASE_NAME", "cjson")
    monkeypatch.setenv("HC_SESSION", "fz-cjson")

    campaign = load_campaign(REPO / "config" / "campaign-pi.yaml")

    assert campaign.agents_dir == "config/agents-pi"
    assert campaign.auth_config == "config/agent_auth-pi.toml"
    assert campaign.manager_role == "ManagerAgent"
    assert campaign.setup_script is None  # no config/setup/cjson.sh ships


def test_driver_writes_the_ark_endpoint_and_keeps_the_mcp_server(
        tmp_path: Path, campaign: CampaignConfig):
    agent = render_agent(
        load_agents(REPO / "config" / "agents-pi")["HarnessAgent"], campaign)
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    home.mkdir()
    cwd.mkdir()
    agent = AgentConfig(**{**agent.__dict__, "home": str(home), "cwd": str(cwd)})

    drivers.driver_for(agent).prepare(agent, campaign, home, cwd)

    models = json.loads((home / ".pi" / "agent" / "models.json").read_text(encoding="utf-8"))
    provider = models["providers"]["ark"]
    assert provider["baseUrl"] == "https://ark.cn-beijing.volces.com/api/v3"
    assert provider["api"] == "openai-completions"
    assert provider["models"] == [{"id": "deepseek-v4-1-flash-261017"}]
    mcp = json.loads((home / ".pi" / "agent" / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["fuzzagent"]["args"] == ["--role", "HarnessAgent"]


def test_driver_skips_models_json_without_a_known_route(
        tmp_path: Path, campaign: CampaignConfig):
    agent = AgentConfig(
        role="HarnessAgent",
        driver="pi",
        launch=["pi"],
        home=str(tmp_path / "home2"),
        cwd=str(tmp_path / "work2"),
        prompt=PromptSpec(source="m:A"),
        tools=ToolsSpec(include=["finish_task"]),
    )
    Path(agent.home).mkdir(parents=True)
    Path(agent.cwd).mkdir(parents=True)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    agent_dir = Path(agent.home) / ".pi" / "agent"
    assert (agent_dir / "mcp.json").is_file()
    assert not (agent_dir / "models.json").exists()
