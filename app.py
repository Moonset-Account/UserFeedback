#!/usr/bin/env python3
"""统一启动入口。

用法：
    python3 app.py --host 127.0.0.1 --port <端口>

启动动作：
1. 检查 Python 版本（要求 3.11+）；运行期零第三方依赖，无需手动激活虚拟环境；
2. 生成全部虚构样例数据包到 .runtime/sample_data（已存在则复用）；
3. 启动本地公开源站（随机端口）与 Web 应用（指定端口）；
4. 打开根路径即可在中文网页上操作。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
# 隔离模式（python -I）下 PYTHONPATH 会被忽略，必须基于文件位置显式加入
sys.path.insert(0, str(SRC_DIR))
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
DATA_DIR = RUNTIME_DIR / "sample_data"


def check_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit(f"需要 Python 3.11+，当前为 {sys.version.split()[0]}")


def generate_samples(force: bool = False) -> None:
    if DATA_DIR.exists() and not force and (DATA_DIR / "index.json").exists():
        print(f"[app] 复用已生成样例：{DATA_DIR}", flush=True)
        return
    print("[app] 生成虚构实验样例数据包 ...", flush=True)
    RUNTIME_DIR.mkdir(exist_ok=True)
    sys.path.insert(0, str(SRC_DIR))
    from sampler.scenarios import build_all

    build_all(str(DATA_DIR))
    print(f"[app] 样例生成完成：{DATA_DIR}", flush=True)


def start_services(host: str, port: int) -> None:
    from source_site.server import start_source_server
    from web.app import create_web_server

    source_server, _source_thread, source_base_url = start_source_server(str(DATA_DIR), host, 0)
    web_server = create_web_server(host, port, source_base_url, str(DATA_DIR))
    actual_host, actual_port = web_server.server_address
    print("=" * 72, flush=True)
    print("  公开实验 ZIP 选择性采集系统已启动", flush=True)
    print(f"  操作页面（根路径） : http://{actual_host}:{actual_port}/", flush=True)
    print(f"  本地公开源站       : {source_base_url}/index.json", flush=True)
    print(f"  样例数据目录       : {DATA_DIR}", flush=True)
    print("  按 Ctrl+C 停止服务", flush=True)
    print("=" * 72, flush=True)
    try:
        web_server.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] 正在停止 ...", flush=True)
    finally:
        web_server.shutdown()
        source_server.shutdown()


def wait_and_open(host: str, port: int) -> None:
    url = f"http://{host}:{port}/"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    break
        except OSError:
            time.sleep(0.2)
    if os.environ.get("NO_BROWSER") != "1":
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="公开实验 ZIP 选择性采集系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--regenerate", action="store_true", help="重新生成全部样例数据")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    check_python()
    if args.no_browser:
        os.environ["NO_BROWSER"] = "1"
    generate_samples(force=args.regenerate)
    threading.Thread(target=wait_and_open, args=(args.host, args.port), daemon=True).start()
    start_services(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
