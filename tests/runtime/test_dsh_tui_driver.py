"""dsh driver: `tui` profile seeding, launch argv and the MCP overlay."""

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


def _agent(tmp_path: Path, role: str = "ManagerAgent", launch: list[str] | None = None,
           model: str | None = None, provider: str | None = None) -> AgentConfig:
    agent = AgentConfig(
        role=role,
        driver="dsh-tui",
        launch=launch or ["dsh"],
        model=model,
        provider=provider,
        home=str(tmp_path / "home"),
        cwd=str(tmp_path / "work"),
        prompt=PromptSpec(source="m:A"),
        tools=ToolsSpec(include=["finish_task"]),
    )
    Path(agent.home).mkdir(parents=True, exist_ok=True)
    Path(agent.cwd).mkdir(parents=True, exist_ok=True)
    return agent


def _baked_profile(tmp_path: Path) -> Path:
    """Stand-in for /opt/dsh-profile/profiles/tui (package.json + node_modules)."""
    tpl = tmp_path / "baked" / "tui"
    (tpl / "node_modules" / "@deepseek-harness-tui" / "dsh-tui").mkdir(parents=True)
    (tpl / "package.json").write_text('{"name": "dsh-profile-tui"}', encoding="utf-8")
    (tpl / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    return tpl


def _repo_patch(tmp_path: Path, monkeypatch) -> Path:
    """Stand-in for config/dsh/profiles/tui/cordis.patch.yml (YAML only)."""
    repo = tmp_path / "repo"
    prof = repo / "config" / "dsh" / "profiles" / "tui"
    prof.mkdir(parents=True)
    patch = prof / "cordis.patch.yml"
    patch.write_text("- id: llm-pi-ai\n", encoding="utf-8")
    monkeypatch.setenv("FUZZAGENT_HOME", str(repo))
    return patch


def test_prepare_copies_the_baked_profile_and_overlays_the_yaml(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    repo_patch = _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    seeded = Path(agent.home) / ".dsh" / "profiles" / "tui"
    assert (seeded / "package.json").is_file()
    assert (seeded / "node_modules").is_dir()
    assert (seeded / "cordis.patch.yml").read_text(encoding="utf-8") == repo_patch.read_text(encoding="utf-8")


def test_prepare_keeps_an_already_seeded_profile(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path)
    seeded = Path(agent.home) / ".dsh" / "profiles" / "tui"
    seeded.mkdir(parents=True)
    (seeded / "package.json").write_text('{"local": true}', encoding="utf-8")

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert (seeded / "package.json").read_text(encoding="utf-8") == '{"local": true}'


def test_launch_uses_the_tui_profile_and_patch_once(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path)
    driver = drivers.driver_for(agent)

    driver.prepare(agent, campaign, Path(agent.home), Path(agent.cwd))
    driver.prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert agent.launch[:3] == ["dsh", "--profile", "tui"]
    assert agent.launch.count("--patch") == 1
    assert Path(agent.launch[agent.launch.index("--patch") + 1]).is_file()


def test_launch_without_dsh_is_left_alone(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path, launch=["dsh-tui"])

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert agent.launch[:1] == ["dsh-tui"]
    assert "--profile" not in agent.launch


def test_mcp_overlay_carries_the_role(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path, role="HarnessAgent")

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    text = (Path(agent.home) / "dsh.patch.yml").read_text(encoding="utf-8")
    assert "@deepseek-ai/dsh-mcp-client" in text
    assert 'args: ["--role", "HarnessAgent"]' in text


def test_onboarding_is_marked_done(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    flag = Path(agent.home) / ".dsh-tui" / "onboarding.json"
    assert flag.is_file() and '"completed": true' in flag.read_text(encoding="utf-8")


def test_prepare_without_a_baked_profile_leaves_dsh_alone(tmp_path, campaign, monkeypatch):
    """No image template means no profile: dsh then fails loudly."""
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", tmp_path / "absent")
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    assert not (Path(agent.home) / ".dsh" / "profiles" / "tui").exists()


def test_patch_pins_the_role_model_route_as_allowed_models(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path, model="ark/deepseek-v4-1-flash-261017", provider="ark")

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    text = (Path(agent.home) / "dsh.patch.yml").read_text(encoding="utf-8")
    assert (
        "- id: agent-default-model\n"
        "  config:\n"
        '    provider: "ark"\n'
        '    model: "deepseek-v4-1-flash-261017"\n'
    ) in text
    assert "- id: dsh-tui-subagent-model-selection-settings" in text
    assert "    allowedModels:\n" in text
    assert '      - provider: "ark"\n' in text
    assert '        model: "deepseek-v4-1-flash-261017"\n' in text


def test_patch_without_a_model_route_leaves_the_model_choice_open(tmp_path, campaign, monkeypatch):
    monkeypatch.setattr(drivers, "DSH_PROFILE_TEMPLATE", _baked_profile(tmp_path))
    _repo_patch(tmp_path, monkeypatch)
    agent = _agent(tmp_path)

    drivers.driver_for(agent).prepare(agent, campaign, Path(agent.home), Path(agent.cwd))

    text = (Path(agent.home) / "dsh.patch.yml").read_text(encoding="utf-8")
    assert "allowedModels" not in text
    assert "@deepseek-ai/dsh-mcp-client" in text


def test_model_route_accepts_qualified_and_bare_names():
    class Role:
        def __init__(self, model, provider):
            self.model, self.provider = model, provider

    assert drivers.model_route(Role("ark/deepseek-v4-1-flash-261017", "ark")) == (
        "ark", "deepseek-v4-1-flash-261017")
    assert drivers.model_route(Role("deepseek-flash", "ark")) == ("ark", "deepseek-flash")
    assert drivers.model_route(Role("ark/deepseek-flash", None)) == ("ark", "deepseek-flash")
    assert drivers.model_route(Role("grok-4.7", None)) is None
    assert drivers.model_route(Role(None, "ark")) is None
