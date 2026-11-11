"""架构隔离约束（验收项）：

1. collector（采集器）不得导入 source_site（源站/生成器）或读取 data/golden 真值；
2. 采集器只能经公开接口、文件或浏览器交互取得数据。
用静态扫描源码的方式强制，防止后续改动破坏分离原则。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "collector"
FORBIDDEN_TOKENS = ("source_site", "golden", "generator")


def test_collector_has_no_privileged_imports_or_paths():
    offenders = []
    for py in COLLECTOR.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{py.relative_to(ROOT)} 含禁用标识 {token!r}")
    assert not offenders, "采集器不得依赖源站生成器或真值文件：\n" + "\n".join(offenders)


def test_collector_only_uses_allowed_data_channels():
    # 采集器的数据入口只允许：requests（HTTP）、playwright（浏览器）、bs4（HTML 解析）
    # 以及标准库和包内相对导入（from .xxx）
    allowed_roots = ("requests", "playwright", "bs4")
    offenders = []
    for py in COLLECTOR.rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if stripped.startswith("from ."):  # 包内相对导入
                continue
            import sys
            root_mod = stripped.split()[1].split(".")[0]
            if root_mod in sys.stdlib_module_names or root_mod == "__future__":
                continue
            if root_mod not in allowed_roots:
                offenders.append(f"{py.name}: {stripped}")
    assert not offenders, "采集器出现未允许的第三方/内部依赖：\n" + "\n".join(offenders)


def test_golden_files_are_not_served_by_source_routes():
    server_src = (ROOT / "source_site" / "server.py").read_text(encoding="utf-8")
    assert "golden" not in server_src, "源站公开路由不得暴露独立真值"
