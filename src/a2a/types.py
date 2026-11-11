from pydantic import BaseModel
from typing import List


class AgentProvider(BaseModel):
    organization: str
    url: str | None = None


class AgentCapabilities(BaseModel):
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


class AgentAuthentication(BaseModel):
    schemes: List[str]
    credentials: str | None = None


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str | None = None
    tags: List[str] | None = None
    examples: List[str] | None = None
    inputModes: List[str] | None = None
    outputModes: List[str] | None = None


class AgentCard(BaseModel):
    name: str
    description: str | None = None
    url: str
    provider: AgentProvider | None = None
    version: str
    documentationUrl: str | None = None
    capabilities: AgentCapabilities
    authentication: AgentAuthentication | None = None
    defaultInputModes: List[str] = ["text"]
    defaultOutputModes: List[str] = ["text"]
    skills: List[AgentSkill]

    def get_agent_description(self) -> str:
        # Build the lines of the resulting YAML
        lines = ["# @agent"]

        lines.append("# Overall details")
        lines.append("name: " + self.name)
        lines.append(f"docstring: {self.description}")

        # Skill section
        lines.append("skills: # A set of capabilities the agent can perform")
        for skill in self.skills:
            lines.append(f"- name: {skill.name}")
            lines.append(f"  description: {skill.description}")
            lines.append("  tags: # Set of tagwords describing classes of capabilities for this specific")
            for tag in skill.tags:
                lines.append(f"    -{tag}")
            lines.append("  examples: # The set of example scenarios that the skill can perform")
            for example in skill.examples:
                lines.append(f"    - {example}")
        # Join all lines into a single string
        return "\n".join(lines)
