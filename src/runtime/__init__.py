"""Runtime layer that drives agents through herdr-core (hc).

The modules here are deliberately free of the legacy ``BaseAgent`` loop: they
describe *what* to run (config), *where* the campaign state lives (artifacts),
*which* tools an agent may call (registry/executor) and *when* a task may end
(guards).  The MCP server in :mod:`src.runtime.mcp` is the only process-side
glue an external coding-agent CLI needs.
"""

__all__ = ["config", "artifacts", "registry", "executor", "guards"]
