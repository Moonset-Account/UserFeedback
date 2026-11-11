"""pytest 共享夹具：在后台线程启动虚构源站，并暴露独立真值。

真值来自 source_site.generator（采集器不得读取它——隔离约束另见 test_isolation.py）。
"""
from __future__ import annotations

import threading
import socket
import time

import pytest
import requests
from werkzeug.serving import make_server

from source_site.generator import generate_dataset, write_golden
from source_site.server import create_app


@pytest.fixture(scope="session")
def datasets(tmp_path_factory):
    golden_dir = tmp_path_factory.mktemp("golden")
    out = {}
    for name in ("demo", "large"):
        ds = generate_dataset(name)
        out[name] = {"dataset": ds, "golden": ds.golden(),
                     "golden_path": write_golden(ds, golden_dir)}
    return out


@pytest.fixture(scope="session")
def source_server(datasets):
    app = create_app()  # 内置两个数据集惰性生成
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if requests.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.1)
    yield base
    httpd.shutdown()


@pytest.fixture
def demo_url(source_server):
    return source_server + "/api/catalog"


@pytest.fixture
def large_url(source_server):
    return source_server + "/api/catalog?dataset=large"


@pytest.fixture
def html_url(source_server):
    return source_server + "/catalog"
