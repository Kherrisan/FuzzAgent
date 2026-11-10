"""Execute one registered tool as a subprocess and return a legacy-shaped result.

The legacy ACI kept a single interactive bash alive and typed commands into it.
That shared pty is exactly what the migration removes: every call here is an
independent process with an explicit cwd/environment, full output persisted to
a log file, and the same ``Command executed with exit code N.`` prefix the
models already know.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import ProjectPaths
from .registry import SOURCE_TOOL_LIST, TOOLS_DIR, ToolSpec

MAX_OUTPUT_CHARACTERS = 100_000


@dataclass
class ToolResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    log_path: str | None = None

    def as_text(self) -> str:
        code = "timeout" if self.timed_out else self.exit_code
        body = self.output
        if self.log_path:
            body += f"\n[full output: {self.log_path}]"
        return f"Command executed with exit code {code}.\n{body}"


def resolve_script(name: str) -> str | None:
    for path in sorted(TOOLS_DIR.glob(f"*/bin/{name}")):
        if path.is_file():
            return str(path)
    return shutil.which(name)


def _truncate(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    result = "\n".join(lines)
    if len(result) > MAX_OUTPUT_CHARACTERS:
        half = MAX_OUTPUT_CHARACTERS // 2
        result = (
            result[:half]
            + f"\n... [Truncated {len(result) - MAX_OUTPUT_CHARACTERS} characters] ...\n"
            + result[-half:]
        )
    return result


def _state_file(paths: ProjectPaths, role: str) -> Path:
    directory = (paths.campaign or paths.output / ".campaign") / "agents" / role
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "aci_state.env"


def _build_command(spec: ToolSpec, arguments: dict[str, Any], paths: ProjectPaths, role: str) -> tuple[list[str], str | None]:
    """Return ``(argv, stdin_text)`` for the tool call."""
    if spec.name == "bash":
        command = arguments.get("command", "")
        return ["bash", "-lc", command], None

    stdin_text: str | None = None
    positional: list[str] = []
    for argument in spec.arguments:
        value = arguments.get(argument.name)
        if value is None:
            continue
        if argument.redirect:
            stdin_text = str(value)
        else:
            positional.append(str(value))

    if spec.name in SOURCE_TOOL_LIST:
        script_dir = TOOLS_DIR / "file" / "bin"
        state = _state_file(paths, role)
        wrapper = f"""
            set -o pipefail
            state={shlex.quote(str(state))}
            [ -f "$state" ] && . "$state"
            export WINDOW="${{WINDOW:-100}}"
            export OVERLAP="${{OVERLAP:-2}}"
            source {shlex.quote(str(script_dir / 'defaults.sh'))}
            {spec.name} "$@"
            rc=$?
            env | grep -E '^(CURRENT_FILE|CURRENT_LINE|OVERLAP|WINDOW|SCROLL_COUNT)=' > "$state" || true
            exit $rc
        """
        return ["bash", "-c", wrapper, spec.name, *positional], stdin_text

    script = resolve_script(spec.name)
    if script is None:
        raise FileNotFoundError(f"no executable found for tool {spec.name!r}")
    argv = [script, *positional]
    if script.endswith(".py"):
        argv = ["python3", script, *positional]
    elif script.endswith(".sh"):
        argv = ["bash", script, *positional]
    return argv, stdin_text


def run_tool(
    spec: ToolSpec,
    arguments: dict[str, Any],
    paths: ProjectPaths,
    role: str,
    task_id: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> ToolResult:
    argv, stdin_text = _build_command(spec, arguments, paths, role)
    tool_timeout = int(timeout if timeout is not None else spec.timeout)

    process_env = dict(os.environ)
    process_env.update(
        {
            "SRC": str(paths.src),
            "OUTPUT": str(paths.output),
            "HARNESS_DIR": str(paths.harness_dir),
            "FUZZER_DIR": str(paths.fuzzer_dir),
            "DICTIONARY_DIR": str(paths.dictionary_dir),
            "SEED_DIR": str(paths.seed_dir),
            "COVERAGE_GUIDANCE_DIR": str(paths.guidance_dir),
            "CRASH_REPORT": str(paths.crash_report_dir),
            "CACHE_DIR": str(paths.cache_dir),
            "PROJECT_BUILD_DIR": str(paths.build_dir),
            "WORK": str(paths.build_dir / "sanitizer"),
            "LIB_FUZZER_LINK_BUILD_DIR": str(paths.build_dir / "sanitizer"),
            "WORK_DEP": str(paths.build_dir / "sanitizer" / "dependencies"),
            "LIB_COVERAGE_BUILD_DIR": str(paths.build_dir / "coverage"),
            "PATH": f"{os.environ.get('PATH', '')}:/usr/local/bin",
        }
    )
    if env:
        process_env.update(env)

    log_dir = (paths.campaign or paths.output / ".campaign") / "logs" / "tools" / (task_id or "adhoc")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{spec.name}-{int(time.time() * 1000)}.log"

    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=tool_timeout if tool_timeout > 0 else None,
            cwd=str(paths.src),
            env=process_env,
            start_new_session=True,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):  # pragma: no cover - text mode normally
            output = output.decode("utf-8", "replace")
        code = None
    except OSError as exc:
        output = f"{type(exc).__name__}: {exc}"
        code = 127

    elapsed = time.time() - started
    header = (
        f"# tool={spec.name} role={role} task={task_id} exit={code} "
        f"timeout={timed_out} elapsed={elapsed:.1f}s\n"
    )
    try:
        log_path.write_text(header + output, encoding="utf-8")
    except OSError:  # pragma: no cover - disk full etc.
        log_path = None

    return ToolResult(
        exit_code=code,
        output=_truncate(output, spec.max_output_lines),
        timed_out=timed_out,
        log_path=str(log_path) if log_path else None,
    )
