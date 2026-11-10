"""Port of the legacy per-agent pre/exit/post hooks.

The legacy runtime decided whether an agent could finish by inspecting project
state (``is_agent_task_completed``) and injected reminders before a run
(``register_agent_pre_call``).  Those rules are the algorithm; they move here
unchanged so both the legacy loop and the hc runtime can share them, and so the
reminder texts stay byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import ProjectPaths

ROLES = (
    "ManagerAgent",
    "BuilderAgent",
    "DictionaryAgent",
    "SeedAgent",
    "HarnessAgent",
    "FuzzerExecutorAgent",
    "CrashAnalyzerAgent",
    "CoverageAnalyzerAgent",
)


@dataclass
class GuardContext:
    role: str
    paths: ProjectPaths
    tool_calls: list[str] = field(default_factory=list)
    task_id: str = ""
    state: dict[str, Any] = field(default_factory=dict)


def _reminder(text: str) -> str:
    return "This is a reminder message from user.\n" + text


def _last_index(calls: list[str], name: str) -> int:
    for index in range(len(calls) - 1, -1, -1):
        if calls[index] == name:
            return index
    return -1


def pre_task(ctx: GuardContext) -> dict[str, Any]:
    """State the guard needs, plus an optional reminder fed to the agent."""
    paths = ctx.paths
    note = ""

    if ctx.role in {"BuilderAgent", "HarnessAgent"}:
        ctx.state["tool_calls"] = []
        ctx.state["baseline_calls"] = 0

    elif ctx.role in {"SeedAgent", "DictionaryAgent"}:
        kind = "seed" if ctx.role == "SeedAgent" else "dictionary"
        waiting = paths.incomplete_projects(kind)
        if waiting:
            progress = paths.progress(kind)
            lines = [_reminder(
                f"Found {len(waiting)} projects waiting for {kind} corpus retrieval after "
                "previous execution. Here is the web retrieve progress:"
            )]
            for name, status in progress.items():
                lines.append(f"    - {name}: {status}")
            lines.append(f"Please continue the {kind} retrieval for the projects.")
            note = "\n".join(lines)
        ctx.state["progress_kind"] = kind

    elif ctx.role == "CoverageAnalyzerAgent":
        plateau = paths.plateau_fuzzers()
        ctx.state["can_return_early"] = not plateau
        ctx.state["guidance_baseline"] = set(paths.list_guidance())
        if ctx.state["can_return_early"]:
            states = paths.fuzzer_states()
            info = "".join(f"Fuzzer {fid:03d} is in state {state}.\n" for fid, state in sorted(states.items()))
            note = (
                "Here is the fuzzer stats:\n"
                + info
                + "\nBut you did not find any fuzzer in plateau state and is able to extract code "
                "coverage. Please directly exit and report this issue to let HarnessAgent or "
                "FuzzerAgent to continue."
            )

    return {"note": note, "state": ctx.state}


def check_exit(ctx: GuardContext) -> tuple[bool, str | None]:
    """Return ``(allowed, reminder)`` exactly like the legacy exit veto."""
    role = ctx.role
    paths = ctx.paths

    if role == "BuilderAgent":
        calls = ctx.tool_calls
        if calls.count("run_and_check_build_script") == 0:
            return False, _reminder(
                "No build script running tool call found. Please make sure you have called the "
                "tool `run_and_check_build_script` to run and check the build script.\n"
            )
        last = _last_index(calls, "run_and_check_build_script")
        if any(name in {"edit", "write_file"} for name in calls[last:]):
            return False, _reminder(
                "Found build script editing after the last `run_and_check_build_script` tool call. "
                "Please make sure you have called the tool `run_and_check_build_script` to run and "
                "check the build script after each editing.\n"
            )
        return True, None

    if role == "HarnessAgent":
        calls = ctx.tool_calls
        if calls.count("compile_and_check_harness") == 0:
            return False, _reminder(
                "No compile and check harness tool call found. Please make sure you have called the "
                "tool `compile_and_check_harness` to compile and check the harness.\n"
            )
        last = _last_index(calls, "compile_and_check_harness")
        if any(name in {"edit", "write_file"} for name in calls[last:]):
            return False, _reminder(
                "Found harness editing after the last `compile_and_check_harness` tool call. Please "
                "make sure you have called the tool `compile_and_check_harness` to compile the "
                "harness after each editing.\n"
            )
        return True, None

    if role in {"SeedAgent", "DictionaryAgent"}:
        kind = ctx.state.get("progress_kind") or ("seed" if role == "SeedAgent" else "dictionary")
        path = paths.progress_file(kind)
        if not path.exists():
            return False, _reminder(
                "No web retrieve progress file found. Please make sure you have recorded the web "
                "retrieve progress using the tool `track_web_retrieve_progress`.\n"
            )
        waiting = paths.incomplete_projects(kind)
        if waiting:
            progress = paths.progress(kind)
            lines = [_reminder(
                f"Found {len(waiting)} projects waiting for {kind} corpus retrieval after "
                "previous execution. Here is the web retrieve progress:"
            )]
            for name, status in progress.items():
                lines.append(f"    - {name}: {status}")
            lines.append(f"Please continue the {kind} retrieval for the projects.")
            return False, "\n".join(lines)
        return True, None

    if role == "CoverageAnalyzerAgent":
        if ctx.state.get("can_return_early"):
            return True, None
        baseline = set(ctx.state.get("guidance_baseline") or set())
        current = set(paths.list_guidance())
        if not (current - baseline):
            return False, _reminder(
                "As a Coverage Analyzer Agent, you are responsible for analyzing the code coverage "
                "of the project and generating coverage guidance. But you did not record any new "
                "coverage guidance by using the tool `generate_coverage_guidance_report`. Please "
                "generate a coverage analysis report before exiting."
            )
        return True, None

    return True, None


def post_report(ctx: GuardContext, report: str) -> str:
    """Append the guidance path to the report, like ``register_agent_post_call``."""
    if ctx.role != "CoverageAnalyzerAgent":
        return report
    if ctx.state.get("can_return_early"):
        return report + (
            "\n[*] No fuzzer in plateau state and is able to extract code coverage. Please directly "
            "exit and report this issue to let HarnessAgent or FuzzerAgent to continue."
        )
    baseline = set(ctx.state.get("guidance_baseline") or set())
    new_files = set(ctx.paths.list_guidance()) - baseline
    if not new_files:
        return report + "\n[*] No new coverage guidance files recorded in this round of analysis."
    new_file = sorted(new_files)[-1]
    return report + (
        f"\n[*] New coverage guidance files recorded in: "
        f"[{ctx.paths.guidance_dir / new_file}]. Please delegate this guidance to the HarnessAgent "
        "to generate the next harness."
    )


def record_tool_call(ctx: GuardContext, name: str) -> None:
    ctx.tool_calls.append(name)


def verify_artifacts(paths: ProjectPaths) -> list[str]:
    """Structural checks shared by ``campaignctl doctor`` and the tests."""
    problems: list[str] = []
    harness_ids = paths.harness_ids()
    fuzzer_ids = paths.fuzzer_ids()

    if harness_ids != list(range(len(harness_ids))):
        problems.append(f"harness ids are not contiguous: {harness_ids}")
    if fuzzer_ids != list(range(len(fuzzer_ids))):
        problems.append(f"fuzzer ids are not contiguous: {fuzzer_ids}")
    if len(harness_ids) != len(fuzzer_ids):
        problems.append(f"{len(harness_ids)} harnesses but {len(fuzzer_ids)} fuzzers")
    if len(paths.list_guidance()) != len(harness_ids):
        problems.append(
            f"{len(paths.list_guidance())} guidance files for {len(harness_ids)} harnesses"
        )
    for name in paths.list_crash_reports():
        report = paths.crash_report_dir / name / "crash_report.md"
        if not report.exists():
            problems.append(f"crash report {name} has no crash_report.md")
    return problems
