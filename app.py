"""统一启动入口。

用法：
    python3 app.py --host 127.0.0.1 --port <端口>

行为：
- 若项目内存在 .venv 且当前解释器不是它，自动重新以 .venv 解释器执行本文件
  （无需手动激活虚拟环境）；
- 同进程挂载虚构源站与中文控制台，按路径前缀分发，共享同一端口；
- 首次启动自动导出样例页面与独立真值到 runtime/。

路径分配：
  /                 中文控制台（操作入口）
  /api/...          控制台任务 API
  /static/...       控制台静态资源
  /scenarios/...    虚构源站样例页面（采集器使用的公开接口，与控制台同端口）
  /source/          虚构源站目录页（源站子应用，内部链接带 /source 前缀）
  /healthz          健康检查（源站）
  /__transport/...  传输故障模拟
  /__truth/...      场景公开目录（不含期望分类）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
RUNTIME_DIR = PROJECT_ROOT / "runtime"


def _ensure_venv() -> None:
    """未使用项目虚拟环境时自动切换（仅做一次，避免循环）。

    不能用 sys.executable 的 resolve() 比较：venv 的 python 通常是指向
    系统解释器的符号链接，解析后路径相同。sys.prefix 才是 venv 的可靠标志
    （venv 内 sys.prefix 指向 venv 根，系统解释器指向框架安装目录）。
    """
    if os.environ.get("APP_ENTRY_REEXEC") == "1":
        return
    if not VENV_PYTHON.exists():
        return
    if Path(sys.prefix).resolve() == PROJECT_ROOT.resolve():
        return
    os.environ["APP_ENTRY_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


def _bootstrap_runtime() -> None:
    """首次启动生成样例与真值，保证控制台开箱可用。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    from source_site.export import export_samples
    from source_site.server import write_ground_truth

    samples_dir = RUNTIME_DIR / "samples"
    if not (samples_dir / "manifest.json").exists():
        export_samples(samples_dir)
    write_ground_truth(RUNTIME_DIR)


class _PrefixDispatcher:
    """按路径前缀把请求分发到两个独立 Flask 应用。

    /source/* 的请求在转发前剥掉 "/source" 前缀，源站自身路由无需改动；
    源站首页输出的链接会在派发器里补回前缀（见 SCRIPT_NAME）。
    """

    def __init__(self, source_app, console_app):
        self.source_app = source_app
        self.console_app = console_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/source" or path.startswith("/source/"):
            environ = dict(environ)
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + "/source"
            environ["PATH_INFO"] = path[len("/source"):] or "/"
            return self.source_app.wsgi_app(environ, start_response)
        if any(path.startswith(p) for p in ("/scenarios/", "/__transport/", "/__truth/", "/healthz")):
            return self.source_app.wsgi_app(environ, start_response)
        return self.console_app.wsgi_app(environ, start_response)


def create_combined_app():
    from source_site.server import create_app as create_source_app
    from source_site.server import write_ground_truth
    from webapp import create_app as create_console_app

    RUNTIME_DIR.mkdir(exist_ok=True)
    write_ground_truth(RUNTIME_DIR)

    source_app = create_source_app(runtime_dir=RUNTIME_DIR)
    console_app = create_console_app(runtime_dir=RUNTIME_DIR)
    return _PrefixDispatcher(source_app, console_app)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="响应语义判定器 · 一体化启动")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    _ensure_venv()
    _bootstrap_runtime()

    dispatcher = create_combined_app()
    # 用一个轻量 Flask 外壳承载 WSGI 分发器，复用 werkzeug 的 run_simple
    from flask import Flask

    wrapper = Flask(__name__)
    wrapper.wsgi_app = dispatcher
    print(f"控制台入口： http://{args.host}:{args.port}/")
    print(f"虚构源站目录： http://{args.host}:{args.port}/source/")
    print(f"健康检查： http://{args.host}:{args.port}/healthz")
    wrapper.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
