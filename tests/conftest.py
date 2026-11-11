"""pytest 公共夹具。"""
import socket
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collector.collector import collect_from_file  # noqa: E402
from generator.sample_generator import (  # noqa: E402
    SAMPLES_DIR, TRUTH_DIR, build_samples,
)


@pytest.fixture(scope="session", autouse=True)
def _ensure_samples():
    build_samples()
    yield


@pytest.fixture(scope="session")
def samples_dir():
    return SAMPLES_DIR


@pytest.fixture(scope="session")
def truth_dir():
    return TRUTH_DIR


@pytest.fixture
def load_spec():
    def _load(scenario_id: str):
        return collect_from_file(SAMPLES_DIR / f"{scenario_id}.json")
    return _load


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """启动本地 Flask 源站（后台线程），返回 base_url。"""
    from werkzeug.serving import make_server

    from web.server import create_app

    app = create_app(headless=True)
    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    yield base_url
    server.shutdown()
