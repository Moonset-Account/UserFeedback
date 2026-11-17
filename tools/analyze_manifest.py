#!/usr/bin/env python3
"""清单分析 CLI（独立入口，适配 python -I 隔离模式）。

用法：
    python -I tools/analyze_manifest.py samples/c01_automount_override_and_projected.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from satoken_analyzer import AnalysisError  # noqa: E402
from satoken_analyzer.analyzer import analyze_file  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "用法: python -I tools/analyze_manifest.py <manifest.json>",
            file=sys.stderr,
        )
        return 2
    try:
        result = analyze_file(argv[1])
    except (AnalysisError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
