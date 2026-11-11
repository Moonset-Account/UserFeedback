"""pytest 公共夹具。

真值使用规则（重要）：
- tests/ 是唯一允许读取 source_site.scenarios / ground_truth 的位置；
- 测试断言以“独立预期表”EXPECTED 为准（手写在本文件中），
  ground_truth 只用来交叉核对预期表本身，不喂给被测代码。
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from source_site.export import export_samples  # noqa: E402
from source_site.server import create_app  # noqa: E402
from source_site.server import write_ground_truth  # noqa: E402

# ---------------------------------------------------------------------------
# 独立预期表：人工声明的分类预期，不引用被测引擎的任何常量做循环论证
# （类别字符串与真值文件中的枚举同名，属于协议约定而非代码复用）
# ---------------------------------------------------------------------------
EXPECTED = {
    "normal": "VALID",
    "normal-keyword": "VALID",
    "empty-zh": "EMPTY",
    "empty-en": "EMPTY",
    "empty-ja": "EMPTY",
    "maintenance-zh": "MAINTENANCE",
    "maintenance-en": "MAINTENANCE",
    "maintenance-ja": "MAINTENANCE",
    "access-login-zh": "ACCESS_NOTICE",
    "access-permission-zh": "ACCESS_NOTICE",
    "access-captcha-zh": "ACCESS_NOTICE",
    "access-en": "ACCESS_NOTICE",
    "access-ja": "ACCESS_NOTICE",
    "error-zh": "ERROR_PAGE",
    "error-en": "ERROR_PAGE",
    "broken-truncated": "BROKEN_TEMPLATE",
    "broken-skeleton": "BROKEN_TEMPLATE",
    "broken-unclosed": "BROKEN_TEMPLATE",
    "ambiguous": "UNKNOWN",
    "uncertain-busy": "UNKNOWN",
    "maintenance-500": "MAINTENANCE",
}
TRANSPORT_EXPECTED = {"tcp-reset": "FETCH_FAILED"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def runtime_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("runtime")
    export_samples(d / "samples")
    write_ground_truth(d)
    return d


@pytest.fixture(scope="session")
def samples_dir(runtime_dir) -> Path:
    return runtime_dir / "samples" / "pages"


@pytest.fixture(scope="session")
def source_server(runtime_dir):
    """启动独立进程的虚构源站（werkzeug 线程服务器）。"""
    port = _free_port()
    app = create_app(runtime_dir=runtime_dir)

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app, threaded=True)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    # 等待就绪
    for _ in range(50):
        try:
            if requests.get(base + "/healthz", timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.1)
    yield base
    server.shutdown()


@pytest.fixture()
def job_store(runtime_dir):
    from runner import JobStore

    return JobStore(runtime_dir / "jobs-root")
