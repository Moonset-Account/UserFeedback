# -*- coding: utf-8 -*-
"""pytest 公共夹具：生成样例并提供路径；测试通过采集器公开接口读取源数据，
独立真值（samples/truth）仅在本测试中用于断言，采集器代码不依赖它。"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from generator import sample_data  # noqa: E402


@pytest.fixture(scope="session")
def samples_dir(tmp_path_factory):
    base = tmp_path_factory.mktemp("samples")
    sample_data.generate(str(base))
    return str(base)


@pytest.fixture(scope="session")
def truth(samples_dir):
    import json
    with open(os.path.join(samples_dir, "truth", "expected_index.json"),
              encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def app_client(samples_dir, tmp_path_factory):
    from web.console import create_app
    runs_dir = str(tmp_path_factory.mktemp("runs"))
    app = create_app(samples_dir=samples_dir, runs_dir=runs_dir)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(scope="session")
def server_base_url(app_client):
    """复用 Flask 测试客户端不方便给 Playwright 用，起真实本地线程服务。"""
    import threading
    from wsgiref.simple_server import make_server
    from web.console import create_app

    runs_dir = os.path.join(ROOT, "runs-test")
    os.makedirs(runs_dir, exist_ok=True)
    app = create_app(samples_dir=os.path.join(ROOT, "samples"), runs_dir=runs_dir)
    httpd = make_server("127.0.0.1", 0, app)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
