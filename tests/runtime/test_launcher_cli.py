"""Exercise launcher options without starting Docker or calling a model."""

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path / "repo"
    (root / "benchmark").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "source").mkdir()
    original = Path(__file__).resolve().parents[2] / "benchmark" / "run.sh"
    script = root / "benchmark" / "run.sh"
    script.write_bytes(original.read_bytes())
    docker = root / "bin" / "docker"
    docker.write_text('''#!/bin/bash
if [[ "$1" == inspect ]]; then echo running; fi
if [[ "$1" == exec ]]; then python3 -c 'import json,os,sys; json.dump(sys.argv[1:], open(os.environ["TEST_CAPTURE"], "w"))' "$@"; fi
if [[ "$1" == run ]]; then
    python3 -c 'import json,os; json.dump({k:os.environ[k] for k in ("FUZZING_DURATION", "FUZZAGENT_ENGINE", "FUZZAGENT_CONFIG", "HC_SESSION", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "FUZZAGENT_AUTH_DIR", "HTTP_PROXY", "FUZZAGENT_RESUME", "RAW_OUTPUT_DIR")}, open(os.environ["TEST_CAPTURE"], "w"))'
    mkdir -p "$RAW_OUTPUT_DIR/.campaign/source"
fi
''')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{root / 'bin'}:{os.environ['PATH']}",
               TEST_CAPTURE=str(root / "capture.json"), FUZZING_DURATION="99h",
               OPENAI_API_KEY="inherited", HTTP_PROXY="inherited")
    (root / ".env").write_text("OPENAI_MODEL=from-dotenv\n")

    def run(*args):
        return subprocess.run(["bash", str(script), *map(str, args)], cwd=root,
                              env=env, text=True, capture_output=True)

    return root, run


def test_cli_defaults_and_resume_ignore_host_environment(launcher):
    root, run = launcher
    result = run("launch", root / "source")
    assert result.returncode == 0, result.stderr
    captured = json.loads((root / "capture.json").read_text())
    output = Path(captured["RAW_OUTPUT_DIR"])
    assert output.parent == root / "output"
    assert re.fullmatch(r"source-\d{8}-\d{4}", output.name)
    assert captured["FUZZING_DURATION"] == "24h"
    assert captured["OPENAI_API_KEY"] == captured["OPENAI_MODEL"] == ""
    assert captured["HTTP_PROXY"] == ""
    assert captured["FUZZAGENT_ENGINE"] == "hc"
    assert captured["FUZZAGENT_AUTH_DIR"] == str(root / ".cache/auth")
    result = run("resume", output, "--duration", "5m")
    assert result.returncode == 0, result.stderr
    captured = json.loads((root / "capture.json").read_text())
    assert captured["FUZZING_DURATION"] == "5m"
    assert captured["FUZZAGENT_RESUME"] == "1"


def test_cli_options_and_validation(launcher):
    root, run = launcher
    result = run("launch", root / "source", "--output", "results with spaces",
                 "--duration", "10m", "--engine", "legacy", "--model", "model",
                 "--api-key", "key", "--api-base-url", "https://llm.invalid/v1",
                 "--auth-dir", "auth", "--http-proxy", "http://proxy.invalid")
    assert result.returncode == 0, result.stderr
    captured = json.loads((root / "capture.json").read_text())
    assert captured["RAW_OUTPUT_DIR"] == str(root / "results with spaces")
    assert captured["FUZZING_DURATION"] == "10m"
    assert captured["OPENAI_MODEL"] == "model"
    assert captured["OPENAI_API_KEY"] == "key"
    assert captured["OPENAI_BASE_URL"] == "https://llm.invalid/v1"
    assert captured["FUZZAGENT_AUTH_DIR"] == str(root / "auth")
    assert captured["HTTP_PROXY"] == "http://proxy.invalid"
    assert run("launch", root / "source", "--engine", "bad").returncode != 0
    assert run("launch", root / "source", "--duration").returncode != 0
    assert run("resume", root / "output/source", "--engine", "hc").returncode != 0


def test_default_output_is_a_new_timestamped_directory(launcher):
    root, run = launcher
    first = run("launch", root / "source")
    assert first.returncode == 0, first.stderr
    first_dir = Path(json.loads((root / "capture.json").read_text())["RAW_OUTPUT_DIR"])
    second = run("launch", root / "source")
    assert second.returncode == 0, second.stderr
    second_dir = Path(json.loads((root / "capture.json").read_text())["RAW_OUTPUT_DIR"])
    assert first_dir != second_dir
    assert first_dir.parent == second_dir.parent == root / "output"
    for path in (first_dir, second_dir):
        assert re.fullmatch(r"source-\d{8}-\d{4}(-\d+)?", path.name)


def test_run_wrapper_translates_local_mount_to_host_path():
    script = Path(__file__).resolve().parents[2] / "benchmark" / "run-wrapper.sh"
    env = os.environ.copy()
    env["FUZZAGENT_HOST_ROOT"] = "/data/exp/dkzou/libaflnet-dev"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; host_mount_path "$2"; host_mount_path "$3"',
            "bash",
            str(script),
            "/home/user/FuzzAgent/output/wolfssl-20261112-1632",
            "/tmp/outside",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "/data/exp/dkzou/libaflnet-dev/FuzzAgent/output/wolfssl-20261112-1632",
        "/tmp/outside",
    ]


def test_tui_uses_container_from_output(launcher):
    root, run = launcher
    out = root / "results with spaces"
    result = run("launch", root / "source", "--output", out)
    assert result.returncode == 0, result.stderr
    metadata = out / ".campaign/launcher.json"
    data = json.loads(metadata.read_text())
    assert data["container"] == "fuzzagent-source"
    data["container"] = "my-campaign"
    metadata.write_text(json.dumps(data))
    result = run("tui", "--output", out, "--", "--version")
    assert result.returncode == 0, result.stderr
    args = json.loads((root / "capture.json").read_text())
    assert "my-campaign" in args and args[-1] == "--version"
    del data["container"]
    metadata.write_text(json.dumps(data))
    assert run("tui", "--output", out).returncode != 0
    assert run("resume", out).returncode != 0
    metadata.write_text('{broken')
    assert run("tui", "--output", out).returncode != 0
    assert run("tui", "--output", root / "missing").returncode != 0
