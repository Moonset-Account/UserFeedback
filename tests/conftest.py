"""pytest 共享夹具。

边界约定：
  - 测试通过 HTTP 与源站交互（与采集器同一公开面）；
  - ground_truth 仅在测试进程内用于预期断言，采集器代码路径从不读取它；
  - 下载物写入独立临时目录，视为不可信数据，仅做只读校验。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from collector.http_backend import HttpBackend
from generator.generate import generate
from source_site.app import create_app as create_source_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("runtime_data")
    generate(d)
    return d


@pytest.fixture(scope="session")
def source_server(data_dir):
    app = create_source_app(data_dir)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def reset_source(source_server):
    # 每个用例前重置令牌状态，保证确定性
    import requests
    requests.post(f"{source_server}/admin/reset", timeout=5)
    yield


@pytest.fixture
def http_backend():
    backend = HttpBackend()
    yield backend
    backend.close()


@pytest.fixture
def downloads_dir(tmp_path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


@pytest.fixture
def truth(data_dir):
    def _load(scenario_id: str) -> dict:
        return json.loads((data_dir / "ground_truth" / f"{scenario_id}.json").read_text("utf-8"))
    return _load


@pytest.fixture
def page_url(source_server):
    def _build(scenario_id: str) -> str:
        return f"{source_server}/experiment/{scenario_id}"
    return _build
