"""隔离守卫：采集器/执行器不得读取 ground_truth，也不得访问白名单之外的资源。

这是“独立真值仅供测试使用，不能直接读取真值代替采集”的强制检查。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RPA_DIR = ROOT / "src" / "rpa"
FORBIDDEN_COLLECTOR_IMPORTS = {"src.sample_data", "tests"}
# 禁止出现的真值目录路径片段（独立真值目录名，见项目 data/ 布局）。
FORBIDDEN_PATH_PARTS = ("ground" + "_truth",)


def _python_files(base: Path):
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def test_rpa_code_never_references_ground_truth():
    """以 AST 字符串字面量方式检查：禁止出现真值目录路径片段。"""
    offenders = []
    for path in _python_files(RPA_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(part in node.value for part in FORBIDDEN_PATH_PARTS):
                    offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"采集/执行代码字面量引用了真值目录: {offenders}"


def test_rpa_code_does_not_import_sample_data_or_tests():
    offenders = []
    for path in _python_files(RPA_DIR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                module = node.names[0].name
            elif isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            if module and any(module.startswith(prefix) for prefix in FORBIDDEN_COLLECTOR_IMPORTS):
                offenders.append((str(path.relative_to(ROOT)), module))
    assert not offenders, f"采集器越权导入: {offenders}"


def test_collector_uses_only_allowed_url_paths():
    collector = (RPA_DIR / "collector.py").read_text(encoding="utf-8")
    # 白名单常量必须存在且路由守卫基于它放行；公开只读接口必须在白名单内。
    assert "_ALLOWED_API_PATHS" in collector
    assert '"/lab/api/snapshot"' in collector
    assert '"/lab/api/health"' in collector
    # 白名单只允许同源 /lab/api/ 路径，不允许任何外域或其他应用接口。
    assert "http://" not in collector and "https://" not in collector
    assert '"/api/run"' not in collector  # 控制台接口不属于采集边界
    # 不允许出现直接文件读取真值的调用。
    assert "open(" not in collector or "evidence_dir" in collector
    forbidden = ["requests.get", "urllib.request", "httpx", "aiohttp"]
    for token in forbidden:
        assert token not in collector, f"采集器出现白名单外 HTTP 客户端: {token}"


def test_ground_truth_files_exist_and_are_separate(samples_dir, truth_dir):
    sample_files = {p.name for p in samples_dir.glob("*.json")}
    truth_files = {p.name for p in truth_dir.glob("*.truth.json")}
    assert len(sample_files) >= 6
    assert len(truth_files) == len(sample_files)
    # 真值与样例物理分离：不同目录、不同文件名后缀。
    assert samples_dir != truth_dir
    assert not (samples_dir / next(iter(truth_files))).exists()
