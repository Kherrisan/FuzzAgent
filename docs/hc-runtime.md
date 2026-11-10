# hc runtime (herdr-core) — design and usage

The multi-agent workflow is unchanged: a Manager observes the campaign state and
dispatches one specialized role at a time; every role keeps its own prompt,
tool set and exit guard. What changed is the *runtime*: agents are now
interactive coding-agent CLIs running inside `hc` terminals, and the glue is an
MCP server instead of the in-process `BaseAgent` loop.

## Status matrix (verified against this machine)

| CLI | version | OSC 7501 | notes |
|---|---|---|---|
| codex | 0.163.0 | yes | must run with `--no-daemon` under hc |
| grok | 1.0.77 | yes | `program_status = "auto"`; reports `blocked` for trust prompts |
| dsh-tui | **≥ 0.17.0** | yes | own `ink/termio/osc7501.js`; the 0.15.0 on the dev box predates it |
| pi | **≥ 1.1.0** | yes | upstream "report program status via OSC 7501" |

grok is installed by the image at build time with the official installer:
`curl -fsSL https://x.ai/cli/install.sh | bash -s 1.0.77` (verified in a clean
`ubuntu:24.04`: installs a static binary in `$HOME/.grok` and symlinks
`/usr/local/bin/grok`; needs the build proxy, which Docker passes to `RUN`).

The old `HERDR_*` channel is **not** used: `env_scrub` removes those variables
from every agent terminal so dsh-tui reports through OSC 7501 like the others.

## Layout

```
config/campaign.yaml           campaign settings (paths, budget, hc session)
config/agents/<Role>.yaml      one independent config per role
src/runtime/config.py          dataclass model + ${VAR} expansion
src/runtime/registry.py        src/tools/*/config.yaml -> MCP inputSchema
src/runtime/executor.py        one subprocess per tool call, full log on disk
src/runtime/guards.py          legacy pre/exit/post hooks (shared with legacy)
src/runtime/hc.py              typed wrapper over the hc CLI
src/runtime/drivers.py         per-CLI launch/MCP/resume adapters
src/runtime/orchestrator.py    dispatch: task -> hc send/wait -> report -> snapshot
src/runtime/campaign.py        campaignctl controller (rounds, budget, doctor)
src/runtime/mcp/server.py      stdio MCP server, one process per agent turn
bin/fuzzagent-mcp              MCP entrypoint (used by every CLI config)
bin/campaignctl                operator entrypoint
$OUTPUT/.campaign/             state, task journal, transcripts, role homes
```

## Per-agent configuration

Each `config/agents/<Role>.yaml` is independent: driver, launch argv, model,
env, isolated `HOME` (so CLI sessions/configs never mix), prompt source, tool
allowlist, session policy, timeouts, snapshot policy and status mode. Paths are
expanded from `SRC`/`OUTPUT` when the campaign starts.

The tool allowlists are the legacy ones, so the prompts keep working; the MCP
schemas are byte-identical to the legacy OpenAI function schemas (covered by
`tests/runtime/test_registry_parity.py`).

## Running

```bash
# legacy (unchanged)
bash benchmark/run_benchmark.sh

# hc runtime
export SRC=/src/cjson OUTPUT=$PWD/output/cjson HC_SESSION=fz-cjson
campaignctl --config config/campaign.yaml prepare
campaignctl --config config/campaign.yaml up --resume
campaignctl --config config/campaign.yaml status
campaignctl --config config/campaign.yaml doctor
campaignctl --config config/campaign.yaml resume-brief
campaignctl --config config/campaign.yaml rollback --to last-good
```

`doctor` checks: launcher binaries, `hc --version`, contiguous harness/fuzzer
ids, guidance count vs harness count, crash report completeness.

## Resume semantics

1. **warm** — the hc terminal still exists; just send the next task.
2. **session** — relaunch the CLI with its resume flag (`codex resume <id>`,
   `grok -r <id>`, `dsh --profile dsh-tui --resume <id>`, `pi -r <id>`); the
   session store lives in the role's `HOME` under `$OUTPUT/.campaign/agents/`,
   so it survives a container restart.
3. **brief** — no session: start fresh and prepend `resume_brief()` (artifact
   inventory, interrupted tasks, recent journal entries).

`$OUTPUT/.campaign/journal.jsonl` is the append-only record of dispatch/finish/
snapshot/interrupt/rollback; `tasks/<id>/{task.md,tools.jsonl,report.md,meta.json}`
keeps everything a guard or a human needs after a crash.

## Verification

```bash
FUZZAGENT_SKIP_ENV_INIT=1 PYTHONPATH=. .cache/venv/bin/python -m pytest tests/runtime -q
python3 tools/baseline_report.py output/openssl output/gnutls-24h-20261108 \
        --out-dir tests/fixtures/baseline
```

Frozen legacy baselines: `output/openssl` (15.83% coverage, 27 harnesses, 3
crash reports) and `output/gnutls-24h-20261108` (23.59%, 29 harnesses, 12 crash
reports). Compare structure and pacing first; coverage is a relative threshold
because the baseline did not record its model settings.

## Remaining work

- P2 probe in the image: codex/dsh-tui 0.19/grok installed (pi still to do) + `hc`, run
  `doctor`, confirm `reported: true` for all four under hc.
- P3 shadow run: legacy manager vs hc manager on a short openssl campaign.
- P4 interruption drills (`kill -9` controller, container restart, host reboot).
- P5 remove `src/a2a`, `BaseAgent`, the shared ACI pty and the summarizer once
  the shadow runs pass.
