"""pytest 公共夹具：在临时端口启动虚构仪器源站，并准备临时样例目录。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from generator.sample_writer import write_all_samples  # noqa: E402
from generator.server import build_server  # noqa: E402


@pytest.fixture(scope="session")
def instrument_server():
    httpd = build_server("127.0.0.1", 0)  # 端口 0 = 操作系统分配空闲端口
    host, port = httpd.server_address
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://{host}:{port}"
    httpd.shutdown()


@pytest.fixture(scope="session")
def samples_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("samples")
    write_all_samples(target)
    return target
