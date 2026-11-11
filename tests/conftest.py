"""pytest 公共夹具：本地源站 + 采集器隔离检查。"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    """在独立进程启动 app.py，测试结束后关闭。"""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "app.py"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    import urllib.request

    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"测试服务器启动失败：\n{output}")
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.4)
    else:
        proc.kill()
        raise RuntimeError("测试服务器 60 秒内未就绪")

    yield {"base_url": base_url, "port": port, "proc": proc}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def manifest_url(base_url: str, scenario: str) -> str:
    return f"{base_url}/sample/manifest/{scenario}/manifest.json"
