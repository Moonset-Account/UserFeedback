"""Post-rotation handshake anomaly analysis (Python 3 standard library only).

Reads a gateway handshake JSONL export, aggregates successful handshakes into
UTC-aligned ten-minute windows per service, and raises an alert when the
share of unexpected public-key fingerprints reaches the policy threshold.

Judgment is always made against the fingerprint validity windows that apply
at the moment the handshake happened, so old and new fingerprints may both be
valid during a rotation overlap.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from fractions import Fraction

WINDOW_SECONDS = 600  # UTC-aligned 10-minute aggregation windows
REQUIRED_FIELDS = ("ts", "service", "conn_id", "result", "fingerprint")


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def parse_ts(value):
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string, got %s" % type(value).__name__)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("unparseable timestamp: %r" % (value,)) from exc
    if moment.tzinfo is None:
        raise ValueError("timestamp lacks a UTC offset: %r" % (value,))
    return moment.astimezone(timezone.utc)


def format_ts(moment):
    """Format a datetime as a canonical UTC ISO-8601 string."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_start(moment):
    """Return the start of the UTC-aligned 10-minute window containing moment."""
    epoch = int(moment.timestamp())
    aligned = epoch - (epoch % WINDOW_SECONDS)
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


# ---------------------------------------------------------------------------
# policy model
# ---------------------------------------------------------------------------

class FingerprintWindow:
    """One fingerprint's effective interval [start, end); open ends are None."""

    def __init__(self, fingerprint, start, end):
        self.fingerprint = fingerprint
        self.start = start
        self.end = end

    def covers(self, moment):
        if self.start is not None and moment < self.start:
            return False
        if self.end is not None and moment >= self.end:
            return False
        return True


class ServicePolicy:
    def __init__(self, name, windows, min_samples, anomaly_ratio):
        self.name = name
        self.windows = list(windows)
        self.min_samples = min_samples
        self.anomaly_ratio = anomaly_ratio  # exact Fraction

    def is_expected(self, fingerprint, moment):
        """True if fingerprint is inside any effective window at moment."""
        return any(
            w.fingerprint == fingerprint and w.covers(moment) for w in self.windows
        )


class Policy:
    def __init__(self, services):
        self._services = dict(services)

    def for_service(self, name):
        return self._services.get(name)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
            raise ValueError("policy must be an object with a 'services' object")
        services = {}
        for name, spec in data["services"].items():
            if not isinstance(spec, dict):
                raise ValueError("service %r: policy entry must be an object" % (name,))
            for field in ("min_samples", "anomaly_ratio"):
                if field not in spec:
                    raise ValueError("service %r: missing required field %r" % (name, field))
            min_samples = int(spec["min_samples"])
            if min_samples < 1:
                raise ValueError("service %r: min_samples must be >= 1" % (name,))
            ratio = Fraction(str(spec["anomaly_ratio"]))
            if ratio < 0 or ratio > 1:
                raise ValueError("service %r: anomaly_ratio must be within [0, 1]" % (name,))
            windows = []
            for entry in spec.get("fingerprints", []):
                if "fingerprint" not in entry:
                    raise ValueError("service %r: fingerprint entry lacks 'fingerprint'" % (name,))
                start = parse_ts(entry["valid_from"]) if entry.get("valid_from") else None
                end = parse_ts(entry["valid_until"]) if entry.get("valid_until") else None
                if start is not None and end is not None and start >= end:
                    raise ValueError(
                        "service %r: fingerprint %r has empty/invalid window"
                        % (name, entry["fingerprint"])
                    )
                windows.append(FingerprintWindow(entry["fingerprint"], start, end))
            services[name] = ServicePolicy(name, windows, min_samples, ratio)
        return cls(services)


