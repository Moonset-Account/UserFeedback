#!/usr/bin/env python3
"""统一启动入口。

用法：
    python3 app.py --host 127.0.0.1 --port <端口>

行为：
  1. 首次运行自动创建 .venv 并安装 requirements.txt（已满足则直接复用）；
  2. 调用生成器生成全部虚构场景数据与独立真值；
  3. 在后台线程启动本地公开源站（端口 = --port + 1，可用 --source-port 覆盖）；
  4. 启动采集器中文网页应用，打开根路径即可操作。
无需手动激活虚拟环境。
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
REEXEC_FLAG = "EXPIRING_ASSETS_VENV_READY"


def _venv_python() -> Path:
    sub = "python.exe" if os.name == "nt" else "bin/python"
    return VENV_DIR / sub


def _bootstrap_venv() -> None:
    """确保依赖可用；必要时创建虚拟环境并安装，失败则回退系统解释器。"""
    if os.environ.get(REEXEC_FLAG) == "1":
        return  # 已在目标解释器内

    if _venv_python().exists():
        os.environ[REEXEC_FLAG] = "1"
        os.execv(str(_venv_python()), [str(_venv_python()), *sys.argv])

    print("[bootstrap] 首次运行，创建虚拟环境 .venv …", flush=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True, cwd=str(PROJECT_ROOT),
        )
        subprocess.run(
            [str(_venv_python()), "-m", "pip", "install", "--upgrade", "pip"],
            check=True, cwd=str(PROJECT_ROOT),
        )
        subprocess.run(
            [str(_venv_python()), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
            check=True, cwd=str(PROJECT_ROOT),
        )
        print("[bootstrap] 依赖安装完成。如需浏览器自动化，请额外执行："
              ".venv/bin/python -m playwright install chromium", flush=True)
        os.environ[REEXEC_FLAG] = "1"
        os.execv(str(_venv_python()), [str(_venv_python()), *sys.argv])
    except subprocess.CalledProcessError as exc:
        print(f"[bootstrap] 虚拟环境准备失败（{exc}），回退系统 Python 解释器。", flush=True)
        _verify_system_deps()


def _verify_system_deps() -> None:
    missing = []
    for module in ("flask", "requests", "bs4", "lxml", "playwright"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        print(f"[bootstrap] 缺少依赖: {', '.join(missing)}；"
              f"请执行 python3 -m pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="公开实验导出链接采集器（本地虚构演练）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source-port", type=int, default=None,
                        help="本地源站端口，默认取 --port + 1")
    parser.add_argument("--data-dir", default=str(PROJECT_ROOT / "runtime_data"))
    parser.add_argument("--no-generate", action="store_true", help="跳过样例数据生成")
    args = parser.parse_args()

    source_port = args.source_port or args.port + 1
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. 生成虚构样例与独立真值
    if not args.no_generate:
        from generator.generate import generate
        info = generate(data_dir)
        print(f"[generator] 已生成 {info['scenarios']} 个虚构场景 -> {data_dir}", flush=True)

    # 2. 后台线程启动源站
    from werkzeug.serving import make_server
    from source_site.app import create_app as create_source_app

    source_app = create_source_app(data_dir)
    source_server = make_server(args.host, source_port, source_app, threaded=True)
    import threading
    threading.Thread(target=source_server.serve_forever, daemon=True).start()
    source_base = f"http://{args.host}:{source_port}"
    print(f"[source] 本地公开源站已启动: {source_base}", flush=True)

    # 3. 启动采集器网页应用
    from flask import Flask
    from webapp.server import bp as web_bp

    web_app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "webapp" / "templates"),
        static_folder=str(PROJECT_ROOT / "webapp" / "static"),
    )
    web_app.config["COLLECTOR_CONFIG"] = {
        "source_base_url": source_base,
        "data_dir": str(data_dir),
        "default_max_refreshes": 3,
    }
    web_app.register_blueprint(web_bp)

    print(f"[webapp] 采集器入口: http://{args.host}:{args.port}/ （Ctrl+C 退出）", flush=True)
    try:
        web_app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        source_server.shutdown()


if __name__ == "__main__":
    _bootstrap_venv()
    main()
