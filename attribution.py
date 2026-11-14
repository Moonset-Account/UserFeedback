"""Core logic: attribute process-start events to confirmed login sessions.

Offline forensic helper. Inputs:
  - login/logout CSV exports (one or more files),
  - process-start JSONL exports (one or more files),
  - a clock calibration CSV giving, per (host, boot_id), the offset of the
    local clock to UTC plus an error margin.

All timestamps in event files are *local* clock readings (naive ISO 8601).
A local reading T with calibration (offset, err) means the true UTC instant
lies anywhere in [T + offset - err, T + offset + err].

A closed session (login + logout known) is therefore only *definitely* valid
during [login_utc + err, logout_utc - err]: outside that window the session
might not have started yet or might already have ended.

A process start at local time T may have happened anywhere in
[T + offset - err, T + offset + err]. Attribution is confirmed only when
that whole possible interval fits inside the definite valid window of
exactly one closed session with the same host, boot_id and session_id.
"""

import csv
import json
from datetime import datetime, timedelta

SESSION_REQUIRED = ("source", "event_id", "event_type", "host", "boot_id",
                    "session_id", "user", "timestamp")
PROCESS_REQUIRED = ("source", "event_id", "host", "boot_id", "session_id",
                    "pid", "process", "timestamp")
EVENT_TYPES = ("login", "logout")


def _parse_ts(value, where):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError("invalid timestamp %r at %s" % (value, where))


# --------------------------------------------------------------------------
# Readers (every record keeps provenance: original file + line number)
# --------------------------------------------------------------------------

def read_session_file(path):
    """Read a login/logout CSV. Returns [{'data': {...}, 'prov': {...}}]."""
    records = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        missing = [c for c in SESSION_REQUIRED if c not in fieldnames]
        if missing:
            raise ValueError("%s: missing CSV columns: %s" % (path, ", ".join(missing)))
        for row in reader:
            line = reader.line_num  # physical line number of this row
            data = {k: (v if v is not None else "").strip()
                    for k, v in row.items() if k is not None}
            empty = [k for k in SESSION_REQUIRED if not data.get(k)]
            if empty:
                raise ValueError("%s:%d: empty required field(s): %s"
                                 % (path, line, ", ".join(empty)))
            if data["event_type"] not in EVENT_TYPES:
                raise ValueError("%s:%d: event_type must be login or logout, got %r"
                                 % (path, line, data["event_type"]))
            _parse_ts(data["timestamp"], "%s:%d" % (path, line))
            records.append({"data": data, "prov": {"file": path, "line": line}})
    return records


def read_process_file(path):
    """Read a process-start JSONL file. Returns [{'data': {...}, 'prov': {...}}]."""
    records = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("%s:%d: invalid JSON: %s" % (path, lineno, exc))
            if not isinstance(obj, dict):
                raise ValueError("%s:%d: JSONL record must be an object" % (path, lineno))
            missing = [k for k in PROCESS_REQUIRED if k not in obj]
            if missing:
                raise ValueError("%s:%d: missing field(s): %s"
                                 % (path, lineno, ", ".join(missing)))
            _parse_ts(obj["timestamp"], "%s:%d" % (path, lineno))
            records.append({"data": obj, "prov": {"file": path, "line": lineno}})
    return records


def read_calibration(path):
    """Read calibration CSV -> {(host, boot_id): (offset_seconds, error_seconds)}."""
    table = {}
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        for col in ("host", "boot_id", "offset_seconds", "error_seconds"):
            if col not in fieldnames:
                raise ValueError("%s: missing calibration column %r" % (path, col))
        for row in reader:
            line = reader.line_num
            key = ((row["host"] or "").strip(), (row["boot_id"] or "").strip())
            if not key[0] or not key[1]:
                raise ValueError("%s:%d: calibration row needs host and boot_id"
                                 % (path, line))
            try:
                val = (float(row["offset_seconds"]), abs(float(row["error_seconds"])))
            except (TypeError, ValueError):
                raise ValueError("%s:%d: offset_seconds/error_seconds must be numeric"
                                 % (path, line))
            if key in table and table[key] != val:
                raise ValueError("%s:%d: conflicting calibration rows for host=%s boot_id=%s"
                                 % (path, line, key[0], key[1]))
            table[key] = val
    return table


