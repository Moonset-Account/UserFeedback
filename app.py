#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行自动在项目内创建 .venv 并安装 requirements.txt，
随后用虚拟环境 Python 重新执行本入口，无需手动激活环境。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
MARKER = VENV_DIR / ".requirements-installed"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _bootstrap() -> None:
    if Path(sys.prefix).resolve() == VENV_DIR.resolve():
        return  # 已在虚拟环境中
    if not VENV_DIR.exists() or not _venv_python().exists():
        print(f"[app.py] 首次运行，创建虚拟环境 {VENV_DIR} ...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    if not MARKER.exists():
        print("[app.py] 安装依赖 requirements.txt ...", flush=True)
        subprocess.run(
            [str(_venv_python()), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
        )
        subprocess.run(
            [str(_venv_python()), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
            check=True,
        )
        MARKER.write_text("ok\n", encoding="utf-8")
    print("[app.py] 使用虚拟环境重新启动 ...", flush=True)
    os.execv(str(_venv_python()), [str(_venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]])


def main() -> None:
    _bootstrap()
    parser = argparse.ArgumentParser(description="IIIF 数字标本清单采集器 Web 入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true", help="开发调试模式")
    args = parser.parse_args()

    from webapp.app import create_app

    app = create_app(host=args.host, port=args.port)
    print(f"[app.py] 服务已启动：打开 http://{args.host}:{args.port}/ 即可操作", flush=True)
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
