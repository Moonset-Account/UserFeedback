#!/usr/bin/env python3
"""统一入口：自动准备项目虚拟环境并启动 Web 应用。

用法：python3 app.py --host 127.0.0.1 --port <端口>

无需手动激活虚拟环境：本脚本会在项目根目录创建 .venv、安装 requirements.txt
并确保 Playwright Chromium 就绪，然后在 venv 内重新执行自身。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
MARKER = VENV / ".bootstrap_ok"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _inside_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _system_deps_satisfied() -> bool:
    """venv 不可用时的兜底：检查系统解释器是否已具备依赖。"""
    try:
        import flask  # noqa: F401
        import playwright  # noqa: F401
        import requests  # noqa: F401
    except ImportError:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


def _run(cmd: list[str]) -> None:
    print(f"[bootstrap] {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def ensure_environment() -> None:
    if _inside_venv():
        return
    if VENV.exists() and _venv_python().exists() and MARKER.exists():
        os.execv(str(_venv_python()), [str(_venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]])
    try:
        if not VENV.exists():
            print("[bootstrap] 创建虚拟环境 .venv ...", flush=True)
            venv_builder = __import__("venv").EnvBuilder(with_pip=True)
            venv_builder.create(str(VENV))
        py = str(_venv_python())
        _run([py, "-m", "pip", "install", "--upgrade", "pip"])
        _run([py, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
        _run([py, "-m", "playwright", "install", "chromium"])
        MARKER.touch()
        os.execv(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]])
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"[bootstrap] 虚拟环境准备失败：{exc}", flush=True)
        if _system_deps_satisfied():
            print("[bootstrap] 系统 Python 已具备所需依赖，回退到系统解释器继续运行。", flush=True)
            return
        print(
            "[bootstrap] 无法自动安装依赖。请手动执行：\n"
            f"  {shutil.which('python3')} -m venv .venv\n"
            "  .venv/bin/pip install -r requirements.txt\n"
            "  .venv/bin/python -m playwright install chromium",
            flush=True,
        )
        raise SystemExit(1)


def main() -> None:
    ensure_environment()
    sys.path.insert(0, str(ROOT / "src"))
    from curve_rpa.web import parse_args, serve

    args = parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
