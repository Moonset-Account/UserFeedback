"""Shared test fixtures: project paths (standard library only).

Importing this module also puts the project root on ``sys.path`` so that the
tests can import ``diagnostics`` and ``app`` when run from ``tests/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "samples"
SAMPLE_DATA = SAMPLES / "errors.json"
SAMPLE_ALLOWLIST = SAMPLES / "allowlist.json"
