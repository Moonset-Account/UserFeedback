#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

首次运行若缺少依赖，会自动在项目内 .venv 引导安装并重新执行自身，
无需手动激活虚拟环境；也支持 --no-bootstrap 与已激活环境直接运行。
"""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIRED_PACKAGES = ("flask", "httpx", "jsonpointer")


def _missing_packages() -> list[str]:
    missing = []
    for dist_name in REQUIRED_PACKAGES:
        try:
            importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(dist_name)
    return missing


def _bootstrap_venv() -> Path:
    venv_dir = PROJECT_ROOT / ".venv"
    if not venv_dir.exists():
        print("[bootstrap] 正在创建项目虚拟环境 .venv ...", flush=True)
        venv.EnvBuilder(with_pip=True).create(str(venv_dir))
    py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    print("[bootstrap] 安装 requirements.txt ...", flush=True)
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet",
         "-r", str(PROJECT_ROOT / "requirements.txt")],
        check=True,
    )
    return py


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RIP/1.0 科研资料增量采集演示服务",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-bootstrap", action="store_true",
                        help="禁止自动创建 .venv，直接使用当前解释器")
    args = parser.parse_args()

    if not args.no_bootstrap and _missing_packages():
        py = _bootstrap_venv()
        os.execv(str(py), [str(py), __file__,
                           "--host", args.host, "--port", str(args.port),
                           "--no-bootstrap"])

    # 让 src 布局可直接 import，无需安装本项目
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    os.environ.setdefault("RIP_HOST", args.host)
    os.environ["RIP_PORT"] = str(args.port)

    from web.app import create_app  # noqa: E402

    app = create_app()
    print(f"[ready] 打开 http://{args.host}:{args.port}/ 即可操作", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
