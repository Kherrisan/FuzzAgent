"""Dispatch plumbing, tested against a fake hc core (no real hc required)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.runtime.config import CampaignConfig, load_agent, render_agent
from src.runtime.hc import Terminal
from src.runtime.orchestrator import Orchestrator

REPO = Path(__file__).resolve().parents[2]


class FakeHC:
    def __init__(self) -> None:
        self.terminals: dict[str, Terminal] = {}
        self.sent: list[str] = []
        self.launched: list[list[str]] = []
        self.launch_labels: list[dict[str, str]] = []
        self.killed: list[str] = []
        self.interrupted: list[str] = []
        self.read_text = "fake screen"

    def get(self, name: str):
        return self.terminals.get(name)

    def run_bg(self, name, argv, cwd=None, env=None, labels=None, parent=None, keep=True,
               outlive_parent=True, scrub_env=None):
        self.launched.append(list(argv))
        self.launch_labels.append(dict(labels or {}))
        terminal = Terminal(
            address="t1", name=name, state="live", status={"state": "idle"}, reported=True, turn=0
        )
        self.terminals[name] = terminal
        return terminal

    def send(self, name, text, if_not_working=True, force=False):
        self.sent.append(text)
        self.terminals[name].turn += 1
        return {"started": True}

    def wait_turn(self, name, previous_turn, timeout=7200, poll=10):
        return self.terminals[name]

    def wait_idle(self, name, timeout=180, poll=5):
        return self.terminals[name]

    def read(self, name, lines=200):
        return self.read_text

    def interrupt(self, name, timeout=10):
        self.interrupted.append(name)
        return {}

    def remove(self, name):
        self.terminals.pop(name, None)

    def kill(self, name):
        self.killed.append(name)

    def keys(self, name, key):
        return None


class OrchestratorStub(Orchestrator):
    def collect_result(self, directory: Path, agent, terminal=None, name=None):
        if terminal is not None and (
            terminal.is_blocked or terminal.state_word == "error" or not terminal.is_live
        ):
            return super().collect_result(directory, agent, terminal=terminal, name=name)
        (directory / "finish_task.json").write_text(
            json.dumps({"status": "done", "report": f"finished {agent.role}", "allowed": True}),
            encoding="utf-8",
        )
        return "done", f"finished {agent.role}"


@pytest.fixture()
def campaign(tmp_path: Path) -> CampaignConfig:
    src = tmp_path / "src" / "cjson"
    out = tmp_path / "out" / "cjson"
    for directory in ("harnesses", "fuzzers", "dictionary", "seeds", "coverage_guidance",
                      "crash_reports", ".cache"):
        (out / directory).mkdir(parents=True)
    src.mkdir(parents=True)
    return CampaignConfig(
        name="cjson", target="cjson", src=str(src), output=str(out), session="fz-cjson",
        base_dir=str(REPO),
    )


@pytest.fixture()
def orchestrator(campaign, tmp_path):
    agent = render_agent(load_agent(REPO / "config" / "agents" / "HarnessAgent.yaml"), campaign)
    agent.home = str(tmp_path / "home")
    agent.cwd = str(tmp_path / "work")
    agent.source_path = str(REPO / "config" / "agents" / "HarnessAgent.yaml")
    fake = FakeHC()
    return OrchestratorStub(campaign, {"HarnessAgent": agent}, hc=fake, observer=lambda: '{"ok": true}'), fake


def test_manager_dispatch_asks_for_finish_task(campaign, tmp_path):
    agent = render_agent(load_agent(REPO / "config" / "agents" / "ManagerAgent.yaml"), campaign)
    agent.home = str(tmp_path / "home")
    agent.cwd = str(tmp_path / "work")
    agent.source_path = str(REPO / "config" / "agents" / "ManagerAgent.yaml")
    fake = FakeHC()
    orch = OrchestratorStub(campaign, {"ManagerAgent": agent}, hc=fake, observer=lambda: "{}")
    orch.dispatch("ManagerAgent", "continue")
    assert "finish_task" in fake.sent[0]
    prime = (Path(campaign.output) / ".campaign" / "agents" / "ManagerAgent" / "prime.md").read_text()
    assert "call the MCP tool `finish_task`" in prime
    assert "Do not call `finish_task`" not in prime


def test_first_dispatch_primes_the_agent(orchestrator, campaign):
    orch, fake = orchestrator
    result = orch.dispatch("HarnessAgent", "write harness_000")
    assert "finished HarnessAgent" in result
    assert fake.launch_labels[0]["herdr.space"] == "HarnessAgent"
    assert "This is the current states of the fuzzing tasks" in result
    assert "role instructions are in" in fake.sent[0]
    prime = Path(orch.role_dir("HarnessAgent") / "prime.md").read_text()
    assert "Harness Generator Agent" in prime  # system prompt delivered by file
    assert "write harness_000" in prime

    orch.dispatch("HarnessAgent", "write harness_001")
    assert "next task" in fake.sent[1]  # primed session afterwards
    task_file = Path(campaign.output) / ".campaign" / "tasks"
    latest = sorted(task_file.iterdir())[-1]
    assert "write harness_001" in (latest / "task_with_context.md").read_text()


def test_dispatch_writes_task_artifacts(orchestrator, campaign):
    orch, _ = orchestrator
    orch.dispatch("HarnessAgent", "compile it")
    tasks = sorted((Path(campaign.output) / ".campaign" / "tasks").iterdir())
    assert len(tasks) == 1
    assert (tasks[0] / "task.md").read_text() == "compile it"
    meta = json.loads((tasks[0] / "meta.json").read_text())
    assert meta["status"] == "done"
    assert meta["role"] == "HarnessAgent"
    journal = (Path(campaign.output) / ".campaign" / "journal.jsonl").read_text().splitlines()
    events = [json.loads(line)["event"] for line in journal]
    assert "dispatch" in events and "finished" in events


def test_resume_brief_lists_artifacts(orchestrator, campaign):
    orch, _ = orchestrator
    (Path(campaign.output) / "harnesses" / "harness_000.cpp").write_text("x")
    brief = orch.resume_brief()
    assert "Campaign resume brief" in brief
    assert "Harnesses: 1" in brief


def test_blocked_terminal_is_reported(orchestrator):
    orch, fake = orchestrator
    terminal = fake.terminals.get("harnessagent")
    # force a blocked status for the next wait
    original = fake.wait_turn

    def blocked(name, previous_turn, timeout=7200, poll=10):
        t = fake.terminals[name]
        t.status = {"state": "blocked", "msg": "Trust this folder?"}
        return t

    fake.wait_turn = blocked  # type: ignore[assignment]
    try:
        result = orch.dispatch("HarnessAgent", "do it")
    finally:
        fake.wait_turn = original  # type: ignore[assignment]
    assert "status blocked" in result
    assert "Trust this folder?" in result


def test_nonblocking_dispatch_returns_without_waiting(orchestrator, campaign):
    orch, fake = orchestrator
    waited = {"called": False}

    def boom(*args, **kwargs):
        waited["called"] = True
        raise AssertionError("wait_turn")

    fake.wait_turn = boom  # type: ignore[assignment]
    text = orch.dispatch("HarnessAgent", "compile it", wait=False)
    assert waited["called"] is False
    assert text.startswith("Dispatched HarnessAgent as ")
    assert text.endswith("on terminal harnessagent.")
    assert "finish_task" in fake.sent[0]
    tasks = sorted((Path(campaign.output) / ".campaign" / "tasks").iterdir())
    meta = json.loads((tasks[0] / "meta.json").read_text())
    assert meta["status"] == "running"
    task_id = text.split(" as ", 1)[1].split(" on ", 1)[0]
    assert task_id == meta["task_id"]
    marker = Path(campaign.output) / ".campaign" / "agents" / "HarnessAgent" / "current_task"
    assert marker.read_text().strip() == task_id
    journal = (Path(campaign.output) / ".campaign" / "journal.jsonl").read_text().splitlines()
    events = [json.loads(line)["event"] for line in journal]
    assert "dispatch" in events
    assert "finished" not in events

    again = orch.dispatch("HarnessAgent", "compile the next one", wait=False)
    assert fake.killed == ["harnessagent"]
    assert again.startswith("Dispatched HarnessAgent as ")
    assert again.split(" as ", 1)[1].split(" on ", 1)[0] != task_id


def test_poll_task_reads_finish_file(orchestrator, campaign):
    orch, _ = orchestrator
    task_id = "HarnessAgent-9"
    directory = Path(campaign.output) / ".campaign" / "tasks" / task_id
    directory.mkdir(parents=True)
    assert orch.poll_task(task_id) == f"Task {task_id} is not finished."
    (directory / "finish_task.json").write_text(
        json.dumps({"status": "done", "report": "built the library"}), encoding="utf-8"
    )
    text = orch.poll_task(task_id)
    assert text.startswith("built the library\n")
    assert "current states of the fuzzing tasks" in text


def _terminal(fake, state="idle", turn=1):
    terminal = Terminal(
        address="t1", name="harnessagent", state="live",
        status={"state": state}, reported=True, turn=turn,
    )
    fake.terminals["harnessagent"] = terminal
    return terminal


def _running_task(campaign, task_id, **meta_updates):
    directory = Path(campaign.output) / ".campaign" / "tasks" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "task_id": task_id,
        "role": "HarnessAgent",
        "status": "running",
        "started": time.time(),
        "terminal": "t1",
        "terminal_name": "harnessagent",
        "started_turn": 0,
    }
    meta.update(meta_updates)
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return directory


def test_poll_task_detects_turn_end_without_finish(orchestrator, campaign):
    orch, fake = orchestrator
    task_id = "HarnessAgent-10"
    directory = _running_task(campaign, task_id)
    _terminal(fake, "idle", 1)
    fake.read_text = "  Reasoning: cannot run two fuzzers\n  Action: Choose target 000 or 001"

    text = orch.poll_task(task_id)
    assert "is blocked" in text
    assert "Choose target 000 or 001" in text
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["status"] == "blocked"
    assert meta["detected_by"] == "poll_task"
    # not a fake completion: no finish_task.json
    assert not (directory / "finish_task.json").exists()


def test_poll_task_keeps_running_while_working(orchestrator, campaign):
    orch, fake = orchestrator
    task_id = "HarnessAgent-11"
    _running_task(campaign, task_id)
    _terminal(fake, "working", 0)
    assert orch.poll_task(task_id) == f"Task {task_id} is not finished."


def test_poll_task_marks_failed_when_screen_shows_error(orchestrator, campaign):
    orch, fake = orchestrator
    task_id = "HarnessAgent-12"
    directory = _running_task(campaign, task_id)
    _terminal(fake, "idle", 1)
    fake.read_text = "Traceback ...\nModuleNotFoundError: No module named 'yaml'"
    text = orch.poll_task(task_id)
    assert "is failed" in text
    assert json.loads((directory / "meta.json").read_text())["status"] == "failed"


def test_poll_task_marks_failed_when_terminal_finished(orchestrator, campaign):
    orch, fake = orchestrator
    task_id = "HarnessAgent-13"
    _running_task(campaign, task_id)
    terminal = _terminal(fake, "idle", 1)
    terminal.state = "finished"
    terminal.exit_code = 1
    text = orch.poll_task(task_id)
    assert "is failed" in text


def test_poll_task_prefers_blocked_finish_file(orchestrator, campaign):
    orch, _ = orchestrator
    task_id = "HarnessAgent-14"
    directory = _running_task(campaign, task_id)
    (directory / "finish_task.json").write_text(
        json.dumps({"status": "blocked", "report": "need a harness id"}), encoding="utf-8"
    )
    text = orch.poll_task(task_id)
    assert "is blocked" in text
    assert "need a harness id" in text


def test_poll_task_is_idempotent(orchestrator, campaign):
    orch, fake = orchestrator
    task_id = "HarnessAgent-15"
    _running_task(campaign, task_id)
    _terminal(fake, "idle", 1)
    first = orch.poll_task(task_id)
    second = orch.poll_task(task_id)
    assert first == second
    journal = (Path(campaign.output) / ".campaign" / "journal.jsonl").read_text().splitlines()
    finished = [line for line in journal if json.loads(line).get("event") == "finished"
                and json.loads(line).get("detected_by") == "poll_task"]
    assert len(finished) == 1


def test_poll_task_interrupts_stuck_task(orchestrator, campaign):
    orch, fake = orchestrator
    task_id = "HarnessAgent-16"
    _running_task(campaign, task_id, started=time.time() - 8000)
    _terminal(fake, "working", 0)
    text = orch.poll_task(task_id)
    assert "interrupted" in text
    assert fake.interrupted == ["harnessagent"]
