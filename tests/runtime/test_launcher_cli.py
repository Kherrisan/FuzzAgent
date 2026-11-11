"""Exercise launcher options without starting Docker or calling a model."""

import json
import os
from pathlib import Path
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
if [[ "$1" == compose && "$2" != version ]]; then
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
    assert captured["FUZZING_DURATION"] == "24h"
    assert captured["OPENAI_API_KEY"] == captured["OPENAI_MODEL"] == ""
    assert captured["HTTP_PROXY"] == ""
    assert captured["FUZZAGENT_ENGINE"] == "hc"
    assert captured["FUZZAGENT_AUTH_DIR"] == str(root / ".cache/auth")
    result = run("resume", root / "output/source", "--duration", "5m")
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
