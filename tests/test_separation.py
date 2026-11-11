"""架构隔离测试：采集器与样例生成器分离，采集侧不得读取真值/生成器内部。"""
from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_DIR = PROJECT_ROOT / "labsoap"
TESTS_DIR = PROJECT_ROOT / "tests"


def _python_files(root: Path):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_collector_never_imports_generator_or_expected_truth():
    for path in _python_files(COLLECTOR_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("generator"), f"{path.name} 不得导入 generator（采集器/生成器分离）"
                assert not name.startswith("tests"), f"{path.name} 不得读取测试真值"


def test_collector_never_reads_samples_as_truth():
    # 采集器只能把 samples 当作“待采集的原始响应”，不得出现读真值/expected 的路径
    for path in _python_files(COLLECTOR_DIR):
        text = path.read_text(encoding="utf-8")
        assert "expected_values" not in text
        assert "tests/expected" not in text


def test_expected_truth_is_independent_literals():
    # 真值文件不得导入生成器或采集器，保证是独立字面量
    path = TESTS_DIR / "expected" / "expected_values.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None) or (node.names[0].name if isinstance(node, ast.Import) else "")
            assert not mod.startswith(("generator", "labsoap")), "真值必须独立维护"
