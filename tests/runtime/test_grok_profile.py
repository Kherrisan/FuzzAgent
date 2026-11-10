"""The all-grok campaign profile (grok-4.7, reasoning effort high)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime.auth import load_auth_spec
from src.runtime.config import load_agents, load_campaign, render_agent

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


def test_every_role_uses_grok_with_the_pinned_model_and_effort():
    agents = load_agents(REPO / "config" / "agents-grok")
    assert set(agents) == ROLES
    for role, agent in agents.items():
        assert agent.driver == "grok", role
        assert agent.model == "grok-4.7", role
        launch = agent.launch
        assert launch[0] == "grok", role
        assert "--model" in launch and launch[launch.index("--model") + 1] == "grok-4.7", role
        assert "--reasoning-effort" in launch, role
        assert launch[launch.index("--reasoning-effort") + 1] == "high", role


def test_auth_config_maps_every_role_to_the_host_grok_auth(monkeypatch):
    monkeypatch.setenv("HOST_AUTH_DIR", "/run/fuzzagent-auth")
    spec = load_auth_spec(REPO / "config" / "agent_auth-grok.toml")
    assert set(spec) == ROLES
    for role, entries in spec.items():
        assert len(entries) == 1, role
        assert entries[0].dest == ".grok/auth.json", role
        assert entries[0].source == "/run/fuzzagent-auth/grok/auth.json", role


def test_campaign_profile_points_at_the_grok_agents(tmp_path, monkeypatch):
    src = tmp_path / "src" / "cjson"
    out = tmp_path / "out" / "cjson"
    src.mkdir(parents=True)
    out.mkdir(parents=True)
    monkeypatch.setenv("SRC", str(src))
    monkeypatch.setenv("OUTPUT", str(out))
    monkeypatch.setenv("REPO_BASE_NAME", "cjson")
    monkeypatch.setenv("HC_SESSION", "fz-cjson")

    campaign = load_campaign(REPO / "config" / "campaign-grok.yaml")
    assert campaign.agents_dir.endswith("config/agents-grok")
    assert campaign.auth_config.endswith("config/agent_auth-grok.toml")
    agents = load_agents(campaign.agents_dir, {"OUTPUT": str(out), "SRC": str(src)})
    for agent in agents.values():
        rendered = render_agent(agent, campaign)
        assert rendered.home.startswith(str(out))
        assert rendered.driver == "grok"


def test_collect_auth_copies_grok_credentials(tmp_path):
    import os
    import subprocess

    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text('{"token": "host"}')

    dest = tmp_path / "bundle"
    env = dict(os.environ, HOME=str(home), GROK_HOME=str(home / ".grok"))
    result = subprocess.run(
        [str(REPO / "bin" / "collect-auth"), str(dest)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    copied = dest / "grok" / "auth.json"
    assert copied.read_text() == '{"token": "host"}'
    assert oct(copied.stat().st_mode)[-3:] == "600"
