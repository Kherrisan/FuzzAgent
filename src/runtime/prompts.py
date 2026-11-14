"""Load the *existing* role prompts and turn them into a first message.

The migration must not rewrite agent behaviour, so the prompt text still comes
from ``src/agents/<Role>/prompt.py``; this module only decides which variant to
use and how it is delivered to the external CLI.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from .config import AgentConfig, CampaignConfig

RESPONSE_FORMAT_ATTR = "RESPONSE_FORMAT_PROMPT"

RESPONSE_FORMAT_PROMPT = """
# Response Format
Your response must follow the following format:
- **Reasoning**: Reasoning about the feedback from the system (should be concise).
- **Thought**: Provide the strategy to achieve the goal (should be concise).
- **Action**: What you want to do next. (should be concise)

--- BEGIN OF EXAMPLE ---
**Reasoning**: The last function call get the location of source code directory.
**Thought**: First, I will check source directory contents.
**Action**: I will use my shell to check the source directory contents.
--- END OF EXAMPLE ---
"""

AGENT_DESCRIPTION_PROMPT = """
# Agent Team Members
You are the manager of the agent team. You can coordinate the agent team to achieve your goal. The following is the description of the agent team members:
{AGENTS}
"""


COMPLETION_DIRECTIVE = """

# Task completion (mandatory)
When your task is complete, you MUST call the MCP tool `finish_task` with
`status="done"` and pass a concise `exit_reason` describing what you did and the
evidence (files, commands, results). If `finish_task` returns a reminder, fix the
problem and call it again.
If you cannot complete the task - missing input, a decision only the manager can
make, a request that conflicts with your role, or a failed prerequisite - call
`finish_task(status="blocked", exit_reason=<exactly what you need>)`. If the work
failed, use `status="failed"`. Never end your turn without calling `finish_task`.
"""

WORKSPACE_DIRECTIVE = """

# Output directory hygiene
`$OUTPUT` has a fixed layout shared by every tool: `build/`, `harnesses/`,
`fuzzers/`, `dictionary/`, `seeds/`, `coverage_guidance/`, `crash_reports/`,
`.cache/`. Never create new entries at its root. Put temporary or scratch
material (package indexes, install prefixes, unpacked tarballs, logs) under
`$OUTPUT/.cache/<name>` and clean it up when the task is done.
"""

MANAGER_COMPLETION_DIRECTIVE = """

# Every round
Before `finish_task`, call `observe_agent_states` to identify the progress each
agent made: its `status` and `turn`. If any agent still has progress, is
`working`, or shows no progress only because it is running a long task and
several checks are unchanged, keep waiting. Do not call `finish_task`.
Call `observe_current_states` to see whether the artifacts changed. If they
changed, do not call `finish_task`.
Stay in this turn and poll with `observe_agent_states` and `wait_agent_task`
until that agent stops or exits. Then end the round with `finish_task` and a
`status` of `done`, `failed`, or `blocked`, as that tool describes.
"""


def _import_attr(source: str) -> str:
    module_name, _, attr = source.partition(":")
    if not module_name or not attr:
        raise ValueError(f"prompt source must be 'module:ATTR', got {source!r}")
    module = importlib.import_module(module_name)
    try:
        return str(getattr(module, attr))
    except AttributeError as exc:
        raise AttributeError(f"{module_name} has no attribute {attr}") from exc


def load_system_prompt(agent: AgentConfig, campaign: CampaignConfig) -> str:
    source = agent.prompt.source
    if not campaign.coverage_guidance and agent.prompt.guidance_variant:
        source = agent.prompt.guidance_variant
    prompt = _import_attr(source)
    prompt = prompt.replace("`exit` tool", "`finish_task` tool").replace("exit tool", "finish_task tool")
    prompt += WORKSPACE_DIRECTIVE
    if agent.role == campaign.manager_role:
        prompt += MANAGER_COMPLETION_DIRECTIVE
    else:
        prompt += COMPLETION_DIRECTIVE
    if agent.prompt.response_format:
        prompt += RESPONSE_FORMAT_PROMPT
    return prompt


def load_initial_input() -> str:
    try:
        return _import_attr("src.agents.ManagerAgent.prompt:INITIAL_INPUT_MESSAGE")
    except Exception:  # pragma: no cover - prompt module changed
        return "Continue the fuzzing campaign. Observe the current state and dispatch the next task."


def render_card_description(card_path: str | Path) -> str:
    """Same text the legacy roster prompt used, from the A2A card JSON."""
    data = json.loads(Path(card_path).read_text(encoding="utf-8"))
    lines = ["# @agent", "# Overall details", f"name: {data.get('name', '')}",
             f"docstring: {data.get('description', '')}",
             "skills: # A set of capabilities the agent can perform"]
    for skill in data.get("skills") or []:
        lines.append(f"- name: {skill.get('name', '')}")
        lines.append(f"  description: {skill.get('description', '')}")
        lines.append("  tags: # Set of tagwords describing classes of capabilities for this specific")
        for tag in skill.get("tags") or []:
            lines.append(f"    -{tag}")
        lines.append("  examples: # The set of example scenarios that the skill can perform")
        for example in skill.get("examples") or []:
            lines.append(f"    - {example}")
    return "\n".join(lines)


def roster_prompt(campaign: CampaignConfig, agents: dict[str, AgentConfig]) -> str:
    parts = []
    for role, agent in agents.items():
        if role == campaign.manager_role or not agent.card:
            continue
        path = Path(agent.card)
        if not path.is_absolute():
            path = Path(campaign.base_dir or ".") / path
        if path.exists():
            parts.append(render_card_description(path))
    template = AGENT_DESCRIPTION_PROMPT
    return template.format(AGENTS="\n\n".join(parts)) if parts else ""


def compose_first_message(agent: AgentConfig, campaign: CampaignConfig, task: str) -> str:
    system = load_system_prompt(agent, campaign)
    if agent.role == campaign.manager_role:
        system += "\n" + roster_prompt(campaign, _all_agents(campaign))
    return system.rstrip() + "\n\n# Your task\n" + task.strip() + "\n"


def _all_agents(campaign: CampaignConfig) -> dict[str, AgentConfig]:
    from .config import load_agents

    return load_agents(campaign.agents_dir, {"OUTPUT": campaign.output, "SRC": campaign.src})
