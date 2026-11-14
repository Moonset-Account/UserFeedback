"""Synthetic credential marker scanner — core logic.

Scans plain-text and gzip-compressed text files under a build directory
for fictional marker strings. Reports line-based positions and marker
ids only; marker values are never echoed into any report.
"""

from __future__ import annotations

import gzip
import os
import zlib
from dataclasses import dataclass, field

STATUS_SCANNED = "scanned"
STATUS_TRUNCATED = "truncated"
STATUS_SKIPPED = "skipped"
STATUS_CORRUPT = "corrupt"
STATUS_UNREADABLE = "unreadable"

GZIP_MAGIC = b"\x1f\x8b"
DEFAULT_CHUNK_SIZE = 64 * 1024
DEFAULT_MAX_BYTES = 16 * 1024 * 1024

# gzip decompression can raise BadGzipFile (an OSError), EOFError or
# zlib.error depending on how the stream is damaged.
_GZIP_ERRORS = (OSError, EOFError, zlib.error)


@dataclass
class Marker:
    marker_id: int
    value: bytes


@dataclass
class FileReport:
    path: str
    status: str = STATUS_SCANNED
    reason: str = ""
    compressed: bool = False
    bytes_scanned: int = 0
    matches: list = field(default_factory=list)  # {"line": int, "marker": int}

    def to_dict(self):
        return {
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
            "compressed": self.compressed,
            "bytes_scanned": self.bytes_scanned,
            "matches": [dict(m) for m in self.matches],
        }


def load_markers(path):
    """Load markers from a text file, one marker per line.

    The marker id is the 1-based line number inside the markers file.
    Empty lines are skipped but still consume a line number, so ids stay
    stable when the file is edited. Raises OSError if unreadable.
    """
    markers = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            value = line.rstrip("\r\n")
            if not value:
                continue
            markers.append(Marker(marker_id=lineno, value=value.encode("utf-8")))
    return markers


def scan_file(path, markers, chunk_size=DEFAULT_CHUNK_SIZE, max_bytes=DEFAULT_MAX_BYTES):
    """Scan a single file for the given markers.

    Reads the (possibly gzip-compressed) stream in chunks of ``chunk_size``
    bytes, keeping an overlap of ``max_marker_len - 1`` bytes so markers
    that straddle a read-chunk boundary are still detected. At most
    ``max_bytes`` (decompressed) bytes are scanned per file.
    """
    report = FileReport(path=path)
    if not markers:
        return report
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if max_bytes < 0:
        raise ValueError("max_bytes must be >= 0")

    try:
        raw = open(path, "rb")
    except OSError as exc:
        report.status = STATUS_UNREADABLE
        report.reason = "open failed: %s" % (exc.strerror or exc)
        return report

    stream = raw
    try:
        try:
            magic = raw.read(len(GZIP_MAGIC))
            raw.seek(0)
        except OSError as exc:
            report.status = STATUS_UNREADABLE
            report.reason = "read failed: %s" % (exc.strerror or exc)
            return report
        if magic == GZIP_MAGIC:
            stream = gzip.GzipFile(fileobj=raw)
            report.compressed = True

        max_len = max(len(m.value) for m in markers)
        overlap = max_len - 1
        window = b""
        window_line = 1    # 1-based line number of window[0]
        window_offset = 0  # absolute (decompressed) offset of window[0]
        seen = set()       # (marker_id, absolute_offset) already reported
        truncated = False
        first_chunk = True

        while True:
            try:
                chunk = stream.read(chunk_size)
            except _GZIP_ERRORS as exc:
                if report.compressed:
                    report.status = STATUS_CORRUPT
                    report.reason = "damaged gzip stream: %s" % exc
                else:
                    report.status = STATUS_UNREADABLE
                    report.reason = "read failed: %s" % exc
                return report
            if not chunk:
                break
            if first_chunk:
                first_chunk = False
                if b"\x00" in chunk:
                    report.status = STATUS_SKIPPED
                    report.reason = "binary (non-text) content"
                    return report
            remaining = max_bytes - report.bytes_scanned
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
                truncated = True
            window += chunk
            report.bytes_scanned += len(chunk)

            for marker in markers:
                start = 0
                while True:
                    idx = window.find(marker.value, start)
                    if idx < 0:
                        break
                    key = (marker.marker_id, window_offset + idx)
                    if key not in seen:
                        seen.add(key)
                        line = window_line + window[:idx].count(b"\n")
                        report.matches.append(
                            {"line": line, "marker": marker.marker_id}
                        )
                    start = idx + 1

            if overlap and len(window) > overlap:
                cut = len(window) - overlap
                consumed = window[:cut]
                window_line += consumed.count(b"\n")
                window_offset += cut
                window = window[cut:]
            elif not overlap:
                window_line += window.count(b"\n")
                window_offset += len(window)
                window = b""

            if truncated:
                break
    finally:
        try:
            stream.close()
        except Exception:
            pass
        try:
            raw.close()
        except Exception:
            pass

    if truncated and report.status == STATUS_SCANNED:
        report.status = STATUS_TRUNCATED
        report.reason = "size limit reached (%d bytes)" % max_bytes
    report.matches.sort(key=lambda m: (m["line"], m["marker"]))
    return report


def scan_directory(build_dir, markers, chunk_size=DEFAULT_CHUNK_SIZE,
                   max_bytes=DEFAULT_MAX_BYTES):
    """Scan every file under build_dir (recursively, sorted for stable output)."""
    reports = []
    for root, dirnames, filenames in os.walk(build_dir):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, build_dir)
            report = scan_file(path, markers, chunk_size=chunk_size,
                               max_bytes=max_bytes)
            report.path = rel
            reports.append(report)
    return reports


def summarize(reports):
    summary = {
        "files_total": len(reports),
        "files_scanned": 0,
        "files_truncated": 0,
        "files_skipped": 0,
        "files_corrupt": 0,
        "files_unreadable": 0,
        "files_with_matches": 0,
        "matches_total": 0,
    }
    for r in reports:
        if r.status == STATUS_SCANNED:
            summary["files_scanned"] += 1
        elif r.status == STATUS_TRUNCATED:
            summary["files_truncated"] += 1
        elif r.status == STATUS_SKIPPED:
            summary["files_skipped"] += 1
        elif r.status == STATUS_CORRUPT:
            summary["files_corrupt"] += 1
        elif r.status == STATUS_UNREADABLE:
            summary["files_unreadable"] += 1
        if r.matches:
            summary["files_with_matches"] += 1
            summary["matches_total"] += len(r.matches)
    return summary


def build_report(build_dir, markers, reports):
    """Assemble the CI-consumable JSON report (no marker values included)."""
    summary = summarize(reports)
    return {
        "tool": "synthetic-marker-scanner",
        "build_dir": os.path.abspath(build_dir),
        "marker_count": len(markers),
        "result": "FAIL" if summary["matches_total"] else "PASS",
        "summary": summary,
        "files": [r.to_dict() for r in reports],
    }
