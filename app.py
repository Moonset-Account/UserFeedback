#!/usr/bin/env python3
"""统一启动入口。

用法：
    python3 app.py --host 127.0.0.1 --port <端口>            # 启动 Web 应用（默认）
    python3 app.py web --host 127.0.0.1 --port <端口>
    python3 app.py source --host 127.0.0.1 --port <端口>     # 启动虚构源站
    python3 app.py gen-samples [目录]                        # 生成样例 WARC
    python3 app.py extract <warc 路径>                       # 命令行提取

首次以系统 python3 运行时，若项目内 .venv 不存在会自动创建并安装依赖，
随后用 .venv 解释器重新执行自身，无需手动激活虚拟环境。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
BOOTSTRAP_MARKER = "XINGLAN_VENV_BOOTSTRAPPED"


def _bootstrap_venv() -> None:
    """确保 .venv 存在且依赖可用，必要时创建并重新 exec。"""
    if os.environ.get(BOOTSTRAP_MARKER) == "1":
        return
    if not VENV_PYTHON.exists():
        print("[启动] 未发现 .venv，正在创建虚拟环境……", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(PROJECT_ROOT / ".venv")], check=True)
        print("[启动] 安装依赖（requirements.txt）……", flush=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)], check=True)
    else:
        missing = subprocess.run(
            [str(VENV_PYTHON), "-c", "import flask, warcio"],
            capture_output=True,
        )
        if missing.returncode != 0:
            print("[启动] 检测到依赖缺失，正在补装……", flush=True)
            subprocess.run(
                [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)], check=True
            )
    env = dict(os.environ)
    env[BOOTSTRAP_MARKER] = "1"
    os.execve(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve())] + sys.argv[1:], env)


def main() -> int:
    _bootstrap_venv()

    args = sys.argv[1:]
    subcommand = args[0] if args and not args[0].startswith("-") else "web"
    rest = args[1:] if subcommand != "web" else args

    import argparse

    if subcommand == "source":
        from warc_tool.source_server import main as source_main
        return source_main(rest)

    if subcommand == "gen-samples":
        from warc_tool.samplegen import build_all
        out_dir = rest[0] if rest else str(PROJECT_ROOT / "sample_outputs")
        samples = build_all(out_dir)
        for sample in samples:
            print(f"{sample.scenario:14s} {sample.path}")
        return 0

    if subcommand == "extract":
        from warc_tool.cli_extract import main as extract_main
        return extract_main(rest)

    # 默认 / web
    from warc_tool.web_app import create_app
    parser = argparse.ArgumentParser(description="WARC 研究资料字段提取 Web 应用")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parsed = parser.parse_args(rest)
    print(f"[启动] Web 应用运行于 http://{parsed.host}:{parsed.port}/ （按 Ctrl+C 停止）")
    create_app().run(host=parsed.host, port=parsed.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
