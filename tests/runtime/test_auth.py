"""Per-role auth files from config/agent_auth.toml."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from src.runtime import auth
from src.runtime.campaign import CampaignController
from src.runtime.config import CampaignConfig


def test_load_and_materialize(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "auth.json").write_text('{"token": "secret"}')
    monkeypatch.setenv("HOST_AUTH_DIR", str(bundle))

    toml = tmp_path / "agent_auth.toml"
    toml.write_text(
        '[HarnessAgent.files]\n".codex/auth.json" = "${HOST_AUTH_DIR}/auth.json"\n',
        encoding="utf-8",
    )
    spec = auth.load_auth_spec(toml)
    home = tmp_path / "home"
    report = auth.materialize("HarnessAgent", home, spec)

    dest = home / ".codex" / "auth.json"
    assert dest.read_text() == '{"token": "secret"}'
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert [entry.dest for entry in report.copied] == [".codex/auth.json"]
    assert report.missing == []


def test_missing_source_is_reported_not_fatal(tmp_path: Path):
    toml = tmp_path / "agent_auth.toml"
    toml.write_text('[GrokRole.files]\n".grok/auth.json" = "/nope/auth.json"\n', encoding="utf-8")
    report = auth.materialize("GrokRole", tmp_path / "home", auth.load_auth_spec(toml))
    assert report.copied == []
    assert report.missing[0].source == "/nope/auth.json"


def test_existing_role_credentials_survive_repeated_preparation(tmp_path: Path):
    source = tmp_path / "auth.json"
    source.write_text("original")
    spec = {"ManagerAgent": [auth.AuthFile("ManagerAgent", ".codex/auth.json", str(source))]}
    home = tmp_path / "home"
    auth.materialize("ManagerAgent", home, spec)
    dest = home / ".codex/auth.json"
    dest.write_text("refreshed")
    source.unlink()

    report = auth.materialize("ManagerAgent", home, spec)

    assert dest.read_text() == "refreshed"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert not report.missing


def test_unknown_role_is_ignored(tmp_path: Path):
    toml = tmp_path / "agent_auth.toml"
    toml.write_text('[OtherRole.files]\n"x" = "/nope"\n', encoding="utf-8")
    assert auth.materialize("HarnessAgent", tmp_path, auth.load_auth_spec(toml)).copied == []


@pytest.fixture()
def controller(tmp_path: Path) -> CampaignController:
    from tests.runtime.test_orchestrator import FakeHC

    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    (out / ".cache").mkdir(parents=True)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "codex-auth.json").write_text('{"token": "x"}')

    toml = tmp_path / "agent_auth.toml"
    toml.write_text(
        '[ManagerAgent.files]\n".codex/auth.json" = "' + str(bundle / "codex-auth.json") + '"\n'
        '[HarnessAgent.files]\n".codex/auth.json" = "' + str(bundle / "missing.json") + '"\n',
        encoding="utf-8",
    )
    campaign = CampaignConfig(
        name="cjson",
        target="cjson",
        src=str(src),
        output=str(out),
        session="fz-cjson",
        agents_dir=str(Path(__file__).resolve().parents[2] / "config" / "agents"),
        auth_config=str(toml),
        snapshot_enabled=False,
    )
    return CampaignController(campaign, hc=FakeHC())


def test_prepare_copies_auth_into_role_home_and_ignores_snapshots(controller):
    controller.prepare()
    home = Path(controller.campaign.output) / ".campaign" / "agents" / "ManagerAgent" / "home"
    assert (home / ".codex" / "auth.json").exists()

    gitignore = (Path(controller.campaign.output) / ".gitignore").read_text()
    assert ".campaign/agents/*/home/" in gitignore

    journal = (Path(controller.campaign.output) / ".campaign" / "journal.jsonl").read_text()
    events = [json.loads(line) for line in journal.splitlines()]
    auth_events = [event for event in events if event["event"] == "auth_materialized"]
    assert auth_events, "auth materialisation should be journalled"
    # never log secrets
    assert "token" not in json.dumps(auth_events)
