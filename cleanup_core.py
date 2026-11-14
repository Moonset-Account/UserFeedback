"""Two-step historical chat attachment cleanup (local directory simulation).

Step 1 -- plan:    operations confirms a deletion candidate list. Each
                   confirmed entry snapshots the content version of the
                   attachment at confirmation time.
Step 2 -- execute: the executor processes confirmed entries one by one.
                   Immediately before each real deletion it re-reads the
                   *current* legal holds, retention rules and content
                   version from disk. If the attachment was replaced
                   (version changed) or newly placed under hold, the entry
                   is skipped with a reason -- the previously confirmed
                   list never applies to a new version.

Execution is resumable: a persistent journal records per-entry status so
an interrupted run can be re-run and continues with the remaining
entries. Statuses are distinguished as:

  deleted        -- file removed and index updated (terminal)
  skipped        -- policy/state based refusal, reason recorded (terminal;
                    a new confirmation round is required to reconsider)
  pending_retry  -- attempted but hit a transient I/O error; a re-run
                    of `execute` will retry it
  pending        -- confirmed but never attempted (only visible between
                    plan and execute, or after an interrupted run)

Starting a new `plan` round invalidates the previous journal.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile

SPACES_FILE = "spaces.json"
INDEX_FILE = "index.json"
HOLDS_FILE = "holds.json"
CONFIRMED_FILE = "confirmed.json"
JOURNAL_FILE = os.path.join("state", "journal.json")
RESULT_FILE = "result.json"

STATUS_DELETED = "deleted"
STATUS_SKIPPED = "skipped"
STATUS_PENDING_RETRY = "pending_retry"
STATUS_PENDING = "pending"
TERMINAL_STATUSES = {STATUS_DELETED, STATUS_SKIPPED}


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def _write_json_atomic(path, payload):
    """Write JSON atomically (tmp file + rename) so a crash mid-write never
    leaves a half-written journal/index behind."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_spaces(data_dir):
    return _read_json(os.path.join(data_dir, SPACES_FILE), {}).get("spaces", {})


def load_index(data_dir):
    return _read_json(os.path.join(data_dir, INDEX_FILE), {"attachments": []})


def load_holds(data_dir):
    return _read_json(os.path.join(data_dir, HOLDS_FILE), {}).get("holds", [])


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

def is_expired(attachment, retention_days, now):
    """An attachment is deletable once it has been kept for the full
    retention duration of its business space: age >= retention_days."""
    created = datetime.date.fromisoformat(attachment["created_at"])
    return (now - created).days >= retention_days


def blocking_holds(holds, attachment):
    """Return ids of active holds covering this attachment. A hold may
    target a single attachment or a whole case; ANY active hold blocks."""
    aid = attachment["attachment_id"]
    cid = attachment["case_id"]
    blocking = []
    for hold in holds:
        if not hold.get("active"):
            continue
        if hold.get("scope") == "attachment" and hold.get("target_id") == aid:
            blocking.append(hold["hold_id"])
        elif hold.get("scope") == "case" and hold.get("target_id") == cid:
            blocking.append(hold["hold_id"])
    return sorted(blocking)


# ---------------------------------------------------------------------------
# step 1: plan (operations confirmation)
# ---------------------------------------------------------------------------

def plan_confirmation(data_dir, now):
    """Build the confirmed deletion list: every indexed attachment whose
    retention period (per its business space) has expired. The current
    content version is snapshotted into each entry. Starting a new round
    invalidates any previous execution journal."""
    spaces = load_spaces(data_dir)
    index = load_index(data_dir)
    entries = []
    for att in index["attachments"]:
        space = spaces.get(att["space"])
        if space is None:
            continue  # unknown business space: not eligible
        if is_expired(att, space["retention_days"], now):
            entries.append({
                "attachment_id": att["attachment_id"],
                "case_id": att["case_id"],
                "space": att["space"],
                "confirmed_version": att["version"],
            })
    payload = {"generated_at": now.isoformat(), "entries": entries}
    _write_json_atomic(os.path.join(data_dir, CONFIRMED_FILE), payload)
    try:
        os.remove(os.path.join(data_dir, JOURNAL_FILE))
    except FileNotFoundError:
        pass
    return entries


# ---------------------------------------------------------------------------
# step 2: execute (resumable)
# ---------------------------------------------------------------------------

def _resolve_target(data_dir, rel_path):
    root = os.path.realpath(data_dir)
    target = os.path.realpath(os.path.join(data_dir, rel_path))
    if target != root and target.startswith(root + os.sep):
        return target
    return None


def _process_entry(data_dir, entry, now):
    """Re-read current holds, retention rules and content version for one
    confirmed entry, then delete or refuse. Returns (status, reason)."""
    aid = entry["attachment_id"]
    # Re-read current state from disk for every single entry.
    spaces = load_spaces(data_dir)
    holds = load_holds(data_dir)
    index = load_index(data_dir)

    att = None
    for candidate in index["attachments"]:
        if candidate["attachment_id"] == aid:
            att = candidate
            break
    if att is None:
        return STATUS_SKIPPED, "missing_from_index"

    if att.get("version") != entry["confirmed_version"]:
        # Replaced after confirmation: the confirmed list must not apply
        # to the new version.
        return STATUS_SKIPPED, "version_changed"

    blocking = blocking_holds(holds, att)
    if blocking:
        return STATUS_SKIPPED, "legal_hold:" + ",".join(blocking)

    space = spaces.get(att["space"])
    if space is None:
        return STATUS_SKIPPED, "unknown_space"
    if not is_expired(att, space["retention_days"], now):
        return STATUS_SKIPPED, "within_retention"

    target = _resolve_target(data_dir, att["path"])
    if target is None:
        return STATUS_SKIPPED, "unsafe_path"
    if not os.path.exists(target):
        return STATUS_SKIPPED, "file_missing"
    try:
        os.remove(target)
    except OSError as exc:
        return STATUS_PENDING_RETRY, "io_error:" + str(exc)

    index["attachments"] = [
        a for a in index["attachments"] if a["attachment_id"] != aid
    ]
    _write_json_atomic(os.path.join(data_dir, INDEX_FILE), index)
    return STATUS_DELETED, None


