#!/usr/bin/env bash
# 统一测试入口：自动使用项目所需环境，无需手动激活虚拟环境。
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
fi

# 系统解释器缺依赖时自动建 .venv 并安装（与 app.py 的引导逻辑一致）
if ! "$PY" -c "import flask, jsonschema, bs4, playwright, pytest" 2>/dev/null; then
  echo "[run_tests] 准备虚拟环境 .venv ..."
  "$PY" -m venv .venv
  PY=".venv/bin/python"
  "$PY" -m pip install -q -r requirements.txt
  "$PY" -m playwright install chromium
fi

exec "$PY" -m pytest "$@"
