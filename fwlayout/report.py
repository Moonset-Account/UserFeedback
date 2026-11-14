"""Per-image report assembly."""

from .checks import check_image
from .parser import parse_image

TOOL_NAME = "fwlayout-check"


def check_one(board, path, data):
    """Check a single image (bytes); returns its report entry."""
    header = parse_image(data, board)
    if not header.determinable:
        # Header size cannot be confirmed: stop checking THIS image.
        return {
            "path": path,
            "image_size": len(data),
            "status": "skipped",
            "skip_reason": header.reason,
            "header": None,
            "findings": [],
        }
    findings = check_image(header, len(data), board)
    for n, finding in enumerate(findings, 1):
        finding["id"] = "F%03d" % n
    return {
        "path": path,
        "image_size": len(data),
        "status": "checked",
        "header": {
            "record_count": header.record_count,
            "record_size": header.record_size,
            "table_start": header.table_start,
            "header_size": header.header_size,
            "records_present": len(header.records),
            "records_missing": header.missing_records,
        },
        "findings": findings,
    }


def check_images(board, paths):
    """Check every image; one image being skipped never stops the others."""
    images = []
    for path in paths:
        with open(path, "rb") as fh:
            data = fh.read()
        images.append(check_one(board, path, data))
    summary = {
        "images_total": len(images),
        "checked": sum(1 for i in images if i["status"] == "checked"),
        "skipped": sum(1 for i in images if i["status"] == "skipped"),
        "findings_total": sum(len(i["findings"]) for i in images),
    }
    return {
        "tool": TOOL_NAME,
        "board": board.name,
        "images": images,
        "summary": summary,
    }
