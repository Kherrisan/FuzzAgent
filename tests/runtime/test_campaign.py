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
        agents_dir=str(REPO / "config" / "agents"),
        snapshot_enabled=False,
        base_dir=str(REPO),
    )
    campaign.agents_dir = str(REPO / "config" / "agents")
    return CampaignController(campaign, hc=FakeHC())


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
