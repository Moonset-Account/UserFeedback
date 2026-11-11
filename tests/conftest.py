"""pytest 公共夹具：把 src 布局加入 sys.path，并提供独立真值模块。

注意：expected_truth 是测试专属的独立预期数据，采集器与 Web 层均不得 import 它，
采集结果只能通过公开接口（引擎 API / HTTP / NDJSON 文件）取得后再与之比对。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from rip_collector import CollectorEngine  # noqa: E402
from rip_collector.transport import run_file_collection  # noqa: E402
from sample_generator import SCENARIOS, raw_ndjson_lines, write_recording  # noqa: E402


def collect_scenario_in_memory(scenario_key: str) -> CollectorEngine:
    """走真实的逐行采集入口（ingest_line），不直接读取生成器对象的内部结构。"""
    engine = CollectorEngine()
    for line in raw_ndjson_lines(SCENARIOS[scenario_key]):
        engine.ingest_line(line)
    # 模拟文件传输在 EOF 时的结束判定
    if engine.snapshots[-1]["status"] == "streaming":
        engine.mark_interrupted("eof_without_end")
    return engine


@pytest.fixture()
def recordings_dir(tmp_path: Path) -> Path:
    from sample_generator import write_all_recordings
    write_all_recordings(tmp_path / "recordings")
    return tmp_path / "recordings"


@pytest.fixture()
def file_collect():
    def _run(scenario_key: str, base_dir: Path) -> CollectorEngine:
        path = write_recording(SCENARIOS[scenario_key], base_dir)
        engine = CollectorEngine()
        return run_file_collection(engine, path)
    return _run
