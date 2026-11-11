#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行会在项目内自动创建 .venv 并安装 requirements.txt；
之后自动使用该环境重新执行本脚本，无需手动激活虚拟环境。
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(PROJECT_ROOT, ".venv")
REQUIREMENTS = os.path.join(PROJECT_ROOT, "requirements.txt")
STAMP = os.path.join(VENV_DIR, ".requirements-installed")


def _venv_python() -> str:
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    return os.path.join(VENV_DIR, bin_dir, "python")


def _in_venv() -> bool:
    return os.path.normcase(sys.prefix) == os.path.normcase(VENV_DIR)


def _bootstrap() -> None:
    if not os.path.exists(_venv_python()):
        print(f"[app.py] 首次运行，创建虚拟环境 {VENV_DIR} ...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    if not os.path.exists(STAMP) or \
            os.path.getmtime(REQUIREMENTS) > os.path.getmtime(STAMP):
        print("[app.py] 安装/更新依赖 requirements.txt ...")
        py = _venv_python()
        subprocess.check_call(
            [py, "-m", "pip", "install", "--upgrade", "pip"],
        )
        subprocess.check_call(
            [py, "-m", "pip", "install", "-r", REQUIREMENTS],
        )
        open(STAMP, "w", encoding="utf-8").close()


def main() -> int:
    if not _in_venv():
        _bootstrap()
        # 用项目虚拟环境重新执行自身，透传全部参数
        os.execv(_venv_python(), [_venv_python(), __file__, *sys.argv[1:]])

    # 以下仅在项目虚拟环境内执行
    sys.path.insert(0, PROJECT_ROOT)
    from webapp.server import main as server_main

    return server_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
