"""FuzzAgent package.

Importing the package used to initialise the whole campaign environment as a
side effect.  That made any tooling (tests, MCP servers, the hc runtime) either
wipe ``$OUTPUT`` or block on an interactive prompt.  The initialisation is now
opt-out via ``FUZZAGENT_SKIP_ENV_INIT=1`` so the legacy entrypoint keeps its
behaviour while the new runtime can import the package safely.
"""

import os

if os.environ.get("FUZZAGENT_SKIP_ENV_INIT") != "1":
    from src.utils.project import init_environment

    init_environment()
