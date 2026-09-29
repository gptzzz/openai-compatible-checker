#!/usr/bin/env python3
"""Deprecated name of openai_compatible_checker.py, kept for one release cycle.

ai-api-relay-checker was renamed to openai-compatible-checker in 2.0.0. This
file forwards to the new module so existing scripts and CI jobs keep working.
It will be removed in 3.0.0. Reports now use schema_version 2.0; see
CHANGELOG.md for the migration notes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from openai_compatible_checker import main
except ImportError:  # the single legacy file was downloaded on its own
    sys.stderr.write(
        "ai_api_relay_checker.py is now a thin wrapper. Download openai_compatible_checker.py "
        "into the same folder, or run: pipx run openai-compatible-checker --help\n"
    )
    raise SystemExit(2) from None

if __name__ == "__main__":
    sys.stderr.write(
        "note: ai_api_relay_checker.py was renamed to openai_compatible_checker.py "
        "(command: oacheck); this alias will be removed in 3.0.0\n"
    )
    raise SystemExit(main())
