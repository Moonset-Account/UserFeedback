#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行自动在项目内创建 .venv 并安装 requirements.txt，
随后用虚拟环境解释器重新执行本文件，无需手动激活环境。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
STAMP = VENV_DIR / ".requirements.stamp"
REQUIREMENTS = ROOT / "requirements.txt"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _bootstrap_venv() -> None:
    python = _venv_python()
    need_install = not STAMP.exists() or (
        REQUIREMENTS.exists()
        and REQUIREMENTS.stat().st_mtime > STAMP.stat().st_mtime
    )
    if not python.exists():
        print(f"[bootstrap] 创建虚拟环境: {VENV_DIR}")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        need_install = True
    if need_install:
        print("[bootstrap] 安装依赖 requirements.txt …")
        subprocess.check_call(
            [str(python), "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call(
            [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])
        STAMP.write_text("ok\n", encoding="utf-8")


def _in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def main() -> None:
    if not _in_venv():
        _bootstrap_venv()
        python = _venv_python()
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])

    # 以下代码只在虚拟环境解释器中执行
    from mailacq.webapp import create_app

    parser = argparse.ArgumentParser(description="实验通知只读采集器 Web 入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = create_app()
    print(f" * 打开 http://{args.host}:{args.port}/ 即可操作（Ctrl+C 退出）")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
