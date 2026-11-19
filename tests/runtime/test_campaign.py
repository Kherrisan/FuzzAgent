"""Controller smoke tests with a fake hc core."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.campaign import CampaignController
from src.runtime.config import CampaignConfig
from tests.runtime.test_orchestrator import FakeHC

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def controller(tmp_path: Path) -> CampaignController:
    src = tmp_path / "src" / "cjson"
    out = tmp_path / "out" / "cjson"
    src.mkdir(parents=True)
    (out / ".cache").mkdir(parents=True)
    campaign = CampaignConfig(
        name="cjson",
        target="cjson",
        src=str(src),
        output=str(out),
        session="fz-cjson",
        agents_dir=str(REPO / "config" / "agents-codex"),
        base_dir=str(REPO),
    )
    campaign.agents_dir = str(REPO / "config" / "agents-codex")
    return CampaignController(campaign, hc=FakeHC())


def test_prepare_chowns_output_to_root(controller: CampaignController, monkeypatch: pytest.MonkeyPatch):
    output = Path(controller.campaign.output)
    seen: list[tuple[Path, int, int]] = []
    monkeypatch.setattr("src.runtime.campaign.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "src.runtime.campaign.os.chown",
        lambda path, uid, gid: seen.append((Path(path), uid, gid)),
    )
    controller.prepare()
    assert seen == [(output, 0, -1)]


def test_prepare_skips_chown_when_not_root(controller: CampaignController, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr("src.runtime.campaign.os.geteuid", lambda: 1013)
    monkeypatch.setattr("src.runtime.campaign.os.chown", lambda *args: calls.append(args))
    controller.prepare()
    assert calls == []


def test_prepare_creates_tree_and_renders_roles(controller: CampaignController):
    controller.prepare()
    campaign_dir = Path(controller.campaign.output) / ".campaign"
    assert (campaign_dir / "campaign.json").exists()
    rendered = sorted(p.name for p in (campaign_dir / "agents").glob("*.yaml"))
    assert len(rendered) == 8
    assert "ManagerAgent.yaml" in rendered
    meta = json.loads((campaign_dir / "campaign.json").read_text())
    assert meta["session"] == "fz-cjson"


def test_run_dispatches_rounds_to_manager(controller: CampaignController):
    calls: list[tuple[str, str]] = []

    def fake_dispatch(role: str, task: str) -> str:
        calls.append((role, task))
        return "round done"

    controller.orchestrator.dispatch = fake_dispatch  # type: ignore[assignment]
    controller.run(max_rounds=2, resume=False)
    assert [role for role, _ in calls] == ["ManagerAgent", "ManagerAgent"]
    assert "[round 1]" in calls[0][1]
    assert "[round 2]" in calls[1][1]


def test_resume_flag_prepends_brief(controller: CampaignController):
    calls: list[str] = []
    controller.orchestrator.dispatch = lambda role, task: calls.append(task) or "ok"  # type: ignore[assignment]
    controller.run(max_rounds=1, resume=True)
    assert "Campaign resume brief" in calls[0]


def test_status_reports_artifacts(controller: CampaignController):
    controller.prepare()
    status = controller.status()
    assert status["campaign"] == "cjson"
    assert len(status["agents"]) == 8
    assert status["artifacts"]["harnesses"] == []


def test_run_setup_is_a_noop_without_script(controller: CampaignController):
    assert controller.run_setup() is True
    assert controller.setup_failed is False


def test_run_setup_runs_script_with_campaign_env(controller: CampaignController, tmp_path: Path):
    script = tmp_path / "setup.sh"
    script.write_text(
        'printf "%s\\n%s\\n" "$SRC" "$OUTPUT" > "$OUTPUT/provisioned.txt"\n',
        encoding="utf-8",
    )
    controller.campaign.setup_script = str(script)
    assert controller.run_setup() is True
    lines = (Path(controller.campaign.output) / "provisioned.txt").read_text().splitlines()
    assert lines[0] == controller.campaign.src
    assert lines[1] == str(Path(controller.campaign.output))
    journal = (Path(controller.campaign.output) / ".campaign" / "journal.jsonl").read_text()
    assert '"setup_started"' in journal
    assert '"setup_finished"' in journal


def test_run_setup_failure_is_reported(controller: CampaignController, tmp_path: Path):
    script = tmp_path / "setup.sh"
    script.write_text("exit 3\n", encoding="utf-8")
    controller.campaign.setup_script = str(script)
    assert controller.run_setup() is False
    log = sorted((Path(controller.campaign.output) / ".campaign" / "logs").glob("setup-*.log"))
    assert log and log[0].exists()
    journal = (Path(controller.campaign.output) / ".campaign" / "journal.jsonl").read_text()
    assert '"setup_failed"' in journal


def test_run_stops_before_round_one_when_setup_fails(controller: CampaignController, tmp_path: Path):
    script = tmp_path / "setup.sh"
    script.write_text("exit 1\n", encoding="utf-8")
    controller.campaign.setup_script = str(script)
    calls: list[str] = []
    controller.orchestrator.dispatch = lambda role, task: calls.append(role) or "ok"  # type: ignore[assignment]
    assert controller.run(max_rounds=2) is False
    assert calls == []
    assert controller.setup_failed is True
    journal = (Path(controller.campaign.output) / ".campaign" / "journal.jsonl").read_text()
    assert '"campaign_setup_failed"' in journal


def test_run_dispatches_after_successful_setup(controller: CampaignController, tmp_path: Path):
    script = tmp_path / "setup.sh"
    script.write_text("exit 0\n", encoding="utf-8")
    controller.campaign.setup_script = str(script)
    calls: list[str] = []
    controller.orchestrator.dispatch = lambda role, task: calls.append(role) or "ok"  # type: ignore[assignment]
    assert controller.run(max_rounds=1) is True
    assert calls == ["ManagerAgent"]


def test_run_setup_skips_when_script_missing(controller: CampaignController, tmp_path: Path):
    controller.campaign.setup_script = str(tmp_path / "absent.sh")
    assert controller.run_setup() is True
    assert controller.setup_failed is False
    journal = (Path(controller.campaign.output) / ".campaign" / "journal.jsonl").read_text()
    assert '"setup_skipped"' in journal
