"""Small helpers shared by the test modules."""

import json
import os
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(REPO_ROOT, "fixtures")


def fixture_path(name):
    return os.path.join(FIXTURE_DIR, name)


def write_json(document):
    """Write ``document`` to a temporary UTF-8 file and return its path."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(document, handle, ensure_ascii=False)
    finally:
        handle.close()
    return handle.name


def record(sample_name, content=None, parse_mode="strict", environment="linux",
           attempt=1, status="passed", failure_category=None, build_id=None,
           content_hash=None, summary=None):
    entry = {
        "sample_name": sample_name,
        "parse_mode": parse_mode,
        "environment": environment,
        "attempt": attempt,
        "status": status,
    }
    if content is not None:
        entry["content"] = content
    if failure_category is not None:
        entry["failure_category"] = failure_category
    if build_id is not None:
        entry["build_id"] = build_id
    if content_hash is not None:
        entry["content_hash"] = content_hash
    if summary is not None:
        entry["summary"] = summary
    return entry


def export(build_id, results, source=None):
    document = {"build_id": build_id, "results": results}
    if source is not None:
        document["source"] = source
    return document
