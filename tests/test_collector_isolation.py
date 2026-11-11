"""采集器隔离测试：采集链路不得读取 ground_truth.json。

边界约定：
- collector 包（协议客户端、解析、提取、身份、模拟服务）禁止出现
  ground_truth / manifest / generated 字样，只认协议字节；
- runner 可以调用生成器的公开函数 load_manifest/load_generation_messages
  获知代际与邮件文件，但同样禁止读取 ground_truth.json；
- ground_truth.json 只允许 tests/ 下的断言代码读取。
"""
from pathlib import Path

from mailacq import collector as collector_pkg
from mailacq.runner import run_scenario

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = Path(collector_pkg.__file__).parent
COLLECTOR_FORBIDDEN = ("ground_truth", "manifest", "generated")


def test_collector_package_has_no_generator_coupling():
    violations = []
    for py in PKG_DIR.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in COLLECTOR_FORBIDDEN:
            if token in text:
                violations.append(
                    f"{py.relative_to(ROOT)} 含禁用标识 {token!r}")
    assert not violations, "collector 包不得耦合生成器目录：\n" + "\n".join(violations)


def test_runner_never_touches_ground_truth():
    runner_text = (ROOT / "mailacq" / "runner.py").read_text(encoding="utf-8")
    assert "ground_truth" not in runner_text


def test_run_succeeds_with_ground_truth_unreadable(monkeypatch):
    """真实跑一次采集；若链路触碰 ground_truth.json 则立即失败。"""
    import builtins

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if "ground_truth" in str(file):
            raise AssertionError(f"采集链路禁止读取真值文件: {file}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    record = run_scenario("normal_basic")
    assert record.status == "success"
    assert sum(len(g.messages) for g in record.generations) == 2
