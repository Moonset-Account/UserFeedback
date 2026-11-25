"""Command line interface for local JSON audit scenarios."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .auditor import audit_document
from .errors import VolumeAuditError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m volume_audit",
        description="Compute handoff-time volume encryption coverage from local synthetic/audit JSON.",
    )
    parser.add_argument("scenario", type=Path, help="Path to a JSON document containing devices/layouts/receipts.")
    parser.add_argument("--output", "-o", type=Path, help="Write JSON report here instead of stdout.")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument("--as-of", help="Override handoff cutoff (ISO-8601 or epoch seconds).")
    parser.add_argument("--strict", action="store_true", help="Fail on validation errors.")
    return parser


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Volume handoff audit: {report['summary']['device_count']} device(s), "
        f"{report['summary']['partition_count']} active volume(s)",
        f"protected={report['summary']['protected_count']} "
        f"in_progress={report['summary']['in_progress_count']} "
        f"missing_receipt={report['summary']['missing_receipt_count']} "
        f"not_required={report['summary']['not_required_count']}",
    ]
    for device in report["devices"]:
        lines.append(f"\nDevice {device['device_id']} (handoff={device.get('handoff_at')}, layout={device.get('layout_version')})")
        for volume in device.get("volumes", []):
            lines.append(
                f"  - {volume.get('volume_name')} [{volume.get('volume_instance_id')}] "
                f"{volume['status']} range={volume.get('actual_range')} "
                f"uncovered={volume.get('uncovered_ranges')}"
            )
            if volume.get("citations"):
                lines.append(f"      evidence: {', '.join(volume['citations'])}")
        historical = device.get("historical_volumes", [])
        protected_old = [item for item in historical if item.get("was_protected_then")]
        for old in protected_old:
            lines.append(
                f"  - historical protected: layout={old['layout_version']} "
                f"{old.get('volume_name')} [{old['volume_instance_id']}] "
                f"citations={', '.join(old.get('citations', []))}"
            )
    if report.get("validation_issues"):
        lines.append(f"\nValidation issues: {len(report['validation_issues'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        document = json.loads(args.scenario.read_text(encoding="utf-8"))
        options: dict[str, Any] = {"strict": args.strict}
        if args.as_of:
            options["as_of"] = args.as_of
        report = audit_document(document, **options)
    except (OSError, json.JSONDecodeError, VolumeAuditError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "text":
        rendered = render_text(report) + "\n"
        output = rendered
    else:
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"

    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
