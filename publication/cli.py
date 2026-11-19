"""命令行入口：从 JSON 读取问题实例并打印发布方案。

用法
====
::

    python -m publication.cli instance.json
    cat instance.json | python -m publication.cli

JSON 结构::

    {
      "table": [[1, 2], [3, 4]],
      "upper_bounds": 9,                      // 或 [[..], [..]]
      "sensitive": [[0, 0]],
      "candidates": [[0, 1], [1, 0]],
      "costs": {"0,1": 3, "1,0": 2}           // 键也可用 [r, c]
    }
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

from .publisher import plan_release


def _load_instance(path: str = "") -> Dict[str, Any]:
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return json.load(sys.stdin)


def _normalize_costs(raw: Any) -> Dict[tuple, int]:
    if not isinstance(raw, dict):
        raise ValueError("costs 必须是对象")
    out: Dict[tuple, int] = {}
    for key, val in raw.items():
        if isinstance(key, str) and "," in key:
            r_text, c_text = key.split(",", 1)
            pair = (int(r_text.strip()), int(c_text.strip()))
        else:
            pair = tuple(key) if isinstance(key, list) else key
        out[pair] = val
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print(__doc__, file=sys.stderr)
        return 2
    instance = _load_instance(argv[0] if argv else "")

    required = ("table", "upper_bounds", "sensitive", "candidates", "costs")
    missing = [k for k in required if k not in instance]
    if missing:
        raise ValueError(f"JSON 缺少字段: {missing}")

    plan = plan_release(
        table=instance["table"],
        upper_bounds=instance["upper_bounds"],
        sensitive=[tuple(p) for p in instance["sensitive"]],
        candidates=[tuple(p) for p in instance["candidates"]],
        costs=_normalize_costs(instance["costs"]),
    )
    json.dump(plan.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
