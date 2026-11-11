"""pytest 夹具：样例目录、独立真值、自管的 Flask 子进程服务。

端到端测试在单个 pytest 进程内用 subprocess 启动/关闭服务，
不依赖外部已运行的服务器，也不读取采集器的内部状态。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data" / "samples"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/api/scenarios",
                                        timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.3)
    raise RuntimeError(f"测试服务未在 {timeout}s 内就绪: {last_err}")


@pytest.fixture(scope="session")
def server():
    """启动一次 Flask 服务，整个测试会话共享。"""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py"),
         "--host", "127.0.0.1", "--port", str(port),
         "--no-browser-check"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="session")
def samples_dir() -> Path:
    if not SAMPLES.exists():
        # 允许在全新检出、尚未启动过 app 时生成样例
        from rpa_grid.sample_generator import generate_all

        generate_all(SAMPLES)
    return SAMPLES


@pytest.fixture
def truth(samples_dir):
    """读取独立真值（仅测试使用；采集器无任何路径可读它）。"""
    def _load(scenario: str):
        return json.loads(
            (samples_dir / scenario / "truth.json").read_text("utf-8"))
    return _load


@pytest.fixture
def matrix_text(samples_dir):
    def _load(scenario: str) -> str:
        # 字节解码，禁用通用换行，保留 \r
        return (samples_dir / scenario / "matrix.tsv").read_bytes().decode("utf-8")
    return _load
