"""采集器集成测试：对真实本地源站进程走公开 HTTP 接口采集。

验证「采集器与样例生成器分离」：harvester 只经 HTTP 抓取，
不导入 source_content；测试通过页面实际 HTTP 响应断言。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from warc_tool.harvester import CaptureSpec, harvest
from warc_tool.pipeline import run_pipeline
from warc_tool.reader import read_warc_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def source_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c",
         f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r});"
         f"from warc_tool.source_server import create_app;"
         f"create_app().run(host='127.0.0.1', port={port}, use_reloader=False)"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("虚构源站启动超时")
    yield base
    proc.terminate()
    proc.wait(timeout=5)


def test_harvest_writes_request_response_pair(source_server, tmp_path):
    out = tmp_path / "harvested.warc.gz"
    specs = [
        CaptureSpec(source_server + "/briefing/001",
                    "2025-01-15T02:00:00Z", sim_date="2025-01-15"),
    ]
    report = harvest(specs, str(out))
    assert report["count"] == 1
    records, errors = read_warc_file(str(out))
    assert errors == []
    types = [r.rec_type for r in records]
    assert "request" in types and "response" in types
    request = next(r for r in records if r.rec_type == "request")
    response = next(r for r in records if r.rec_type == "response")
    # request 通过 WARC-Concurrent-To 关联 response
    assert request.header("WARC-Concurrent-To").strip("<>") == response.record_id
    assert request.header("X-Sim-Date") is None  # 请求头在 HTTP 头而非 WARC 头
    assert request.http_header("X-sim-date") == "2025-01-15"


def test_same_url_two_versions_via_public_header(source_server, tmp_path):
    out = tmp_path / "versions.warc.gz"
    url = source_server + "/briefing/001"
    specs = [
        CaptureSpec(url, "2025-01-15T02:00:00Z", sim_date="2025-01-15"),
        CaptureSpec(url, "2025-04-20T06:30:00Z", sim_date="2025-04-20"),
    ]
    harvest(specs, str(out))
    result = run_pipeline(str(out))
    responses = [e for e in result["entries"] if e["rec_type"] == "response"]
    assert len(responses) == 2
    titles = [e["fields"]["title"]["value"] for e in responses]
    assert "观测简报第 001 期：冬季近海水温初报" in titles
    assert "观测简报第 001 期：冬季近海水温观测（修订版）" in titles
    # 同 URL，不同记录标识与不同正文指纹
    assert responses[0]["target_uri"] == responses[1]["target_uri"]
    assert responses[0]["record_id"] != responses[1]["record_id"]
    assert responses[0]["payload_sha256"] != responses[1]["payload_sha256"]


def test_harvester_does_not_import_source_content():
    """结构约束：采集器源码不得导入源站内容/样例生成模块。"""
    src = (PROJECT_ROOT / "warc_tool" / "harvester.py").read_text(encoding="utf-8")
    code_lines = [
        line for line in src.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "import source_content" not in code
    assert "from warc_tool.source_content" not in code
    assert "import samplegen" not in code
    assert "from warc_tool.samplegen" not in code
