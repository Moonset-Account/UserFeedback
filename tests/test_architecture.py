"""架构边界测试：保证采集与判定不能偷看独立真值。

边界约定：
- classifier/、collector/、runner.py、webapp.py 是“判定/采集层”，
  不得 import source_site.scenarios（真值模块），不得引用真值文件/字段；
- webapp.py 允许导入 source_site.catalog（已审核不含真值的公开目录）；
- app.py 是组合根，负责装配两个子系统，允许导入源站服务；
- 真值文件名 ground_truth 只允许出现在 source_site/、tests/、app.py、文档中。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JUDGE_LAYER = ["classifier", "collector"]
JUDGE_LAYER_FILES = ["runner.py", "webapp.py"]
TRUTH_MODULE = "source_site.scenarios"
TRUTH_TOKENS = ("ground_truth", "source_truth", "expected_category")
NETWORK_TOKENS = ("requests", "urllib.request", "socket", "playwright")


def _iter_py(rel_dirs: list[str], include_layer_files: bool = True) -> list[Path]:
    out = []
    for d in rel_dirs:
        out.extend((PROJECT_ROOT / d).rglob("*.py"))
    if include_layer_files:
        for f in JUDGE_LAYER_FILES:
            p = PROJECT_ROOT / f
            if p.exists():
                out.append(p)
    return out


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_judge_layer_never_imports_truth_module():
    offenders = []
    for path in _iter_py(JUDGE_LAYER + JUDGE_LAYER_FILES):
        for mod in _imports_of(path):
            if mod == TRUTH_MODULE or mod.startswith(TRUTH_MODULE + "."):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} -> {mod}")
    assert not offenders, "判定/采集层禁止导入真值模块：" + "；".join(offenders)


def test_classifier_and_collector_never_import_any_source_site():
    """最严格边界：引擎与采集器连源站的任何模块都不能依赖。"""
    offenders = []
    for path in _iter_py(JUDGE_LAYER, include_layer_files=False):
        for mod in _imports_of(path):
            if mod == "source_site" or mod.startswith("source_site."):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} -> {mod}")
    assert not offenders, "classifier/collector 必须与源站完全解耦：" + "；".join(offenders)


def test_webapp_only_uses_public_catalog():
    """webapp 如依赖源站，只能是 catalog（公开目录），不能是 scenarios（真值）。"""
    path = PROJECT_ROOT / "webapp.py"
    for mod in _imports_of(path):
        assert not mod.startswith("source_site.scenarios"), \
            "webapp 禁止读取 scenarios 真值模块"
        if mod.startswith("source_site."):
            assert mod == "source_site.catalog", f"webapp 只允许 catalog，发现 {mod}"


def test_judge_layer_never_references_truth_tokens():
    """用 AST 字符串/名扫描，避免文档字符串误报。"""
    offenders = []
    for path in _iter_py(JUDGE_LAYER + JUDGE_LAYER_FILES):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                candidates.append(node.value)
            elif isinstance(node, ast.Name):
                candidates.append(node.id)
            elif isinstance(node, ast.Attribute):
                candidates.append(node.attr)
            for text in candidates:
                if any(tok in text for tok in TRUTH_TOKENS):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {text}")
    assert not offenders, "判定/采集层不得出现真值标识：" + "；".join(offenders)


def test_ground_truth_files_only_in_allowed_places():
    allowed = ("source_site", "tests", "app.py")
    for path in PROJECT_ROOT.rglob("*.py"):
        rel = str(path.relative_to(PROJECT_ROOT))
        if any(rel == a or rel.startswith(a + "/") or rel == a for a in allowed):
            continue
        if "ground_truth" in path.read_text(encoding="utf-8", errors="ignore"):
            pytest.fail(f"ground_truth 出现在非允许位置：{rel}")


def test_classifier_has_no_network_dependencies():
    """判定引擎保持纯函数：不出现任何网络/浏览器库导入。"""
    for path in (PROJECT_ROOT / "classifier").rglob("*.py"):
        for mod in _imports_of(path):
            root = mod.split(".")[0]
            assert root not in NETWORK_TOKENS, (
                f"{path.name} 导入了网络库 {mod}，引擎必须保持纯函数"
            )


def test_collector_never_opens_truth_paths():
    """采集器不得构造或打开 runtime 下的真值/任务目录。"""
    for path in (PROJECT_ROOT / "collector").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert "source_truth" not in node.value
                assert not node.value.endswith("ground_truth.json")
