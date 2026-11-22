"""Port of the legacy per-agent pre/exit/post hooks.

The legacy runtime decided whether an agent could finish by inspecting project
state (``is_agent_task_completed``) and injected reminders before a run
(``register_agent_pre_call``).  Those rules are the algorithm; they move here
unchanged so both the legacy loop and the hc runtime can share them, and so the
reminder texts stay byte-identical.
"""

from __future__ import annotations

import json
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


def check_stamp_path(paths: ProjectPaths, task_id: str) -> Path:
    root = paths.campaign or paths.output / ".campaign"
    return root / "tasks" / task_id / "check_stamp.json"


def load_check_stamp(paths: ProjectPaths, task_id: str) -> dict[str, Any]:
    try:
        data = json.loads(check_stamp_path(paths, task_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def record_build_stamp(paths: ProjectPaths, task_id: str, build_script: Path) -> None:
    data = load_check_stamp(paths, task_id)
    data["build"] = {"path": str(build_script), "mtime": _mtime(build_script)}
    _write_stamp(paths, task_id, data)


def record_harness_stamp(paths: ProjectPaths, task_id: str, harness: Path) -> None:
    data = load_check_stamp(paths, task_id)
    harnesses = data.get("harness")
    if not isinstance(harnesses, dict):
        harnesses = {}
    harnesses[str(harness)] = _mtime(harness)
    data["harness"] = harnesses
    _write_stamp(paths, task_id, data)


def _write_stamp(paths: ProjectPaths, task_id: str, data: dict[str, Any]) -> None:
    path = check_stamp_path(paths, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def harness_source(paths: ProjectPaths, harness_id: object) -> Path:
    return paths.harness_dir / f"harness_{int(harness_id):03d}.cpp"


def _newer(path: Path, recorded: object) -> bool:
    current = _mtime(path)
    if current is None or recorded is None:
        return True
    return current > float(recorded)


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
        build = load_check_stamp(paths, ctx.task_id).get("build") or {}
        script = Path(str(build.get("path") or paths.build_dir / "build.sh"))
        if not isinstance(build, dict) or _newer(script, build.get("mtime")):
            return False, _reminder(
                "build.sh is missing a successful check, or it was modified after the last "
                "`run_and_check_build_script` call. Run `run_and_check_build_script` again "
                "after editing the build script.\n"
            )
        return True, None

    if role == "HarnessAgent":
        calls = ctx.tool_calls
        if calls.count("compile_and_check_harness") == 0:
            return False, _reminder(
                "No compile and check harness tool call found. Please make sure you have called the "
                "tool `compile_and_check_harness` to compile and check the harness.\n"
            )
        harnesses = load_check_stamp(paths, ctx.task_id).get("harness") or {}
        if not isinstance(harnesses, dict) or not harnesses or any(
            _newer(Path(path), recorded) for path, recorded in harnesses.items()
        ):
            return False, _reminder(
                "A harness source is missing a successful check, or it was modified after the last "
                "`compile_and_check_harness` call. Compile the harness again after editing it.\n"
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
