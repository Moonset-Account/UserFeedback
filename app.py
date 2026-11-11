#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一启动入口。

用法：
    python3 app.py --host 127.0.0.1 --port 8000

脚本会自动切换到项目自带 .venv 解释器（若存在且当前不在其中），
无需手动激活虚拟环境。
"""
from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _reexec_in_venv() -> None:
    venv_python = os.path.join(BASE_DIR, ".venv", "bin", "python")
    if os.path.exists(venv_python) and os.path.realpath(sys.executable) != os.path.realpath(venv_python):
        os.execv(venv_python, [venv_python] + sys.argv)


def main() -> None:
    _reexec_in_venv()

    parser = argparse.ArgumentParser(description="科研测量数值采集器操作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true", help="Flask 调试模式")
    args = parser.parse_args()

    # 让 collector / generator 包可导入
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)

    from web.console import create_app

    samples_dir = os.path.join(BASE_DIR, "samples")
    runs_dir = os.path.join(BASE_DIR, "runs")
    app = create_app(samples_dir=samples_dir, runs_dir=runs_dir)

    print(f" * 科研测量数值采集器已启动: http://{args.host}:{args.port}/")
    print(f" * 虚构源站页面: http://{args.host}:{args.port}/source/measurements.html")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
