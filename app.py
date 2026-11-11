#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行自动在项目内 .venv 创建虚拟环境并安装 requirements.txt，
随后用该环境重新执行本进程——使用者无需手动激活虚拟环境。
另提供 --test 入口运行全部测试。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
MARKER = VENV_DIR / ".requirements-installed"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _inside_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def ensure_environment() -> Path:
    python = _venv_python()
    if not python.exists():
        print("[app] 首次运行，正在创建虚拟环境 .venv ...")
        venv.EnvBuilder(with_pip=True).create(str(VENV_DIR))
    if not MARKER.exists() or MARKER.read_text().strip() != REQUIREMENTS.read_text():
        print("[app] 安装/核对依赖 requirements.txt ...")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet",
             "-r", str(REQUIREMENTS)],
            check=True,
        )
        MARKER.write_text(REQUIREMENTS.read_text(), encoding="utf-8")
    return python


def relaunch_in_venv(extra_args: list[str]) -> None:
    python = ensure_environment()
    os.execv(str(python), [str(python), str(Path(__file__)), *extra_args])


def run_tests() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [str(_venv_python()), "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(PROJECT_ROOT), env=env,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="公开天文观测窗口 iCalendar 采集器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--test", action="store_true", help="运行测试套件后退出")
    args = parser.parse_args()

    if not _inside_venv():
        relaunch_in_venv(sys.argv[1:])
        return 0  # execv 不会返回，这一行不可达

    # 以下代码运行在项目虚拟环境内
    if args.test:
        return run_tests()

    from webapp.server import create_server

    server = create_server(args.host, args.port)
    print(f"[app] 服务已启动：http://{args.host}:{args.port}/")
    print("[app] 浏览器打开根路径即可操作；Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] 已停止。")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
