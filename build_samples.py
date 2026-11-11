"""生成全部虚构样例 SVG 与独立真值。

用法：python build_samples.py [--out samples]
布局：
  samples/<场景>.svg        —— 公开的实验图（采集器的唯一输入）
  samples/truth/<场景>.json —— 独立真值，仅供测试断言，采集器不读取
"""

from __future__ import annotations

import argparse
import json
import os

from generator.scenarios import SCENARIOS


def build(out_dir: str) -> list[str]:
    svg_dir = out_dir
    truth_dir = os.path.join(out_dir, "truth")
    os.makedirs(truth_dir, exist_ok=True)
    written = []
    index = []
    for key, (title, func) in SCENARIOS.items():
        svg_text, truth = func()
        svg_path = os.path.join(svg_dir, f"{key}.svg")
        truth_path = os.path.join(truth_dir, f"{key}.json")
        with open(svg_path, "w", encoding="utf-8") as fh:
            fh.write(svg_text)
        with open(truth_path, "w", encoding="utf-8") as fh:
            json.dump({"scenario": key, "title": title, **truth},
                      fh, ensure_ascii=False, indent=2)
        written.append(svg_path)
        index.append({"key": key, "title": title,
                      "svg": f"{key}.svg", "truth": f"truth/{key}.json"})
    with open(os.path.join(svg_dir, "index.json"), "w",
              encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成虚构 SVG 实验图样例")
    parser.add_argument("--out", default="samples", help="输出目录")
    args = parser.parse_args(argv)
    written = build(args.out)
    print(f"已生成 {len(written)} 个样例到 {args.out}/")
    for path in written:
        print(" -", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
