#!/usr/bin/env python3
"""供应商目录 XML 安全导入 — CLI 主入口。

用法:
    python3 app.py <catalog.xml> [--config import_config.json]
                   [--max-depth N] [--max-nodes N] [--max-field-length N]
                   [--chunk-size N]

输出（stdout, JSON, 键排序、UTF-8）:
    成功: {"status": "ok", "record_count": N, "records": [...]}
    失败: {"status": "error", "error": {"category": ..., "message": ...,
                                        "line": ..., "column": ...}}

退出码: 0 = 导入成功; 1 = 文档被拒绝（含分类与位置）; 2 = 用法/运行环境错误。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalog_import
from catalog_import import CatalogImportError, ImportConfig

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "samples", "import_config.json"
)


def load_config(args):
    if args.config:
        with open(args.config, "r", encoding="utf-8") as fileobj:
            data = json.load(fileobj)
        config = ImportConfig.from_dict(data)
    else:
        config = ImportConfig()
    # 命令行上限覆盖配置文件，便于本地审计复现。
    if args.max_depth is not None:
        config.max_depth = args.max_depth
    if args.max_nodes is not None:
        config.max_nodes = args.max_nodes
    if args.max_field_length is not None:
        config.max_field_length = args.max_field_length
    return config


def emit(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="供应商目录 XML 安全导入（拒绝 DTD/实体/外部引用，限制深度/节点数/字段长度）",
    )
    parser.add_argument("xml_path", help="待导入的目录 XML 文件路径")
    parser.add_argument(
        "--config",
        default=None,
        help="导入配置 JSON（allowed_namespaces/allowed_fields/max_*），"
        "缺省使用内置默认配置",
    )
    parser.add_argument("--max-depth", type=int, default=None, help="覆盖最大嵌套深度")
    parser.add_argument("--max-nodes", type=int, default=None, help="覆盖最大节点总数")
    parser.add_argument(
        "--max-field-length", type=int, default=None, help="覆盖单字段最大长度"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=catalog_import.CHUNK_SIZE,
        help="分段读取的字节数（默认 %(default)s）",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args)
    except (OSError, ValueError) as error:
        emit({"status": "error", "error": {"category": "config_error",
                                           "message": str(error),
                                           "line": None, "column": None}})
        return 2

    if not os.path.isfile(args.xml_path):
        emit({"status": "error", "error": {"category": "input_not_found",
                                           "message": "输入文件不存在: %s" % args.xml_path,
                                           "line": None, "column": None}})
        return 2

    try:
        records = catalog_import.parse_file(args.xml_path, config,
                                            chunk_size=args.chunk_size)
    except CatalogImportError as error:
        emit({"status": "error", "error": error.to_dict()})
        return 1

    emit({"status": "ok", "record_count": len(records), "records": records})
    return 0


if __name__ == "__main__":
    sys.exit(main())
