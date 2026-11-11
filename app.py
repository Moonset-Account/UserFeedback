#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

自举逻辑：
1. 若未在项目虚拟环境中运行，自动创建 .venv 并在其中重装依赖后 re-exec；
2. 已在 venv 中时直接启动 Flask，首次使用 Playwright 会提示/自动安装 Chromium；
3. 样例数据不存在时由应用自动生成（全部虚构）。
"""
from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _in_project_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def _bootstrap_venv() -> None:
    py = _venv_python()
    if not py.exists():
        print(f"[bootstrap] 创建虚拟环境: {VENV_DIR}")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    print("[bootstrap] 安装/校验依赖（Flask、Playwright）…")
    subprocess.check_call(
        [str(py), "-m", "pip", "install", "--quiet",
         "-r", str(REQUIREMENTS)])
    os.execv(str(py), [str(py), *sys.argv])


def _ensure_playwright_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
                browser.close()
                return
            except Exception:  # noqa: BLE001
                print("[bootstrap] 安装 Playwright Chromium（仅首次，体积较大）…")
                subprocess.check_call(
                    [sys.executable, "-m", "playwright", "install", "chromium"])
    except ImportError:
        print("[bootstrap] 未安装 playwright，请先确认 requirements 安装完成。")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="类型化实验矩阵 RPA 录入系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser-check", action="store_true",
                        help="跳过 Chromium 安装检查（测试时使用）")
    args = parser.parse_args()

    if not _in_project_venv():
        _bootstrap_venv()  # 内部会 re-exec，不会返回

    # 确保依赖可导入
    for mod in ("flask", "playwright"):
        try:
            importlib.import_module(mod)
        except ImportError:
            print(f"[bootstrap] 缺少依赖 {mod}，重新安装 requirements …")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet",
                 "-r", str(REQUIREMENTS)])

    if not args.no_browser_check:
        _ensure_playwright_browser()

    from rpa_grid.webapp import create_app

    app = create_app()
    print(f"[ready] 打开 http://{args.host}:{args.port}/ 即可操作")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
