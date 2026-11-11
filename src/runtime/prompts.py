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
    if agent.prompt.response_format:
        prompt += _import_attr("src.planning.prompt:RESPONSE_FORMAT_PROMPT")
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
    from .prompts import _import_attr as _attr

    parts = []
    for role, agent in agents.items():
        if role == campaign.manager_role or not agent.card:
            continue
        path = Path(agent.card)
        if not path.is_absolute():
            path = Path(campaign.base_dir or ".") / path
        if path.exists():
            parts.append(render_card_description(path))
    template = _attr("src.planning.prompt:AGENET_DESCRIPTION_PROMPT")
    return template.format(AGENTS="\n\n".join(parts)) if parts else ""


def compose_first_message(agent: AgentConfig, campaign: CampaignConfig, task: str) -> str:
    system = load_system_prompt(agent, campaign)
    if agent.role == campaign.manager_role:
        system += "\n" + roster_prompt(campaign, _all_agents(campaign))
    return system.rstrip() + "\n\n# Your task\n" + task.strip() + "\n"


def _all_agents(campaign: CampaignConfig) -> dict[str, AgentConfig]:
    from .config import load_agents

    return load_agents(campaign.agents_dir, {"OUTPUT": campaign.output, "SRC": campaign.src})
