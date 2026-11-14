"""campaignctl: the non-LLM controller that owns a campaign.

It prepares the tree, keeps the manager's round loop going, enforces the time
budget, and offers the operator commands (status/doctor/resume/rollback).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import artifacts, guards, prompts
from .config import CampaignConfig, load_agents, load_campaign
from .drivers import probe_binaries
from .hc import HC
from .orchestrator import Orchestrator

REPO_ROOT = Path(__file__).resolve().parents[2]


def _report_is_blocked(report: str) -> bool:
    head = report[:160].lower()
    return "status blocked" in head or " is blocked:" in head


def _own_output_as_root(output: Path) -> None:
    """Make the campaign output directory owned by uid 0.

    bubblewrap maps its uid 0 to the outer uid 0 and drops
    ``CAP_DAC_OVERRIDE``, so it can create entries only where the mode bits
    allow uid 0. The group is left unchanged.
    """
    if os.geteuid() != 0:
        return
    if output.stat().st_uid == 0:
        return
    os.chown(output, 0, -1)


class CampaignController:
    def __init__(self, campaign: CampaignConfig, hc: HC | None = None):
        self.campaign = campaign
        self.hc = hc or HC(str(campaign.hc.get("binary", "hc")), campaign.session)
        self.paths = artifacts.ProjectPaths(
            Path(campaign.src), Path(campaign.output), Path(campaign.output) / ".campaign"
        )
        self.agents = load_agents(
            campaign.agents_dir, {"OUTPUT": campaign.output, "SRC": campaign.src}
        )
        self.orchestrator = Orchestrator(campaign, self.agents, hc=self.hc)
        self.stop_requested = False
        # Set when the provisioning script failed; the campaign must not run.
        self.setup_failed = False

    # --- provisioning -----------------------------------------------------
    def run_setup(self) -> bool:
        """Run the campaign's provisioning script once, before any agent starts.

        The script installs the external libraries and build tools the target
        needs (gperf, bison, gtk-doc, ...). Returns ``False`` when the script is
        missing or exits non-zero: the campaign must not start in that case.
        """
        script = self.campaign.setup_script
        if not script:
            return True

        path = Path(script)
        base = str(self.campaign.base_dir or REPO_ROOT)
        if not path.is_absolute():
            path = Path(base) / path
        log_dir = self.orchestrator.campaign_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"setup-{int(time.time())}.log"

        if not path.is_file():
            # The campaign profile may point at a per-target provisioner
            # (config/setup/${REPO_BASE_NAME}.sh). Targets without one have
            # nothing to install, so this is a skip rather than a failure.
            self.orchestrator.journal(
                {
                    "event": "setup_skipped",
                    "script": str(path),
                    "reason": "script not found; nothing to provision",
                }
            )
            return True

        env = os.environ.copy()
        env.update(
            {
                "SRC": str(self.campaign.src),
                "OUTPUT": str(self.campaign.output),
                "CAMPAIGN_DIR": str(self.orchestrator.campaign_dir),
                "REPO_ROOT": base,
                "SETUP_SCRIPT": str(path),
                "DEBIAN_FRONTEND": "noninteractive",
            }
        )
        timeout = float(self.campaign.setup_timeout_seconds or 0) or None
        self.orchestrator.journal(
            {"event": "setup_started", "script": str(path), "log": str(log_path)}
        )

        started = time.time()
        detail: dict[str, Any] = {}
        ok = False
        try:
            with log_path.open("w", encoding="utf-8") as handle:
                proc = subprocess.run(
                    ["bash", str(path)],
                    cwd=base,
                    env=env,
                    # campaignctl owns the container's controlling terminal, and
                    # apt/dpkg stop themselves (SIGTTIN/SIGTTOU) as soon as they
                    # touch /dev/tty. Detach the provisioner from that terminal:
                    # its own session, no inherited stdin, output to the log.
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    timeout=timeout,
                )
            ok = proc.returncode == 0
            detail["returncode"] = proc.returncode
        except subprocess.TimeoutExpired:
            detail["returncode"] = None
            detail["timeout_seconds"] = timeout
        except OSError as exc:  # pragma: no cover - spawn failure is unusual
            detail["returncode"] = None
            detail["error"] = str(exc)
        detail["seconds"] = round(time.time() - started, 1)

        event = "setup_finished" if ok else "setup_failed"
        self.orchestrator.journal(
            {"event": event, "script": str(path), "log": str(log_path), **detail}
        )
        return ok

    # --- preparation ------------------------------------------------------
    def prepare(self) -> None:
        root = Path(self.campaign.output)
        for directory in (
            "build", "harnesses", "fuzzers", "dictionary", "seeds",
            "coverage_guidance", "crash_reports", ".cache",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        # The SanCov build links this standalone driver (it supplies main() for
        # harnesses, which only export LLVMFuzzerTestOneInput). copy_and_check
        # _harness refuses to run without it, so it must exist before round 1.
        wrapper = Path(self.campaign.base_dir or ".") / "src" / "utils" / "fuzz_wrapper.cpp"
        if wrapper.is_file():
            shutil.copy(wrapper, root / ".cache" / "fuzz_wrapper.cpp")
        # Install the tool scripts (observe_current_states, project_utils, ...)
        # so agent MCP tools find them on PATH.
        script = Path(self.campaign.base_dir or ".") / "src" / "tools" / "copy_tools.sh"
        if script.exists():
            subprocess.run(["bash", str(script)], check=False)
        self.orchestrator.campaign_dir.mkdir(parents=True, exist_ok=True)
        (self.orchestrator.campaign_dir / "agents").mkdir(parents=True, exist_ok=True)
        (self.orchestrator.campaign_dir / "tasks").mkdir(parents=True, exist_ok=True)
        _own_output_as_root(root)

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
            "agents_dir": str(Path(self.campaign.agents_dir).resolve()),
            "base_dir": self.campaign.base_dir,
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

    def run(self, max_rounds: int | None = None, resume: bool = False) -> bool:
        """Run the round loop. Returns False when provisioning failed."""
        if not self.run_setup():
            self.setup_failed = True
            self.orchestrator.journal(
                {
                    "event": "campaign_setup_failed",
                    "script": str(self.campaign.setup_script),
                }
            )
            return False
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
            blocked_streak = 0
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

                if _report_is_blocked(report):
                    blocked_streak += 1
                    self.orchestrator.journal(
                        {
                            "event": "round_blocked",
                            "round": round_index,
                            "streak": blocked_streak,
                            "report": report[:300],
                        }
                    )
                    max_blocked = int(self.campaign.max_blocked_rounds or 0)
                    if max_blocked and blocked_streak >= max_blocked:
                        self.orchestrator.journal(
                            {"event": "campaign_blocked", "round": round_index}
                        )
                        break
                    self._sleep(self.campaign.blocked_backoff_seconds)
                else:
                    blocked_streak = 0
        finally:
            signal.signal(signal.SIGTERM, previous)
        return True

    def _sleep(self, seconds: float) -> None:
        """Sleep, but stay responsive to a stop request."""
        deadline = time.time() + max(0.0, float(seconds or 0.0))
        while not self.stop_requested and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))

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
        if shutil.which(self.hc.binary) is None:
            problems.append(f"hc binary not found on PATH: {self.hc.binary}")
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
        for role, entries in self.orchestrator.auth_spec.items():
            for entry in entries:
                agent = self.agents.get(role)
                dest = Path(agent.home) / entry.dest if agent else None
                if not Path(entry.source).is_file() and not (dest and dest.is_file()):
                    problems.append(f"{role}: auth source missing: {entry.source}")
        return {"problems": problems, "binaries": binaries, "hc": hc_version}

