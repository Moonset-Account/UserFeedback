#!/usr/bin/env python3
"""端到端验证入口：在临时数据目录启动真实 app，再用 Playwright 操作页面。

用法::

    python3 tests/run_e2e.py [--port 端口]

会自动使用项目 .venv（app.py 已自举依赖）；数据目录使用系统临时目录，
不污染项目内 data/。退出码 0 表示全部通过。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(PROJECT_DIR, "app.py")


def wait_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.3)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last_err}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--keep-data", action="store_true",
                        help="保留临时数据目录以便检查")
    args = parser.parse_args()

    data_dir = tempfile.mkdtemp(prefix="rpa-e2e-")
    # 通过环境变量把临时数据目录传给 app（app 默认使用项目 data/）
    env = dict(os.environ)
    env["RPA_DATA_DIR"] = data_dir
    base_url = f"http://127.0.0.1:{args.port}"

    proc = subprocess.Popen(
        [sys.executable, APP, "--host", "127.0.0.1",
         "--port", str(args.port)],
        cwd=PROJECT_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_ready(base_url + "/")
        # 延迟导入，保证未安装 Playwright 时后端测试仍可独立运行
        from playwright.sync_api import sync_playwright

        sys.path.insert(0, PROJECT_DIR)
        from tests.e2e_app import run_all

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            failures = run_all(page, base_url)
            browser.close()
        if failures:
            print(f"[e2e] {failures} 个端到端验证失败")
            return 1
        print("[e2e] 全部端到端验证通过")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        if args.keep_data:
            print(f"[e2e] 临时数据目录保留在: {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
