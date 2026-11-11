"""样例导出器：把虚构源站页面导出为本地 HTML 文件（文件模式采集输入）。

用法：
    python -m source_site.export --out runtime/samples

导出物：
    pages/<id>.html      语义场景页面（传输场景无 HTML，不导出）
    ground_truth.json    独立真值（仅测试使用）
    manifest.json        公开清单（路径、标题，不含期望分类）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scenarios import SCENARIOS, TRANSPORT_SCENARIOS, ground_truth_records
from .templates import render


def export_samples(out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    manifest = []
    for spec in SCENARIOS:
        html = render(spec["id"])
        target = pages_dir / f"{spec['id']}.html"
        target.write_text(html, encoding="utf-8")
        written[spec["id"]] = target
        manifest.append(
            {"scenario_id": spec["id"], "file": f"pages/{spec['id']}.html",
             "title": spec["title"], "http_status": spec["status"]}
        )

    for spec in TRANSPORT_SCENARIOS:
        manifest.append(
            {"scenario_id": spec["id"], "file": None,
             "title": spec["title"], "http_status": None,
             "note": "传输层场景，无静态 HTML 导出"}
        )

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth_records(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="导出虚构源站样例页面")
    parser.add_argument("--out", default="runtime/samples")
    args = parser.parse_args()
    written = export_samples(args.out)
    print(f"已导出 {len(written)} 个页面到 {Path(args.out) / 'pages'}")
    print(f"独立真值：{Path(args.out) / 'ground_truth.json'}（仅测试使用）")


if __name__ == "__main__":
    main()
