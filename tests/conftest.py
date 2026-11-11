"""pytest 共享夹具。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.loader import load_spec  # noqa: E402

SPEC_PATH = ROOT / "openapi" / "experiments.openapi.yaml"
SCENARIOS_PATH = ROOT / "fixtures" / "scenarios.json"
DATA_DIR = ROOT / "data"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def spec():
    return load_spec(SPEC_PATH)


@pytest.fixture(scope="session")
def scenarios():
    return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def experiments():
    data_file = DATA_DIR / "experiments.json"
    if not data_file.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "generator" / "generate.py"),
             "--out", str(DATA_DIR)],
            check=True, cwd=str(ROOT),
        )
    return json.loads(data_file.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def ground_truth():
    gt_file = DATA_DIR / "ground_truth.json"
    if not gt_file.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "generator" / "generate.py"),
             "--out", str(DATA_DIR)],
            check=True, cwd=str(ROOT),
        )
    return json.loads(gt_file.read_text(encoding="utf-8"))
