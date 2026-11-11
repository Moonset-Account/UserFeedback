"""统一入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行若当前解释器缺少依赖，会自动在项目内创建 .venv、安装 requirements.txt，
并确保 Playwright Chromium 就绪，然后用该环境重启自身，无需手动激活虚拟环境。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PY = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIRED = ["flask", "jsonschema", "bs4", "playwright", "pytest"]


def _have_all_deps() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in REQUIRED)


def _bootstrap() -> None:
    """在 .venv 中准备依赖，并用该解释器重新执行本文件。"""
    if not VENV_PY.exists():
        print("[bootstrap] 创建虚拟环境 .venv ...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(PROJECT_ROOT / ".venv")])
    print("[bootstrap] 安装 requirements.txt ...", flush=True)
    subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q",
                           "-r", str(PROJECT_ROOT / "requirements.txt")])
    print("[bootstrap] 确认 Playwright Chromium ...", flush=True)
    env = dict(os.environ)
    subprocess.check_call([str(VENV_PY), "-m", "playwright", "install", "chromium"], env=env)
    os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])


def main() -> None:
    # 仅当当前解释器（可能是系统 Python）缺依赖时才创建/使用 .venv
    if not _have_all_deps():
        _bootstrap()
        return

    # 依赖就绪，正常启动（延迟导入，确保 bootstrap 后才加载第三方库）
    from web.server import create_app

    import argparse
    parser = argparse.ArgumentParser(description="RPA 实验步骤重排平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-headless", action="store_true",
                        help="让 Playwright 以有头模式运行（调试用）")
    args = parser.parse_args()

    # 首次启动自动生成样例（幂等）
    from generator.sample_generator import build_samples
    build_samples()

    app = create_app(headless=not args.no_headless)
    print(f"* 入口页面：http://{args.host}:{args.port}/")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