# --------------------------------------------------------------------------
# Deduplication of repeated exports
# --------------------------------------------------------------------------

def _sort_prov(provs):
    return sorted(provs, key=lambda p: (p["file"], p["line"]))


def dedup(records, kind):
    """Group records by (source, event_id).

    Identical content merges into one record keeping every provenance.
    Same key with differing content becomes a conflict entry and is
    excluded from matching/timeline (its content cannot be trusted).
    """
    groups = {}
    for rec in records:
        key = (rec["data"]["source"], rec["data"]["event_id"])
        groups.setdefault(key, []).append(rec)

    merged, conflicts = [], []
    for key in sorted(groups):
        variants = {}
        for rec in groups[key]:
            content = json.dumps(rec["data"], sort_keys=True, ensure_ascii=False)
            variants.setdefault(content, []).append(rec)
        if len(variants) == 1:
            prov = _sort_prov([r["prov"] for r in groups[key]])
            merged.append({"data": groups[key][0]["data"], "prov": prov})
        else:
            conflicts.append({
                "kind": kind,
                "source": key[0],
                "event_id": key[1],
                "variants": [
                    {"content": json.loads(content),
                     "provenance": _sort_prov([r["prov"] for r in recs])}
                    for content, recs in sorted(variants.items())
                ],
            })
    return merged, conflicts


# --------------------------------------------------------------------------
# Session assembly and definite valid window
# --------------------------------------------------------------------------

def assemble_sessions(events):
    """Group deduped login/logout events into sessions.

    One session per (host, boot_id, session_id). Closed means at least one
    login and one logout were seen. With several logins/logouts the earliest
    login and latest logout delimit the session.
    """
    groups = {}
    for e in events:
        d = e["data"]
        key = (d["host"], d["boot_id"], d["session_id"])
        g = groups.setdefault(key, {"logins": [], "logouts": []})
        g["logins" if d["event_type"] == "login" else "logouts"].append(e)

    sessions = []
    for host, boot_id, session_id in sorted(groups):
        g = groups[(host, boot_id, session_id)]
        logins = sorted(g["logins"], key=lambda e: (e["data"]["timestamp"], e["data"]["event_id"]))
        logouts = sorted(g["logouts"], key=lambda e: (e["data"]["timestamp"], e["data"]["event_id"]))
        users = sorted({e["data"]["user"] for e in g["logins"] + g["logouts"]})
        prov = _sort_prov([p for e in g["logins"] + g["logouts"]
                           for p in e["prov"]])
        sessions.append({
            "host": host,
            "boot_id": boot_id,
            "session_id": session_id,
            "users": users,
            "closed": bool(logins and logouts),
            "login_local": logins[0]["data"]["timestamp"] if logins else None,
            "logout_local": logouts[-1]["data"]["timestamp"] if logouts else None,
            "provenance": prov,
        })
    return sessions


def definite_window(session, calib):
    """UTC window during which a closed session definitely existed.

    Returns (lo, hi) datetimes, or None if the session is not closed or its
    (host, boot_id) has no calibration row.
    """
    if not session["closed"]:
        return None
    cal = calib.get((session["host"], session["boot_id"]))
    if cal is None:
        return None
    off, err = cal
    lo = _parse_ts(session["login_local"], "session login") + timedelta(seconds=off + err)
    hi = _parse_ts(session["logout_local"], "session logout") + timedelta(seconds=off - err)
    return lo, hi


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------