def execute_confirmed(data_dir, now):
    """Process the confirmed list entry by entry. Entries already terminal
    in the journal (deleted/skipped) are reported from the journal and not
    re-processed; pending and pending_retry entries are (re)attempted."""
    confirmed = _read_json(os.path.join(data_dir, CONFIRMED_FILE), None)
    if confirmed is None:
        raise FileNotFoundError(
            "confirmed list not found; run the plan step first")

    journal_path = os.path.join(data_dir, JOURNAL_FILE)
    journal = _read_json(journal_path, {"entries": {}})
    journal.setdefault("entries", {})

    outcomes = []
    for entry in confirmed["entries"]:
        aid = entry["attachment_id"]
        prev = journal["entries"].get(aid)
        if prev and prev["status"] in TERMINAL_STATUSES:
            outcomes.append({
                "attachment_id": aid,
                "status": prev["status"],
                "reason": prev.get("reason"),
                "from_journal": True,
            })
            continue
        status, reason = _process_entry(data_dir, entry, now)
        journal["entries"][aid] = {
            "status": status,
            "reason": reason,
            "confirmed_version": entry["confirmed_version"],
            "updated_at": now.isoformat(),
        }
        _write_json_atomic(journal_path, journal)  # persist after each entry
        outcomes.append({
            "attachment_id": aid,
            "status": status,
            "reason": reason,
            "from_journal": False,
        })

    counts = {STATUS_DELETED: 0, STATUS_SKIPPED: 0,
              STATUS_PENDING_RETRY: 0, STATUS_PENDING: 0}
    for entry in confirmed["entries"]:
        aid = entry["attachment_id"]
        journaled = journal["entries"].get(aid)
        status = journaled["status"] if journaled else STATUS_PENDING
        counts[status] = counts.get(status, 0) + 1

    result = {"run_at": now.isoformat(), "counts": counts, "outcomes": outcomes}
    _write_json_atomic(os.path.join(data_dir, RESULT_FILE), result)
    return result


def status_summary(data_dir):
    """Current per-entry status view (used by the `status` CLI command)."""
    confirmed = _read_json(os.path.join(data_dir, CONFIRMED_FILE), {"entries": []})
    journal = _read_json(os.path.join(data_dir, JOURNAL_FILE), {"entries": {}})
    counts = {STATUS_DELETED: 0, STATUS_SKIPPED: 0,
              STATUS_PENDING_RETRY: 0, STATUS_PENDING: 0}
    entries = {}
    for entry in confirmed.get("entries", []):
        aid = entry["attachment_id"]
        journaled = journal.get("entries", {}).get(aid)
        status = journaled["status"] if journaled else STATUS_PENDING
        counts[status] = counts.get(status, 0) + 1
        entries[aid] = {"status": status,
                        "reason": (journaled or {}).get("reason")}
    return {"counts": counts, "entries": entries}


# ---------------------------------------------------------------------------
# demo dataset (used by `app.py demo` and the README walkthrough)
# ---------------------------------------------------------------------------

def create_demo_dataset(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    spaces = {"spaces": {
        "support": {"retention_days": 365},
        "sales": {"retention_days": 30},
    }}
    holds = {"holds": [
        {"hold_id": "H-DEMO-CASE", "scope": "case",
         "target_id": "case-hold", "active": True},
        {"hold_id": "H-DEMO-ATT", "scope": "attachment",
         "target_id": "att-demo-att-hold", "active": True},
        {"hold_id": "H-DEMO-OFF", "scope": "attachment",
         "target_id": "att-demo-inactive", "active": False},
    ]}
    attachments = [
        ("att-demo-expired", "case-plain", "support", "2025-01-01"),
        ("att-demo-fresh", "case-plain", "support", "2026-11-01"),
        ("att-demo-case-hold", "case-hold", "support", "2025-01-01"),
        ("att-demo-att-hold", "case-plain", "support", "2025-01-01"),
        ("att-demo-inactive", "case-plain", "support", "2025-01-01"),
    ]
    index = {"attachments": []}
    lines = []
    for aid, case_id, space, created in attachments:
        rel = os.path.join("files", case_id, aid + ".bin")
        abspath = os.path.join(data_dir, rel)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "wb") as fh:
            fh.write(("demo content for " + aid + "\n").encode("utf-8"))
        index["attachments"].append({
            "attachment_id": aid,
            "case_id": case_id,
            "space": space,
            "path": rel,
            "version": 1,
            "created_at": created,
        })
        lines.append(f"{aid} case={case_id} space={space} created={created}")
    _write_json_atomic(os.path.join(data_dir, SPACES_FILE), spaces)
    _write_json_atomic(os.path.join(data_dir, HOLDS_FILE), holds)
    _write_json_atomic(os.path.join(data_dir, INDEX_FILE), index)
    return lines
