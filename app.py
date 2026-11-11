#!/usr/bin/env python3
"""统一启动入口。

用法:
    python3 app.py --host 127.0.0.1 --port <端口>
    python3 app.py --test            # 先引导环境，再运行测试

首次运行会在项目内 .venv 自动创建虚拟环境并安装 requirements.txt，
随后用该虚拟环境重新执行本文件，无需手动激活虚拟环境。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
STAMP = VENV_DIR / ".requirements.stamp"
REQUIREMENTS = ROOT / "requirements.txt"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _inside_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def ensure_environment() -> None:
    """在项目虚拟环境中安装依赖并准备 Playwright 浏览器。"""
    if not VENV_DIR.exists():
        print(f"[bootstrap] 创建虚拟环境: {VENV_DIR}")
        venv.EnvBuilder(with_pip=True, clear=False).create(str(VENV_DIR))

    py = _venv_python()
    if not py.exists():
        raise SystemExit(f"[bootstrap] 虚拟环境解释器不存在: {py}")

    if not STAMP.exists() or STAMP.read_text(encoding="utf-8").strip() != _req_hash():
        print("[bootstrap] 安装依赖 (requirements.txt) ...")
        subprocess.run(
            [str(py), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
        )
        subprocess.run(
            [str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
            check=True,
        )
        # 仅安装 chromium；本机已有缓存时会直接复用，不产生下载。
        subprocess.run(
            [str(py), "-m", "playwright", "install", "chromium"],
            check=True,
        )
        STAMP.write_text(_req_hash(), encoding="utf-8")
    else:
        print("[bootstrap] 依赖已就绪，跳过安装。")


def _req_hash() -> str:
    import hashlib

    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="命令面板 RPA 实验台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--test", action="store_true", help="引导环境后运行测试")
    parser.add_argument("--pytest-args", default="", help="透传给 pytest 的参数")
    args = parser.parse_args()

    if not _inside_venv():
        ensure_environment()
        cmd = [str(_venv_python()), str(Path(__file__).resolve())]
        if args.test:
            cmd.append("--test")
        else:
            cmd += ["--host", args.host, "--port", str(args.port)]
        if args.pytest_args:
            cmd += ["--pytest-args", args.pytest_args]
        print("[bootstrap] 使用虚拟环境重新启动:", " ".join(cmd))
        return subprocess.run(cmd, cwd=str(ROOT)).returncode

    # 以下代码只在项目虚拟环境内运行。
    if args.test:
        import pytest

        pytest_args = args.pytest_args.split() if args.pytest_args else []
        return pytest.main([str(ROOT / "tests"), *pytest_args])

    from src.webapp.server import create_app, ensure_samples

    ensure_samples()
    app = create_app()
    print(f"[server] 控制台入口: http://{args.host}:{args.port}/")
    print(f"[server] 被测模拟实验台: http://{args.host}:{args.port}/lab/")
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
