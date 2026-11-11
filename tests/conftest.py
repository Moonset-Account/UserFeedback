"""pytest 夹具：生成样例、启动源站、提供独立真值访问。

真值 truth.json 只在测试中使用，采集器代码路径完全不接触它。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from sampler.experiment_data import EXPERIMENT_ID  # noqa: E402
from sampler.scenarios import build_all  # noqa: E402
from source_site.server import start_source_server  # noqa: E402


@pytest.fixture(scope="session")
def sample_data_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("sample_data")
    build_all(str(out))
    return out


@pytest.fixture(scope="session")
def truth(sample_data_dir) -> dict:
    with open(sample_data_dir / "truth.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def source_server(sample_data_dir):
    server, _thread, base_url = start_source_server(str(sample_data_dir), "127.0.0.1", 0)
    # 等待端口就绪
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/index.json", timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    yield {"server": server, "base_url": base_url, "root": sample_data_dir}
    server.shutdown()


@pytest.fixture()
def base_url(source_server) -> str:
    return source_server["base_url"]


@pytest.fixture()
def experiment_id() -> str:
    return EXPERIMENT_ID


def url_for(base_url: str, package: str) -> str:
    return f"{base_url}/packages/{package}.zip"


@pytest.fixture()
def fetch_url(base_url):
    def _fetch(package: str) -> str:
        return url_for(base_url, package)

    return _fetch


@pytest.fixture()
def isolated_workdir(tmp_path):
    """给需要落地文件的测试提供独立空目录（隔离不可信数据）。"""
    work = tmp_path / "untrusted_work"
    work.mkdir()
    return work
