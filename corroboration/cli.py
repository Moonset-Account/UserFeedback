"""命令行入口：读取 JSON 输入，输出各指标裁决 JSON。

用法：
    python -m corroboration input.json
    python -m corroboration input.json -o verdicts.json

输入格式：
{
  "observations": [
    {"id": "obs-1", "indicator": "ind-1", "source": "sensor-a"}
  ],
  "advisories": [
    {"id": "adv-1", "indicator": "ind-1", "direction": "malicious",
     "evidence_groups": [["obs-1"], ["adv-2", "obs-3"]]}
  ]
}
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import CorroborationEngine
from .models import Advisory, Observation


def load_payload(path: str) -> tuple[list[Observation], list[Advisory]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    observations = [Observation(**item) for item in data.get("observations", [])]
    advisories = [
        Advisory(
            id=item["id"],
            indicator=item["indicator"],
            direction=item["direction"],
            evidence_groups=tuple(tuple(g) for g in item["evidence_groups"]),
        )
        for item in data.get("advisories", [])
    ]
    return observations, advisories


def evaluate_file(path: str) -> dict:
    observations, advisories = load_payload(path)
    engine = CorroborationEngine(observations, advisories)
    verdicts = engine.evaluate()
    return {"verdicts": [verdicts[ind].to_dict() for ind in sorted(verdicts)]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corroboration", description="安全通告佐证判定库")
    parser.add_argument("input", help="输入 JSON 文件路径")
    parser.add_argument("-o", "--output", help="输出 JSON 文件路径（缺省打印到标准输出）")
    args = parser.parse_args(argv)

    try:
        result = evaluate_file(args.input)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"输入无效: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
