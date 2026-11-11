"""采集器对外入口：抓取 + 解析 + 状态汇总。"""
from __future__ import annotations

from .fetcher import FetchError, fetch_manifest
from .models import CollectionResult, Issue
from .parser import parse_manifest


def collect(source: str) -> CollectionResult:
    """对一个 IIIF Presentation 3 清单 URL 或本地文件路径执行采集。

    任何无法解析的部分都以 Issue 形式返回；只有清单整体无法获取时 status=failed。
    """
    try:
        manifest, kind = fetch_manifest(source)
    except FetchError as exc:
        issue = Issue("FETCH_FAILED", "error", str(exc), "")
        return CollectionResult(
            manifest_id=None,
            manifest_label=None,
            manifest_type=None,
            source=source,
            source_kind="file" if "://" not in source else "http",
            status="failed",
            canvases=[],
            ranges=[],
            issues=[issue],
            stats={"canvas_count": 0, "image_count": 0, "range_count": 0, "error_count": 1, "warning_count": 0},
        )

    canvases, ranges, issues, stats = parse_manifest(manifest)
    has_error = any(i.severity == "error" for i in issues)
    status = "partial" if has_error else "ok"

    return CollectionResult(
        manifest_id=manifest.get("id"),
        manifest_label=_safe_label(manifest),
        manifest_type=manifest.get("type"),
        source=source,
        source_kind=kind,
        status=status,
        canvases=canvases,
        ranges=ranges,
        issues=issues,
        stats=stats,
        raw_manifest=manifest,
    )


def _safe_label(manifest: dict) -> str | None:
    label = manifest.get("label")
    if isinstance(label, dict):
        for values in label.values():
            if isinstance(values, list) and values:
                return str(values[0])
    elif isinstance(label, str):
        return label
    return None
