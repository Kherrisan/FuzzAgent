"""dsh driver: profile seeding, ``dsh --profile fuzzagent`` launch, MCP patch."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime import drivers
from src.runtime.config import AgentConfig, CampaignConfig, PromptSpec, ToolsSpec


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
        agents_dir="config/agents-codex",
        base_dir=str(tmp_path),
    )


def _agent(tmp_path: Path, role: str = "ManagerAgent", launch: list[str] | None = None) -> AgentConfig:
    agent = AgentConfig(
        role=role,
        driver="dsh-tui",
        launch=launch or ["dsh"],
        home=str(tmp_path / "home"),
        cwd=str(tmp_path / "work"),
        prompt=PromptSpec(source="m:A"),
        tools=ToolsSpec(include=["finish_task"]),
    )
    Path(agent.home).mkdir(parents=True, exist_ok=True)
    Path(agent.cwd).mkdir(parents=True, exist_ok=True)
    return agent


def _baked_profile(tmp_path: Path) -> Path:
    """Stand-in for /opt/dsh-profile/profiles/dsh-tui (the node_modules donor)."""
    tpl = tmp_path / "baked-dsh-tui"
    (tpl / "node_modules" / "@deepseek-harness-tui" / "dsh-tui").mkdir(parents=True)
    (tpl / "package.json").write_text('{"name": "dsh-profile-dsh-tui"}', encoding="utf-8")
    return tpl


def _repo_profile(tmp_path: Path, monkeypatch) -> Path:
    """Stand-in for config/dsh/profiles/ark-deepseek-flash."""
    repo = tmp_path / "repo"
    prof = repo / "config" / "dsh" / "profiles" / "ark-deepseek-flash"
    prof.mkdir(parents=True)
    (prof / "package.json").write_text('{"name": "dsh-profile-ark-deepseek-flash"}', encoding="utf-8")
    (prof / "cordis.patch.yml").write_text("- id: llm-pi-ai\n", encoding="utf-8")
    monkeypatch.setenv("FUZZAGENT_HOME", str(repo))
    return prof


def test_prepare_seeds_the_fuzzagent_profile(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_profile(tmp_path, monkeypatch)
    agent = _agent(tmp_path)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    seeded = Path(agent.home) / ".dsh" / "profiles" / "ark-deepseek-flash"
    assert (seeded / "package.json").is_file()
    assert (seeded / "cordis.patch.yml").is_file()
    assert (seeded / "node_modules").is_dir()


def test_prepare_keeps_an_already_seeded_profile(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_profile(tmp_path, monkeypatch)
    agent = _agent(tmp_path)
    seeded = Path(agent.home) / ".dsh" / "profiles" / "ark-deepseek-flash"
    seeded.mkdir(parents=True)
    (seeded / "package.json").write_text('{"local": true}', encoding="utf-8")

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert (seeded / "package.json").read_text(encoding="utf-8") == '{"local": true}'


def test_launch_uses_the_launcher_profile_and_patch_once(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_profile(tmp_path, monkeypatch)
    agent = _agent(tmp_path)
    driver = drivers.driver_for(agent)

    driver.prepare(agent, campaign, Path(agent.home), Path(agent.cwd))
    driver.prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert agent.launch[:3] == ["dsh", "--profile", "ark-deepseek-flash"]
    assert agent.launch.count("--patch") == 1
    patch = Path(agent.launch[agent.launch.index("--patch") + 1])
    assert patch.is_file()


def test_launch_without_dsh_is_left_alone(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_profile(tmp_path, monkeypatch)
    agent = _agent(tmp_path, launch=["dsh-tui"])

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert agent.launch[:1] == ["dsh-tui"]
    assert "--profile" not in agent.launch


def test_mcp_patch_carries_the_role(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_profile(tmp_path, monkeypatch)
    agent = _agent(tmp_path, role="HarnessAgent")

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    text = (Path(agent.home) / "dsh-mcp.patch.yml").read_text(encoding="utf-8")
    assert "@deepseek-ai/dsh-mcp-client" in text
    assert 'args: ["--role", "HarnessAgent"]' in text

def test_prepare_without_a_baked_profile_leaves_dsh_alone(tmp_path, campaign, monkeypatch):
    """No template (and no network) means no profile: dsh then fails loudly."""
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", tmp_path / "absent")
    agent = _agent(tmp_path)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert not (Path(agent.home) / ".dsh" / "profiles" / "ark-deepseek-flash").exists()
