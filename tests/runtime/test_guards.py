"""Guard parity: the exit veto and reminders must keep the legacy semantics."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.runtime.artifacts import ProjectPaths
from src.runtime.guards import (
    GuardContext,
    check_exit,
    post_report,
    pre_task,
    record_build_stamp,
    record_harness_stamp,
    verify_artifacts,
)


@pytest.fixture()
def paths(tmp_path: Path) -> ProjectPaths:
    root = tmp_path / "out" / "cjson"
    src = tmp_path / "src" / "cjson"
    for directory in (
        "harnesses",
        "fuzzers",
        "dictionary",
        "seeds",
        "coverage_guidance",
        "crash_reports",
        "build",
        ".cache",
    ):
        (root / directory).mkdir(parents=True)
    src.mkdir(parents=True)
    return ProjectPaths(src=src, output=root, campaign=root / ".campaign")


def test_manager_always_may_exit(paths: ProjectPaths):
    ctx = GuardContext(role="ManagerAgent", paths=paths)
    pre_task(ctx)
    assert check_exit(ctx) == (True, None)


def test_builder_requires_a_build_call(paths: ProjectPaths):
    ctx = GuardContext(role="BuilderAgent", paths=paths, tool_calls=["write_file"])
    pre_task(ctx)
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "No build script running tool call found" in reminder


def test_builder_rejects_a_missing_or_newer_build_script(paths: ProjectPaths):
    ctx = GuardContext(
        role="BuilderAgent",
        paths=paths,
        task_id="build-1",
        tool_calls=["run_and_check_build_script"],
    )
    pre_task(ctx)
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "missing a successful check" in reminder

    script = paths.build_dir / "build.sh"
    script.write_text("echo old\n")
    record_build_stamp(paths, "build-1", script)
    future = time.time() + 5
    os.utime(script, (future, future))
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "modified after the last" in reminder


def test_builder_accepts_a_checked_build_script(paths: ProjectPaths):
    script = paths.build_dir / "build.sh"
    script.write_text("echo ok\n")
    record_build_stamp(paths, "build-1", script)
    ctx = GuardContext(
        role="BuilderAgent",
        paths=paths,
        task_id="build-1",
        tool_calls=["edit", "run_and_check_build_script"],
    )
    pre_task(ctx)
    assert check_exit(ctx) == (True, None)


def test_harness_rejects_a_newer_source(paths: ProjectPaths):
    source = paths.harness_dir / "harness_000.cpp"
    source.write_text("int x;\n")
    record_harness_stamp(paths, "harness-1", source)
    future = time.time() + 5
    os.utime(source, (future, future))
    ctx = GuardContext(
        role="HarnessAgent",
        paths=paths,
        task_id="harness-1",
        tool_calls=["compile_and_check_harness"],
    )
    pre_task(ctx)
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "modified after the last" in reminder


def test_harness_accepts_a_checked_source(paths: ProjectPaths):
    source = paths.harness_dir / "harness_000.cpp"
    source.write_text("int x;\n")
    record_harness_stamp(paths, "harness-1", source)
    ctx = GuardContext(
        role="HarnessAgent",
        paths=paths,
        task_id="harness-1",
        tool_calls=["compile_and_check_harness"],
    )
    pre_task(ctx)
    assert check_exit(ctx) == (True, None)


def test_harness_requires_compile_and_check(paths: ProjectPaths):
    ctx = GuardContext(role="HarnessAgent", paths=paths, tool_calls=["write_file"])
    pre_task(ctx)
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "No compile and check harness tool call found" in reminder


def test_dictionary_requires_progress_file(paths: ProjectPaths):
    ctx = GuardContext(role="DictionaryAgent", paths=paths)
    pre_task(ctx)
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "No web retrieve progress file found" in reminder

    paths.progress_file("dictionary").write_text(json.dumps({"zlib": "incomplete"}))
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "1 projects waiting" in reminder

    paths.progress_file("dictionary").write_text(json.dumps({"zlib": "completed"}))
    assert check_exit(ctx) == (True, None)


def test_seed_uses_seed_progress(paths: ProjectPaths):
    ctx = GuardContext(role="SeedAgent", paths=paths)
    pre_task(ctx)
    paths.progress_file("seed").write_text(json.dumps({"zlib": "completed"}))
    assert check_exit(ctx) == (True, None)


def test_coverage_requires_new_guidance(paths: ProjectPaths):
    paths.fuzzer_stat_file.write_text(
        json.dumps({"fuzzers": {"0": {"fuzzer_id": 0, "state": "PLATEAU"}}})
    )
    ctx = GuardContext(role="CoverageAnalyzerAgent", paths=paths)
    pre_task(ctx)
    allowed, reminder = check_exit(ctx)
    assert not allowed
    assert "did not record any new coverage guidance" in reminder

    (paths.guidance_dir / "harness_000_guidance.md").write_text("x")
    allowed, _ = check_exit(ctx)
    assert allowed
    report = post_report(ctx, "done")
    assert "New coverage guidance files recorded in" in report


def test_coverage_early_exit_without_plateau(paths: ProjectPaths):
    paths.fuzzer_stat_file.write_text(
        json.dumps({"fuzzers": {"0": {"fuzzer_id": 0, "state": "LIBRARY_BUG"}}})
    )
    ctx = GuardContext(role="CoverageAnalyzerAgent", paths=paths)
    result = pre_task(ctx)
    assert "did not find any fuzzer in plateau state" in result["note"]
    assert check_exit(ctx) == (True, None)


def test_verify_artifacts_flags_gaps(paths: ProjectPaths):
    (paths.harness_dir / "harness_000.cpp").write_text("")
    (paths.harness_dir / "harness_002.cpp").write_text("")
    problems = verify_artifacts(paths)
    assert any("not contiguous" in problem for problem in problems)
