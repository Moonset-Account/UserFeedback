#!/usr/bin/env python3
"""样例生成器独立入口（与采集器分离）。

用法：
    python3 generate_samples.py [输出目录]

默认输出到 .runtime/sample_data。采集器永远只通过 HTTP 公开接口访问这些包，
生成的 truth.json 仅供测试断言使用。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
# 隔离模式（python -I）下 PYTHONPATH 被忽略，基于文件位置显式加入
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sampler.scenarios import build_all  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / ".runtime" / "sample_data"
    out.mkdir(parents=True, exist_ok=True)
    manifest = build_all(str(out))
    print(f"已生成 {len(manifest['packages'])} 个样例包与 {len(manifest['scenarios'])} 个场景：{out}")
    for package in manifest["packages"]:
        print(f"  - {package['name']} ({package['size']} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
