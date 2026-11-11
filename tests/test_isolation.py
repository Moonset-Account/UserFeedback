"""架构隔离测试：采集器不得依赖样例生成器或测试真值。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_collector_does_not_import_generator_or_truth():
    collector_files = list((PROJECT_ROOT / "iiif_collector").glob("*.py"))
    assert collector_files
    for path in collector_files:
        text = path.read_text(encoding="utf-8")
        assert "sample_site" not in text, f"采集器不得依赖样例生成器：{path.name}"
        assert "expected_truth" not in text, f"采集器不得读取测试真值：{path.name}"
        assert "tests" not in text


def test_collector_runs_without_project_on_syspath(server, tmp_path):
    """在隔离目录、python -I 模式下仅用标准库经公开 HTTP 接口读取清单。"""
    script = "\n".join([
        "import json, sys, urllib.request",
        "with urllib.request.urlopen(sys.argv[1], timeout=10) as r:",
        "    data = json.load(r)",
        "assert data['type'] == 'Manifest'",
        "print('public-interface-ok', len(data['items']))",
    ])
    result = subprocess.run(
        [sys.executable, "-I", "-c", script,
         f"{server['base_url']}/sample/manifest/demo-normal/manifest.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "public-interface-ok 4" in result.stdout


def test_truth_module_not_imported_by_app():
    for pkg in ("webapp",):
        for path in (PROJECT_ROOT / pkg).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "expected_truth" not in text
