"""采集器测试：三条公开通道可用，且 truth 真值目录被硬拒绝。"""
import json

import pytest

from collector.collector import (
    CollectionError,
    collect_dom_via_bs4,
    collect_from_file,
    collect_via_http,
)
from generator.sample_generator import TRUTH_DIR


def test_file_channel_returns_spec(samples_dir):
    spec = collect_from_file(samples_dir / "normal-titration.json")
    assert spec.scenario_id == "normal-titration"
    assert len(spec.steps) == 8


def test_collector_refuses_truth_directory(truth_dir):
    truth_file = truth_dir / "normal-titration.expected.json"
    with pytest.raises(CollectionError, match="truth"):
        collect_from_file(truth_file)


def test_missing_file_raises():
    with pytest.raises(CollectionError, match="不存在"):
        collect_from_file("/tmp/definitely-not-exists-rpa-105.json")


def test_http_channel_against_local_server(live_server):
    spec = collect_via_http(live_server, "manual-swap")
    assert spec.interference is not None
    assert spec.interference["swap_a"] == "P3"


def test_http_channel_bad_scenario(live_server):
    with pytest.raises(CollectionError):
        collect_via_http(live_server, "no-such-scenario")


def test_bs4_html_subset_reads_static_order(live_server):
    order = collect_dom_via_bs4(live_server, "normal-titration")
    # 与生成器写入的公开 initial 文件一致
    assert order == ["S8", "S4", "S1", "S7", "S2", "S6", "S5", "S3"]


def test_collector_does_not_import_planner():
    """架构约束：采集模块不得导入规划/验证/执行逻辑，只能依赖协议解析。"""
    import collector.collector as mod
    forbidden = ("core.planner", "core.verifier", "rpa.executor", "rpa.runner")
    imported = set(getattr(mod, "__dict__", {}).keys())
    for name in forbidden:
        assert name not in str(imported), f"采集器不得依赖 {name}"
    # 不允许任何形式的 planner import 语句
    import inspect
    src = inspect.getsource(mod)
    assert "import planner" not in src and "from rpa" not in src
