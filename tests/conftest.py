"""pytest 公共夹具。

所有测试使用独立临时目录（data / generated），互不污染仓库默认目录。
真值文件由样例生成器写入临时 generated/truth，测试通过显式路径读取，
采集器自身的任何代码路径都不接触该目录。
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import pytest
from werkzeug.test import EnvironBuilder, run_wsgi_app

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.browser import BrowserDriver
from collector.engine import Engine
from collector.storage import Storage
from site_gen.generator import generate_all


@pytest.fixture(scope="session")
def generated_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("generated")
    generate_all(out)
    return out


@pytest.fixture(scope="session")
def browser_driver():
    driver = BrowserDriver()
    driver.start()
    yield driver
    driver.close()


@pytest.fixture()
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "data")


@pytest.fixture()
def engine(storage, browser_driver) -> Engine:
    return Engine(storage, driver=browser_driver)


class ThreadedResponse:
    def __init__(self, status: str, headers, body: bytes):
        self.status_code = int(status.split(" ", 1)[0])
        self.headers = headers
        self.content_type = headers.get("Content-Type", "")
        self._body = body

    @property
    def data(self) -> bytes:
        return self._body

    def get_data(self, as_text: bool = False):
        return self._body.decode() if as_text else self._body

    def get_json(self):
        return json.loads(self._body.decode())

    @property
    def is_json(self) -> bool:
        return "application/json" in self.content_type


class ThreadedClient:
    """在专用工作线程内同步执行 WSGI 请求。

    Playwright sync API 会在 pytest 主线程留下运行中的事件循环；
    让 Flask 视图在独立线程执行，服务端 Playwright 才能拥有干净的线程局部状态。
    """

    def __init__(self, flask_app):
        self._app = flask_app

    def request(self, method: str, path: str, json_body=None) -> ThreadedResponse:
        data = json.dumps(json_body).encode() if json_body is not None else None
        builder = EnvironBuilder(
            method=method, path=path, data=data,
            content_type="application/json" if data is not None else None,
        )
        env = builder.get_environ()
        result: dict = {}

        def worker():
            app_iter, status, headers = run_wsgi_app(self._app, env)
            body = b"".join(app_iter)
            if hasattr(app_iter, "close"):
                app_iter.close()
            result["status"] = status
            result["headers"] = headers
            result["body"] = body

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join()
        return ThreadedResponse(result["status"], result["headers"], result["body"])

    def get(self, path: str) -> ThreadedResponse:
        return self.request("GET", path)

    def post(self, path: str, json=None) -> ThreadedResponse:  # noqa: A002
        return self.request("POST", path, json_body=json)


@pytest.fixture(scope="session")
def flask_server(tmp_path_factory):
    """真实 HTTP 服务（独立线程 Werkzeug）+ 独立数据目录 + 线程化测试客户端。"""
    import os

    import werkzeug.serving

    work = tmp_path_factory.mktemp("server")
    os.environ["COLLECTOR_DATA_DIR"] = str(work / "data")
    os.environ["SAMPLE_GEN_DIR"] = str(work / "generated")

    from app import create_app

    app, engine = create_app()
    client = ThreadedClient(app)

    httpd = werkzeug.serving.make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.server_address[1]
    # 测试客户端的 request.host 不含端口，显式声明浏览器可达的基址
    os.environ["COLLECTOR_BASE_URL"] = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/health")
            if conn.getresponse().status == 200:
                break
        except OSError:
            time.sleep(0.15)

    yield {"base": base, "client": client, "work": work, "engine": engine}

    httpd.shutdown()
    engine.close()
    for key in ("COLLECTOR_DATA_DIR", "SAMPLE_GEN_DIR", "COLLECTOR_BASE_URL"):
        os.environ.pop(key, None)