def load_policy(path):
    with open(path, "r", encoding="utf-8") as fh:
        return Policy.from_dict(json.load(fh))


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def analyze_lines(lines, policy):
    """Analyze an iterable of raw JSONL lines against a Policy.

    Returns a report dict with keys: alerts, evaluated_windows, quality.
    """
    parse_error_lines = []
    missing_field_records = []
    invalid_timestamp_records = []
    conflicting_connections = []
    unjudgeable_records = []
    failed_handshakes = 0
    duplicate_reports = 0

    # Pass 1: parse, validate required fields, group by (service, conn_id) and
    # deduplicate identical reports. Identity is defined over REQUIRED_FIELDS.
    groups = {}
    group_order = []
    for lineno, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            parse_error_lines.append(lineno)
            continue
        if not isinstance(record, dict):
            parse_error_lines.append(lineno)
            continue
        missing = [f for f in REQUIRED_FIELDS if record.get(f) in (None, "")]
        if missing:
            missing_field_records.append({"line": lineno, "missing": missing})
            continue
        key = (record["service"], record["conn_id"])
        if key not in groups:
            groups[key] = {}
            group_order.append(key)
        canon = json.dumps({f: record[f] for f in REQUIRED_FIELDS}, sort_keys=True)
        if canon in groups[key]:
            duplicate_reports += 1
        else:
            groups[key][canon] = record

    # Pass 2: drop conflicting connections, then bucket judgeable successful
    # handshakes into (service, 10-minute window) buckets.
    buckets = {}
    for key in group_order:
        variants = groups[key]
        if len(variants) > 1:
            conflicting_connections.append({
                "service": key[0],
                "conn_id": key[1],
                "variants": [json.loads(c) for c in sorted(variants)],
            })
            continue
        record = next(iter(variants.values()))
        if record["result"] != "success":
            failed_handshakes += 1
            continue
        svc_policy = policy.for_service(record["service"])
        if svc_policy is None:
            unjudgeable_records.append({
                "service": record["service"],
                "conn_id": record["conn_id"],
                "reason": "no policy for service",
            })
            continue
        try:
            moment = parse_ts(record["ts"])
        except ValueError as exc:
            invalid_timestamp_records.append({
                "service": record["service"],
                "conn_id": record["conn_id"],
                "ts": record["ts"],
                "error": str(exc),
            })
            continue
        win = window_start(moment)
        bucket = buckets.setdefault(
            (record["service"], win), {"total": 0, "anomalies": {}}
        )
        bucket["total"] += 1
        if not svc_policy.is_expected(record["fingerprint"], moment):
            fp = record["fingerprint"]
            bucket["anomalies"][fp] = bucket["anomalies"].get(fp, 0) + 1

    # Pass 3: evaluate alert conditions per (service, window).
    alerts = []
    evaluated = []
    for service, win in sorted(buckets, key=lambda k: (k[1], k[0])):
        bucket = buckets[(service, win)]
        svc_policy = policy.for_service(service)
        total = bucket["total"]
        anomalous = sum(bucket["anomalies"].values())
        ratio = Fraction(anomalous, total)
        alerted = (
            anomalous > 0
            and total >= svc_policy.min_samples
            and ratio >= svc_policy.anomaly_ratio
        )
        entry = {
            "service": service,
            "window_start": format_ts(win),
            "window_end": format_ts(win + timedelta(seconds=WINDOW_SECONDS)),
            "samples": total,
            "anomalous": anomalous,
        }
        evaluated.append(dict(entry, alerted=alerted))
        if alerted:
            alerts.append(dict(
                entry,
                anomaly_ratio=float(ratio),
                min_samples=svc_policy.min_samples,
                threshold=float(svc_policy.anomaly_ratio),
                anomalous_fingerprints=[
                    {"fingerprint": fp, "count": bucket["anomalies"][fp]}
                    for fp in sorted(bucket["anomalies"])
                ],
            ))

    return {
        "alerts": alerts,
        "evaluated_windows": evaluated,
        "quality": {
            "parse_error_lines": parse_error_lines,
            "missing_field_records": missing_field_records,
            "invalid_timestamp_records": invalid_timestamp_records,
            "conflicting_connections": conflicting_connections,
            "unjudgeable_records": unjudgeable_records,
            "failed_handshakes": failed_handshakes,
            "duplicate_reports": duplicate_reports,
        },
    }


def analyze_file(path, policy):
    with open(path, "r", encoding="utf-8") as fh:
        return analyze_lines(fh, policy)
