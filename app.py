#!/usr/bin/env python3
"""统一启动入口：python3 app.py --host 127.0.0.1 --port <端口>

行为：
1. 若未运行在项目 .venv 中，自动创建虚拟环境并安装 requirements.txt（仅首次），
   然后用该解释器重新执行本文件——使用者无需手动激活虚拟环境；
2. 在后台守护线程启动本地虚构标本源站（独立端口）；
3. 启动中文 Web 操作入口，打开根路径即可使用。
设置环境变量 SKIP_VENV_BOOTSTRAP=1 可跳过自举（例如 CI 已自行配置环境时）。
"""
from __future__ import annotations

import os
import sys
import venv
import subprocess
import threading
import time
import socket
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"


def _in_project_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def _bootstrap_venv() -> None:
    if os.environ.get("SKIP_VENV_BOOTSTRAP") == "1":
        return
    if _in_project_venv():
        return
    if not VENV_DIR.exists():
        print(f"[bootstrap] 首次运行：创建虚拟环境 {VENV_DIR} ...", flush=True)
        venv.create(VENV_DIR, with_pip=True)
    py = VENV_DIR / "bin" / "python"
    if not py.exists():  # pragma: no cover - 环境异常时的兜底
        print("[bootstrap] 虚拟环境解释器缺失，改用当前解释器继续。", flush=True)
        return
    print("[bootstrap] 安装/校验依赖 requirements.txt ...", flush=True)
    try:
        subprocess.run(
            [str(py), "-m", "pip", "install", "--disable-pip-version-check",
             "-q", "-r", str(REQUIREMENTS)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[bootstrap] 依赖安装失败（{exc}）。若网络不可用且系统解释器已具备依赖，"
              "可设置 SKIP_VENV_BOOTSTRAP=1 后重试。", flush=True)
        raise SystemExit(2)
    print("[bootstrap] 重新以项目虚拟环境解释器启动 ...", flush=True)
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


def _wait_source_ready(url: str, timeout: float = 15.0) -> bool:
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=1.0).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.2)
    return False


def _start_source_server(port: int) -> None:
    from source_site.server import create_app

    server_app = create_app()

    def serve() -> None:
        # 独立 WSGI server 线程；daemon 随主进程退出
        from werkzeug.serving import make_server

        httpd = make_server("127.0.0.1", port, server_app, threaded=True)
        httpd.serve_forever()

    threading.Thread(target=serve, name="source-site", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="有界蓄水池抽样采集演示（虚构数据，本地运行）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--source-port", type=int, default=8766,
                        help="本地虚构源站端口；被占用时自动选择空闲端口")
    parser.add_argument("--no-source", action="store_true",
                        help="不启动内置源站（改用外部已运行的源站时配合 SOURCE_BASE_URL）")
    args = parser.parse_args()

    source_base_url = os.environ.get("SOURCE_BASE_URL")
    if not args.no_source and not source_base_url:
        # 显式端口 0 或首选端口被占用时，自动选择空闲端口
        source_port = _free_port(args.source_port) if args.source_port else _free_port(0)
        _start_source_server(source_port)
        source_base_url = f"http://127.0.0.1:{source_port}"
        if not _wait_source_ready(f"{source_base_url}/healthz"):
            print("[startup] 本地源站健康检查超时，请查看错误输出。", flush=True)
            raise SystemExit(3)
        print(f"[startup] 虚构源站已启动：{source_base_url}", flush=True)
    elif source_base_url:
        print(f"[startup] 使用外部源站：{source_base_url}", flush=True)

    from webapp.app import create_app

    web_app = create_app(source_base_url)
    print(f"[startup] Web 入口：http://{args.host}:{args.port}/ （Ctrl+C 退出）", flush=True)
    web_app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    _bootstrap_venv()
    main()
