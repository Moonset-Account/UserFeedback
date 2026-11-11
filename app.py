#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

引导逻辑：
1. 若未在项目 .venv 中运行，自动创建虚拟环境（继承系统包，离线可用）并安装 requirements.txt；
2. 在虚拟环境解释器中重新执行本文件，无需用户手动激活环境；
3. 生成样例文件、启动虚构仪器源站（端口+1）与中文 Web 采集台，打开根路径即可操作。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_DIR = PROJECT_ROOT / ".venv"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def bootstrap_venv() -> None:
    if Path(sys.prefix).resolve() == VENV_DIR.resolve():
        return  # 已在项目虚拟环境内
    if not _venv_python().exists():
        print(f"[bootstrap] 首次运行，创建虚拟环境 {VENV_DIR}（继承系统已装包，离线可用）…")
        subprocess.check_call([sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)])
    py = str(_venv_python())
    print("[bootstrap] 校验依赖（flask / requests / defusedxml）…")
    check = subprocess.run([py, "-c", "import flask, requests, defusedxml"], capture_output=True)
    if check.returncode != 0:
        subprocess.check_call([py, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    print("[bootstrap] 切换到虚拟环境解释器重新启动…")
    os.execv(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]])


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="实验仪器 SOAP/MTOM 响应采集台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    # 确保项目根在 sys.path（包导入 labsoap / generator / web）
    sys.path.insert(0, str(PROJECT_ROOT))

    from generator.sample_writer import write_all_samples
    from generator.server import serve_in_background
    from web.app import create_app

    written = write_all_samples()
    print(f"[startup] 已生成 {len(written)} 个虚构场景样例到 samples/")

    instrument_port = args.port + 1
    serve_in_background(args.host, instrument_port)
    base_url = f"http://{args.host}:{instrument_port}"
    print(f"[startup] 虚构仪器源站已启动：{base_url}（/soap?scenario=<name>）")

    app = create_app(base_url)
    print(f"[startup] 采集台页面：http://{args.host}:{args.port}/ （Ctrl+C 退出）")
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    bootstrap_venv()
    main()
