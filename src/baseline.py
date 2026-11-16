"""Strict versioned finding-baseline loader."""

import json
import re
from pathlib import Path

from .safeio import read_regular_bounded, write_private_atomic

BASELINE_SCHEMA = "coderisktools.scanner.baseline"
BASELINE_VERSION = 1
MAX_BASELINE_BYTES = 5 * 1024 * 1024
MAX_FINGERPRINTS = 100_000
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Baseline contains duplicate key: {key}")
        result[key] = value
    return result


def load_baseline(path: str) -> set[str]:
    """Load a strict bounded v1 baseline from a regular non-symlink file."""
    baseline = Path(path)
    if not baseline.exists() and not baseline.is_symlink():
        raise FileNotFoundError(f"Baseline file not found: {path}")
    try:
        raw = read_regular_bounded(baseline, MAX_BASELINE_BYTES, "baseline")
        text = raw.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_unique_object)
    except UnicodeDecodeError as exc:
        raise ValueError("Baseline is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid baseline JSON") from exc
    except OSError as exc:
        raise RuntimeError("Cannot read baseline file") from exc

    if not isinstance(data, dict):
        raise ValueError("Baseline root must be a JSON object")
    expected = {"schema", "version", "fingerprints"}
    if set(data) != expected:
        raise ValueError("Baseline root must contain exactly: schema, version, fingerprints")
    if data["schema"] != BASELINE_SCHEMA:
        raise ValueError(f"Unsupported baseline schema: {data['schema']!r}")
    if type(data["version"]) is not int or data["version"] != BASELINE_VERSION:
        raise ValueError(f"Unsupported baseline version: {data['version']!r}")
    fingerprints = data["fingerprints"]
    if not isinstance(fingerprints, list):
        raise ValueError("Baseline fingerprints must be an array")
    if len(fingerprints) > MAX_FINGERPRINTS:
        raise ValueError(f"Baseline exceeds {MAX_FINGERPRINTS} fingerprints")
    if not all(isinstance(item, str) and _FINGERPRINT.fullmatch(item) for item in fingerprints):
        raise ValueError("Baseline fingerprints must be lowercase sha256 digests")
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("Baseline fingerprints must be unique")
    return set(fingerprints)


def write_baseline(path: str, fingerprints, overwrite: bool = False) -> None:
    """Atomically write a deterministic private baseline without matched evidence."""
    normalized = sorted(set(fingerprints))
    if len(normalized) > MAX_FINGERPRINTS:
        raise ValueError(f"Baseline exceeds {MAX_FINGERPRINTS} fingerprints")
    for fingerprint in normalized:
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("Baseline contains an invalid fingerprint")
    payload = json.dumps(
        {"schema": BASELINE_SCHEMA, "version": BASELINE_VERSION, "fingerprints": normalized},
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_BASELINE_BYTES:
        raise ValueError(f"Baseline exceeds {MAX_BASELINE_BYTES} bytes")
    write_private_atomic(path, encoded, "baseline", overwrite=overwrite)
