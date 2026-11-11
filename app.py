#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行若项目内 .venv 不存在，会自动创建并安装 requirements.txt，
随后用该解释器重新执行本脚本，无需手动激活虚拟环境。
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


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _bootstrap_venv() -> None:
    python = _venv_python()
    requirements = ROOT / "requirements.txt"
    need_install = (
        not python.exists()
        or not STAMP.exists()
        or STAMP.read_text(encoding="utf-8").strip()
        != requirements.read_text(encoding="utf-8").strip()
    )
    if not python.exists():
        print(f"[app] 首次运行：创建虚拟环境 {VENV_DIR} ...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    if need_install:
        print("[app] 安装运行依赖 requirements.txt ...", flush=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-q",
             "-r", str(requirements)],
            check=True,
        )
        STAMP.write_text(requirements.read_text(encoding="utf-8"), encoding="utf-8")
    if Path(sys.executable).resolve() != python.resolve():
        print("[app] 切换到项目虚拟环境解释器重新启动 ...", flush=True)
        os.execv(str(python), [str(python), str(ROOT / "app.py"), *sys.argv[1:]])


def main() -> int:
    parser = argparse.ArgumentParser(description="参数序列化采集练习（本地源站 + Web 入口）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-venv", action="store_true",
                        help="跳过自动虚拟环境（CI 中已自行准备环境时使用）")
    args = parser.parse_args()

    if not args.no_venv and os.environ.get("APP_SKIP_VENV") != "1":
        _bootstrap_venv()

    # 延迟导入：确保在 venv 解释器内执行
    sys.path.insert(0, str(ROOT))
    from server.app import create_app

    app = create_app({"BASE_URL": f"http://{args.host}:{args.port}"})
    print(f"[app] 服务已启动：打开 http://{args.host}:{args.port}/ 即可操作", flush=True)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
