"""Keep test imports side-effect free.

Importing the legacy ``src`` package initialises the campaign environment
(and would wipe ``$OUTPUT`` on a machine with ``FUZZAGENT_AUTO_CLEAN=1``), so
the runtime tests opt out and provide the legacy config env it expects.
"""

from __future__ import annotations

import os

os.environ.setdefault("FUZZAGENT_SKIP_ENV_INIT", "1")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
os.environ.setdefault("OPENAI_MODEL", "test-model")
