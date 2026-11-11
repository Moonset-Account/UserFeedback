"""pytest 公共夹具：在独立端口启动 Flask 测试服务（真实 HTTP + 真实浏览器）。"""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.webapp.server import create_app, ensure_samples  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    ensure_samples()
    app = create_app()
    port = _free_port()
    thread = threading.Thread(
        target=app.run,
        kwargs={"host": "127.0.0.1", "port": port, "use_reloader": False, "threaded": True},
        daemon=True,
    )
    thread.start()
    import time
    import urllib.request

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base_url + "/lab/api/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.exit("测试服务启动失败")
    yield base_url


@pytest.fixture()
def run_dir(tmp_path):
    path = tmp_path / "run"
    path.mkdir()
    return path


@pytest.fixture()
def samples_dir():
    return ROOT / "data" / "samples"


@pytest.fixture()
def truth_dir():
    return ROOT / "data" / "ground_truth"
