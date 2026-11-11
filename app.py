#!/usr/bin/env python3
"""统一启动入口。

用法::

    python3 app.py --host 127.0.0.1 --port <端口>

首次运行会在项目内自动创建 .venv 并安装运行/测试依赖（仅 Playwright，
采集器本体与 Web 服务只用 Python 标准库）。已安装依赖时秒级启动，
无需手动激活虚拟环境。打开 http://127.0.0.1:<端口>/ 即可操作。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import venv

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(PROJECT_DIR, ".venv")
STAMP_FILE = os.path.join(VENV_DIR, ".deps-installed")
# 仅端到端浏览器验证需要；核心功能零第三方依赖
REQUIREMENTS = ("playwright==1.49.1",)
DATA_DIR = os.environ.get(
    "RPA_DATA_DIR", os.path.join(PROJECT_DIR, "data"),
)


def _venv_python() -> str:
    binary = "python.exe" if os.name == "nt" else "bin/python"
    return os.path.join(VENV_DIR, binary)


def ensure_venv() -> str:
    """返回带依赖环境的解释器路径。

    优先在项目内创建 .venv（继承系统 site-packages，因此系统已安装的
    Playwright/Chromium 可直接复用）；缺失时尝试 pip 安装，安装失败
    不阻塞核心功能（采集与 Web 服务零第三方依赖）。
    """
    py = _venv_python()
    if os.path.exists(STAMP_FILE) and os.path.exists(py):
        return py
    if not os.path.exists(py):
        print("[app] 首次运行：创建虚拟环境 .venv ...", flush=True)
        try:
            venv.create(
                VENV_DIR, with_pip=True, clear=False,
                system_site_packages=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[app] 虚拟环境创建失败（{exc}），使用当前解释器继续，"
                  "浏览器端到端测试可能不可用。", flush=True)
            return sys.executable

    # 检查依赖是否已可用（含继承自系统 site-packages 的情况）
    check = subprocess.run(
        [py, "-c", "import playwright"],
        capture_output=True, text=True,
    )
    if check.returncode == 0:
        with open(STAMP_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(REQUIREMENTS) + "\n# reused system site-packages\n")
        return py

    print("[app] 安装依赖（playwright，仅端到端测试需要）...", flush=True)
    try:
        subprocess.run(
            [py, "-m", "pip", "install", *REQUIREMENTS],
            check=True, capture_output=True, text=True, timeout=300,
        )
        with open(STAMP_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(REQUIREMENTS))
        print("[app] 依赖安装完成。", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[app] 依赖安装失败（{exc}），核心功能仍可运行，"
              "仅 Playwright 浏览器测试受限。", flush=True)
    return py if os.path.exists(py) else sys.executable


def regenerate_samples() -> None:
    sys.path.insert(0, PROJECT_DIR)
    from rec.generator import generate_all

    manifest = generate_all(DATA_DIR)
    total = sum(
        item["summary"]["size"] for item in manifest["objects"].values()
    )
    print(f"[app] 已（重新）生成 {len(manifest['objects'])} 个虚构对象，"
          f"共 {total} 字节，真值清单: data/manifest.json", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="大型公开实验数据对象分段采集演示",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--regenerate", action="store_true",
        help="重新生成虚构样例对象与真值清单后退出",
    )
    args = parser.parse_args()

    if args.regenerate:
        regenerate_samples()
        return 0

    # 自动准备项目环境（无需手动 source 激活），并在需要时用该解释器重启
    env_python = ensure_venv()
    if (
        os.path.exists(env_python)
        and os.path.realpath(sys.executable) != os.path.realpath(env_python)
        and os.environ.get("APP_VENV_ACTIVE") != "1"
    ):
        os.environ["APP_VENV_ACTIVE"] = "1"
        os.execv(
            env_python,
            [env_python, os.path.abspath(__file__),
             "--host", args.host, "--port", str(args.port)],
        )

    sys.path.insert(0, PROJECT_DIR)
    from rec.web import build_app

    regenerate_samples()
    server = build_app(args.host, args.port, DATA_DIR)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print(" 实验数据对象分段采集演示已启动")
    print(f" 操作页面: {url}")
    print(f" 本地源站: {url}origin/  （采集器唯一数据入口）")
    print(" 停止服务: Ctrl+C")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] 收到中断，关闭服务。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
