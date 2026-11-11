"""测试公共工具：准备虚构样例，并通过公开采集接口（文件路径）取得结果。

注意：本测试包是唯一允许读取 generated/truth 独立真值的地方；
collector/ 与 webapp/ 源码均不引用 truth 目录。
"""

from __future__ import annotations

import json
import pathlib
import unittest

from collector.service import RunRequest, collect
from samples.generator import PRESETS, generate

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
GENERATED = PROJECT_ROOT / "generated"
SOURCE_DIR = GENERATED / "source"
TRUTH_DIR = GENERATED / "truth"


class CollectorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generate(GENERATED)  # 幂等：测试自身保证样例存在，不依赖服务器

    def collect_preset(self, preset_key: str, target_name: str | None = None) -> dict:
        preset = PRESETS[preset_key]
        request = RunRequest(
            source=str(SOURCE_DIR / preset["file"]),
            range_start=preset["range_start"],
            range_end=preset["range_end"],
            target_name=target_name,
        )
        return collect(request)

    def load_truth(self, preset_key: str) -> dict:
        return json.loads((TRUTH_DIR / f"{preset_key}.json").read_text(encoding="utf-8"))["truth"]

    def index_instances(self, result: dict) -> dict[str, dict]:
        return {inst["instance_id"]: inst for inst in result["instances"]}
