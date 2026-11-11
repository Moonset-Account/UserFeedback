#!/usr/bin/env python3
"""统一启动入口。

用法：
    python3 app.py --host 127.0.0.1 --port 8765

首次启动会在项目目录下自动创建 .venv 虚拟环境并安装 requirements.txt，
随后用该环境重新执行自身，无需手动激活虚拟环境。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
READY_FLAG = "FEEDCOLLECTOR_VENV_READY"
MARKER = "feedcollector_bootstrap"


def _venv_python() -> Path:
    if os.name == "nt":  # pragma: no cover - 本项目目标平台为 macOS/Linux
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _ensure_venv() -> Path:
    python = _venv_python()
    if python.exists():
        return python
    print(f"[bootstrap] 正在创建虚拟环境：{VENV_DIR}")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    print("[bootstrap] 安装运行时依赖（requirements.txt）...")
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL,
    )
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "-r",
         str(PROJECT_ROOT / "requirements.txt")]
    )
    return python


def main() -> int:
    parser = argparse.ArgumentParser(description="研究讲座音频目录采集器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    # 已在项目虚拟环境中（或显式跳过引导）则直接启动服务
    if os.environ.get(READY_FLAG) == MARKER or sys.prefix.startswith(str(VENV_DIR)):
        from feedcollector.webapp import serve

        return serve(host=args.host, port=args.port)

    python = _ensure_venv()
    env = dict(os.environ)
    env[READY_FLAG] = MARKER
    # 让重新执行的进程能够 import 到项目包
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    print(f"[bootstrap] 使用虚拟环境启动服务：http://{args.host}:{args.port}")
    os.execve(str(python), [str(python), str(Path(__file__).resolve()),
                            "--host", args.host, "--port", str(args.port)], env)
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
