"""Command line entry point: ``python3 -m deid_gateway``.

Example
-------
python3 -m deid_gateway \
    --dictionary examples/data/field_dictionary.json \
    --mapping /tmp/deid-map.json \
    --project hospital-a \
    --input batch.json --output batch.out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .gateway import DeidGateway
from .processor import ProcessReport
from .uids import UidMapper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deid_gateway",
        description="Project isolated DICOM JSON UID de-identification gateway.",
    )
    parser.add_argument("--input", required=True, help="Input JSON attribute tree")
    parser.add_argument("--output", help="Output JSON path (default: stdout)")
    parser.add_argument("--dictionary", help="Field dictionary JSON file")
    parser.add_argument("--mapping", help="Persistent UID mapping JSON file")
    parser.add_argument("--project", default="default", help="Receiving project id")
    parser.add_argument("--base-root", default=None, help="Generated UID prefix")
    parser.add_argument(
        "--report", action="store_true", help="Print audit report to stderr"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    dictionary = args.dictionary
    mapper = None
    if args.mapping and Path(args.mapping).exists():
        mapper = UidMapper.load(args.mapping)
    gateway = DeidGateway(
        dictionary=dictionary,
        mapper=mapper,
        mapping_path=None if mapper is not None else args.mapping,
        base_root=args.base_root,
    )
    # If we loaded a mapper, keep its configured path for persistence.
    if mapper is not None and args.mapping:
        mapper.path = Path(args.mapping)

    tree = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = ProcessReport()
    result = gateway.process(tree, args.project, report=report)

    if args.mapping:
        gateway.save_mapping(args.mapping)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")

    if args.report:
        sys.stderr.write(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
