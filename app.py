#!/usr/bin/env python3
"""离线对账 CLI 主入口：对比采集端分片序号清单与归档端持久化清单。

用法：
    python3 app.py --collection samples/collection.json --archive samples/archive.json
    python3 app.py --collection c.json --archive a.json --output report.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reconcile import ManifestError, reconcile, validate_records


def load_manifest(path, source):
    """读取 JSON 清单：接受记录数组，或 {"records": [...]} 对象。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ManifestError(f"无法读取{source}清单 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{source}清单不是合法 JSON {path}: {exc}") from exc
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    return validate_records(data, source)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="采集端/归档端审计清单离线对账：输出连续缺口、摘要不一致、归档端多出记录、水位线与补传计划"
    )
    parser.add_argument("--collection", required=True, help="采集端按分片序号清单 (JSON)")
    parser.add_argument("--archive", required=True, help="归档端持久化清单 (JSON)")
    parser.add_argument("--output", help="报告输出路径，缺省写到标准输出")
    args = parser.parse_args(argv)

    try:
        collection = load_manifest(args.collection, "采集端")
        archive = load_manifest(args.archive, "归档端")
        report = reconcile(collection, archive)
    except ManifestError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2

    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