def attribute(proc, sessions, calib):
    """Attribute one process event. Returns ((lo, hi) | None, attribution dict)."""
    d = proc["data"]
    cal = calib.get((d["host"], d["boot_id"]))
    if cal is None:
        return None, {
            "status": "unattributed",
            "reason": "missing_calibration",
            "detail": "no calibration row for host=%s boot_id=%s"
                      % (d["host"], d["boot_id"]),
        }
    off, err = cal
    ts = _parse_ts(d["timestamp"], "process event_id=%s" % d["event_id"])
    lo = ts + timedelta(seconds=off - err)
    hi = ts + timedelta(seconds=off + err)
    interval = (lo, hi)

    cands = [s for s in sessions
             if s["host"] == d["host"]
             and s["boot_id"] == d["boot_id"]
             and s["session_id"] == d["session_id"]]
    if not cands:
        return interval, {
            "status": "unattributed",
            "reason": "no_matching_session",
            "detail": "no session with same host/boot_id/session_id",
        }
    closed = [s for s in cands if s["closed"]]
    if not closed:
        return interval, {
            "status": "unattributed",
            "reason": "session_not_closed",
            "detail": "matching session lacks a login or logout record",
        }

    containing = []
    calibration_missing = False
    for s in closed:
        window = definite_window(s, calib)
        if window is None:
            calibration_missing = True
            continue
        wlo, whi = window
        if wlo <= lo and hi <= whi:
            containing.append((s, wlo, whi))

    if len(containing) == 1:
        s, wlo, whi = containing[0]
        return interval, {
            "status": "confirmed",
            "user": s["users"][0] if len(s["users"]) == 1 else None,
            "users": s["users"],
            "session_definite_window_utc": [wlo.isoformat(), whi.isoformat()],
            "session_provenance": s["provenance"],
        }
    if len(containing) > 1:
        return interval, {
            "status": "unattributed",
            "reason": "ambiguous_sessions",
            "detail": "interval fits inside %d closed sessions" % len(containing),
        }
    if calibration_missing:
        return interval, {
            "status": "unattributed",
            "reason": "missing_calibration",
            "detail": "no calibration row for candidate session host/boot_id",
        }
    return interval, {
        "status": "unattributed",
        "reason": "interval_outside_session",
        "detail": "possible UTC interval is not fully inside any closed "
                  "session's definite valid window",
    }


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def build_report(session_paths, process_paths, calibration_path):
    """Build the full deterministic report from the input files."""
    calib = read_calibration(calibration_path)

    raw_sessions = []
    for path in session_paths:
        raw_sessions.extend(read_session_file(path))
    raw_processes = []
    for path in process_paths:
        raw_processes.extend(read_process_file(path))

    session_events, session_conflicts = dedup(raw_sessions, "session")
    process_events, process_conflicts = dedup(raw_processes, "process")
    sessions = assemble_sessions(session_events)

    timeline = []
    for proc in process_events:
        d = proc["data"]
        interval, attribution = attribute(proc, sessions, calib)
        timeline.append({
            "event_id": d["event_id"],
            "source": d["source"],
            "host": d["host"],
            "boot_id": d["boot_id"],
            "session_id": d["session_id"],
            "pid": d["pid"],
            "process": d["process"],
            "local_timestamp": d["timestamp"],
            "utc_interval": [interval[0].isoformat(), interval[1].isoformat()]
                            if interval else None,
            "attribution": attribution,
            "provenance": proc["prov"],
        })
    timeline.sort(key=lambda e: (
        e["utc_interval"][0] if e["utc_interval"] else e["local_timestamp"],
        e["host"], e["boot_id"], e["session_id"], str(e["pid"]), e["event_id"]))

    sessions_out = []
    for s in sessions:
        window = definite_window(s, calib)
        sessions_out.append({
            "host": s["host"],
            "boot_id": s["boot_id"],
            "session_id": s["session_id"],
            "users": s["users"],
            "closed": s["closed"],
            "login_local": s["login_local"],
            "logout_local": s["logout_local"],
            "definite_window_utc": [window[0].isoformat(), window[1].isoformat()]
                                   if window else None,
            "provenance": s["provenance"],
        })

    conflicts = sorted(session_conflicts + process_conflicts,
                       key=lambda c: (c["kind"], c["source"], c["event_id"]))
    return {"timeline": timeline, "sessions": sessions_out, "conflicts": conflicts}
