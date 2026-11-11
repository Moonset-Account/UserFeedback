"""pytest 公共夹具。"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


CHROMIUM_OK = _chromium_available()
e2e = pytest.mark.skipif(not CHROMIUM_OK, reason="本机未安装 Playwright Chromium，跳过端到端测试")


@pytest.fixture(scope="session")
def live_server():
    """启动真实本地源站（独立进程，走 HTTP 公开接口），返回 base_url。"""
    if not CHROMIUM_OK:
        pytest.skip("Chromium 不可用")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # 输出重定向到临时文件：若用 PIPE 且无人读取，Werkzeug 访问日志会写满缓冲区导致服务器阻塞退出
    log_path = Path("/tmp") / f"curve_rpa_pytest_{port}.log"
    log_f = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    import requests

    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"本地源站提前退出，日志：\n{log_path.read_text(encoding='utf-8')[-3000:]}")
        try:
            r = requests.get(f"{base_url}/api/tasks", timeout=1)
            if r.status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.4)
    else:
        proc.terminate()
        pytest.fail(f"本地源站启动超时，日志：\n{log_path.read_text(encoding='utf-8')[-3000:]}")
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_f.close()
