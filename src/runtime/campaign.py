"""campaignctl: the non-LLM controller that owns a campaign.

It prepares the tree, keeps the manager's round loop going, enforces the time
budget, and offers the operator commands (status/doctor/resume/rollback).
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from . import artifacts, guards, prompts
from .config import CampaignConfig, load_agents, load_campaign
from .drivers import probe_binaries
from .hc import HC
from .orchestrator import Orchestrator

REPO_ROOT = Path(__file__).resolve().parents[2]


class CampaignController:
    def __init__(self, campaign: CampaignConfig, hc: HC | None = None):
        self.campaign = campaign
        self.hc = hc or HC(str(campaign.hc.get("binary", "hc")), campaign.session)
        self.paths = artifacts.ProjectPaths(
            Path(campaign.src), Path(campaign.output), Path(campaign.output) / ".campaign"
        )
        os.environ.setdefault("FUZZAGENT_SKIP_ENV_INIT", "1")
        self.agents = load_agents(
            campaign.agents_dir, {"OUTPUT": campaign.output, "SRC": campaign.src}
        )
        self.orchestrator = Orchestrator(campaign, self.agents, hc=self.hc)
        self.stop_requested = False

    # --- preparation ------------------------------------------------------
    def prepare(self) -> None:
        root = Path(self.campaign.output)
        for directory in (
            "build", "harnesses", "fuzzers", "dictionary", "seeds",
            "coverage_guidance", "crash_reports", ".cache",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        self.orchestrator.campaign_dir.mkdir(parents=True, exist_ok=True)
        (self.orchestrator.campaign_dir / "agents").mkdir(parents=True, exist_ok=True)
        (self.orchestrator.campaign_dir / "tasks").mkdir(parents=True, exist_ok=True)

        for agent in self.agents.values():
            self.orchestrator.prepare_role(agent)
            rendered = self.orchestrator.campaign_dir / "agents" / f"{agent.role}.yaml"
            if agent.source_path and not rendered.exists():
                rendered.write_text(Path(agent.source_path).read_text(encoding="utf-8"), encoding="utf-8")

        meta = {
            "name": self.campaign.name,
            "target": self.campaign.target,
            "src": self.campaign.src,
            "output": self.campaign.output,
            "session": self.campaign.session,
            "duration_hours": self.campaign.duration_hours,
            "started": time.time(),
        }
        (self.orchestrator.campaign_dir / "campaign.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        self.orchestrator.journal({"event": "campaign_prepared", **meta})

    # --- rounds -----------------------------------------------------------
    def _manager_prompt(self, round_index: int, resumed: bool) -> str:
        base = prompts.load_initial_input()
        if resumed:
            base = self.orchestrator.resume_brief() + "\n\n" + base
        return f"[round {round_index}]\n{base}"

    def run(self, max_rounds: int | None = None, resume: bool = False) -> None:
        self.prepare()
        manager = self.agents[self.campaign.manager_role]
        limit = max_rounds if max_rounds is not None else int(self.campaign.max_rounds or 0)
        deadline = time.time() + self.campaign.duration_hours * 3600

        previous = signal.getsignal(signal.SIGTERM)

        def _stop(signum, frame):  # pragma: no cover - signal path
            self.stop_requested = True
            self.orchestrator.journal({"event": "stop_requested", "signal": signum})

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        try:
            round_index = 0
            while not self.stop_requested:
                round_index += 1
                if limit and round_index > limit:
                    break
                if time.time() >= deadline:
                    self.orchestrator.journal({"event": "budget_exhausted"})
                    break
                prompt = self._manager_prompt(round_index, resumed=resume and round_index == 1)
                try:
                    report = self.orchestrator.dispatch(manager.role, prompt)
                except Exception as exc:  # noqa: BLE001 - keep the campaign alive
                    self.orchestrator.journal(
                        {"event": "round_failed", "round": round_index, "error": str(exc)}
                    )
                    time.sleep(30)
                    continue
                self.orchestrator.journal(
                    {"event": "round_finished", "round": round_index, "report": report[:400]}
                )
                if "campaign-complete" in report.lower():
                    self.orchestrator.journal({"event": "campaign_complete"})
                    break
        finally:
            signal.signal(signal.SIGTERM, previous)

    # --- operator commands ------------------------------------------------
    def status(self) -> dict[str, Any]:
        terminals = []
        try:
            terminals = [
                {"name": t.name, "address": t.address, "state": t.state, "status": t.status}
                for t in self.hc.list_all()
            ]
        except Exception as exc:  # noqa: BLE001 - hc may be down
            terminals = [{"error": str(exc)}]
        agents = {}
        for role, agent in self.agents.items():
            agents[role] = {
                "driver": agent.driver,
                "terminal": self.orchestrator.read_state(role).get("terminal"),
                "primed": self.orchestrator.read_state(role).get("primed", False),
            }
        return {
            "campaign": self.campaign.name,
            "artifacts": {
                "harnesses": self.paths.harness_ids(),
                "fuzzers": self.paths.fuzzer_states(),
                "guidance": len(self.paths.list_guidance()),
                "crashes": len(self.paths.list_crash_reports()),
            },
            "agents": agents,
            "terminals": terminals,
            "interrupted": self.orchestrator.interrupted_tasks(),
        }

    def doctor(self) -> dict[str, Any]:
        problems: list[str] = []
        binaries = probe_binaries(self.agents)
        for role, path in binaries.items():
            if path.startswith("missing:"):
                problems.append(f"{role}: launcher not installed ({path})")
        hc_version = None
        try:
            import subprocess

            proc = subprocess.run(
                [self.hc.binary, "--version"], capture_output=True, text=True, timeout=15
            )
            hc_version = (proc.stdout or proc.stderr).strip() or None
            if proc.returncode != 0:
                problems.append(f"hc --version failed: {hc_version}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"hc unavailable: {exc}")
        problems.extend(guards.verify_artifacts(self.paths))
        return {"problems": problems, "binaries": binaries, "hc": hc_version}

    def rollback(self, target: str) -> str:
        from src.utils.vms import VMS

        vms = VMS(self.campaign.output)
        if target in {"last-good", "last"}:
            snapshots = vms.list_snapshots()
            if not snapshots:
                raise RuntimeError("no snapshots recorded")
            target_id = snapshots[-1]["id"]
        else:
            target_id = int(target)
        for terminal in self.hc.find_by_label(campaign=self.campaign.name):
            self.hc.kill(terminal.name or terminal.address)
        result = vms.restore_snapshot(target_id)
        self.orchestrator.journal({"event": "rollback", "snapshot": target_id})
        return str(result)


def load(path: str) -> CampaignConfig:
    return load_campaign(path)
