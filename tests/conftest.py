"""pytest 公共夹具：在临时目录生成样例归档。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from warc_tool.samplegen import (  # noqa: E402
    build_acceptance,
    build_demo_boundary,
    build_demo_normal,
)


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("warc_samples")
    return {
        "normal": build_demo_normal(str(out)),
        "boundary": build_demo_boundary(str(out)),
        "acceptance": build_acceptance(str(out)),
        "dir": str(out),
    }
