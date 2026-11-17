"""命令行入口：python -m satoken_analyzer.cli <manifest.json>"""

from __future__ import annotations

import json
import sys

from .analyzer import AnalysisError, analyze_file


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python -m satoken_analyzer.cli <manifest.json>", file=sys.stderr)
        return 2
    try:
        result = analyze_file(argv[1])
    except (AnalysisError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
