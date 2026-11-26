"""Core offline transfer approval engine."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any, Iterable

from .intervals import (
    Interval,
    add_intervals,
    intersect,
    intersect_all,
    merge_intervals,
    sample_point,
    subtract_many,
    union_minus_intersection,
)
from .parser import (
    ApprovalInputError,
    KeyRef,
    as_list,
    first_present,
    key_accepts,
    normalize_subject,
    optional_mapping,
    parse_clock_error,
    parse_interval,
    parse_key_ref,
    parse_number,
    require_mapping,
)


class TranscriptValidationError(ApprovalInputError):
    """Raised when callers assert that a transcript failed validation."""


SUBJECT_LABELS = {"send": "sender", "receive": "receiver"}


def _conflict(
    code: str,
    message: str,
    *,
    category: str = "contradiction",
    sources: Iterable[str] | None = None,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "category": category,
        "message": message,
        "sources": list(sources or []),
    }
    result.update(details)
    return result


def _extract_time_interval(mapping: dict[str, Any], *, name: str) -> Interval | None:
    direct = first_present(
        mapping,
        "trusted_interval",
        "time_interval",
        "committed_interval",
        "interval",
        "time_range",
        "credible_interval",
    )
    if direct is not None:
        return parse_interval(direct, name=name)

    observed = first_present(
        mapping, "observed_at", "local_time", "timestamp", "clock_time", "reported_time"
    )
    if observed is not None:
        earlier, later = parse_clock_error(
            first_present(mapping, "clock_error", "error", "uncertainty", "skew")
        )
        observed = parse_number(observed, f"{name}.observed_at")
        return Interval(observed - earlier, observed + later, True, True)

    start_end = first_present(mapping, "start", "lo")
    if start_end is not None and first_present(mapping, "end", "hi", "upper") is not None:
        return parse_interval(mapping, name=name)

    return None


def _key_from_record(record: dict[str, Any]) -> KeyRef | None:
    return parse_key_ref(record) or parse_key_ref(record.get("key_commitment"))


def _index_commitments(
    transcript: dict[str, Any], conflicts: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    raw = first_present(transcript, "commitments", "key_commitments", "promises") or []
    indexed: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(as_list(raw)):
        record = require_mapping(item, f"transcript.commitments[{i}]")
        cid = str(first_present(record, "id", "commitment_id", "promise_id") or f"commitment-{i+1}")
        subject_value = first_present(record, "subject", "side", "phase", "event", "event_id")
        subject = None
        if subject_value is not None:
            try:
                subject = normalize_subject(subject_value)
            except ApprovalInputError:
                # Event IDs such as "send-key-1" may be used by transcripts;
                # callers can state subject explicitly in that case.
                subject = None
                for candidate in ("send", "receive"):
                    if candidate in str(subject_value).lower() or (
                        candidate == "receive" and "recv" in str(subject_value).lower()
                    ):
                        subject = candidate
                        break
                if subject is None:
                    raise

        interval = None
        if first_present(
            record,
            "interval",
            "time_interval",
            "committed_interval",
            "trusted_interval",
            "time_range",
        ) is not None:
            interval = _extract_time_interval(record, name=f"commitment.{cid}")

        indexed[cid] = {
            "id": cid,
            "subject": subject,
            "key": _key_from_record(record),
            "interval": interval,
            "window_id": first_present(record, "window_id", "window", "entry_window_id"),
            "raw": record,
        }
    return indexed


def _prior_window_index(
    transcript: dict[str, Any], commitments: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    prior: dict[str, dict[str, Any]] = {}
    raw = first_present(
        transcript, "prior_windows", "known_windows", "handover_windows", "windows"
    )
    for i, item in enumerate(as_list(raw)):
        record = require_mapping(item, f"transcript.prior_windows[{i}]")
        wid = str(first_present(record, "id", "window_id") or f"prior-window-{i+1}")
        side_value = first_present(record, "side", "subject", "endpoint")
        side = normalize_subject(side_value) if side_value is not None else None
        prior[wid] = {
            "id": wid,
            "side": side,
            "key": _key_from_record(record),
            "raw": record,
        }

    for commitment in commitments.values():
        wid = commitment.get("window_id")
        if wid is not None and str(wid) not in prior:
            prior[str(wid)] = {
                "id": str(wid),
                "side": commitment.get("subject"),
                "key": commitment.get("key"),
                "raw": commitment.get("raw", {}),
            }
    return prior


def _side_ids_from_value(value: Any) -> tuple[list[str], list[str]]:
    sender: list[str] = []
    receiver: list[str] = []
    if value is None:
        return sender, receiver

    if isinstance(value, dict):
        sender_keys = (
            "sender", "senders", "send", "s",
            "sender_windows", "send_windows", "sender_window_ids",
        )
        receiver_keys = (
            "receiver", "receivers", "receive", "recv", "r",
            "receiver_windows", "receive_windows", "recv_windows",
            "receiver_window_ids",
        )
        for key in sender_keys:
            if key in value and value[key] is not None:
                sender.extend(str(x) for x in as_list(value[key]))
        for key in receiver_keys:
            if key in value and value[key] is not None:
                receiver.extend(str(x) for x in as_list(value[key]))
        # A dict may itself be a window ID -> metadata map in some transcripts.
        if not sender and not receiver and value:
            for wid, meta in value.items():
                side = None
                if isinstance(meta, dict):
                    side_value = first_present(meta, "side", "subject")
                    if side_value is not None:
                        side = normalize_subject(side_value)
                if side == "send":
                    sender.append(str(wid))
                elif side == "receive":
                    receiver.append(str(wid))
        return sender, receiver

    for item in as_list(value):
        if isinstance(item, dict):
            side_value = first_present(item, "side", "subject", "endpoint")
            ids = first_present(item, "ids", "window_ids", "windows", "id", "window_id")
            if side_value is not None and ids is not None:
                side = normalize_subject(side_value)
                target = sender if side == "send" else receiver
                target.extend(str(x) for x in as_list(ids))
            continue

        text = str(item)
        if ":" in text:
            prefix, wid = text.split(":", 1)
            prefix = prefix.strip().lower()
            if prefix in ("sender", "send", "s"):
                sender.append(wid)
                continue
            if prefix in ("receiver", "receive", "recv", "r"):
                receiver.append(wid)
                continue
        # Unprefixed IDs are resolved against prior window metadata later.
        sender.append(text)
    return sender, receiver


def _audit_recoveries(
    transcript: dict[str, Any],
    commitments: dict[str, dict[str, Any]],
    requested_key: KeyRef | None,
    conflicts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prior = _prior_window_index(transcript, commitments)
    raw_recoveries = first_present(transcript, "recoveries", "recovery_events", "resumptions") or []
    declarations: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []

    for i, raw_rec in enumerate(as_list(raw_recoveries)):
        rec = require_mapping(raw_rec, f"transcript.recoveries[{i}]")
        rid = str(first_present(rec, "id", "recovery_id") or f"recovery-{i+1}")
        inherited = first_present(
            rec, "inherits", "inherit", "inherit_windows", "inherited_windows"
        )

        sender_ids, receiver_ids = _side_ids_from_value(inherited)

        # Explicit side fields override/augment shorthand.
        for field_name in (
            "sender_windows", "senders", "send_windows", "sender_window_ids"
        ):
            if field_name in rec:
                extra_s, _ = _side_ids_from_value({field_name: rec[field_name]})
                sender_ids.extend(extra_s)
        for field_name in (
            "receiver_windows", "receivers", "receive_windows",
            "recv_windows", "receiver_window_ids",
        ):
            if field_name in rec:
                _, extra_r = _side_ids_from_value({field_name: rec[field_name]})
                receiver_ids.extend(extra_r)

        # "all" is accepted only as an explicit transcript declaration.  It is
        # never an implicit default.
        if inherited == "all" or (isinstance(inherited, dict) and inherited.get("all") is True):
            sender_ids = [wid for wid, meta in prior.items() if meta.get("side") == "send"]
            receiver_ids = [wid for wid, meta in prior.items() if meta.get("side") == "receive"]

        # Resolve unprefixed global IDs using prior metadata.
        resolved_sender: list[str] = []
        resolved_receiver: list[str] = []
        for wid in sender_ids:
            meta = prior.get(wid)
            if meta and meta.get("side") == "receive":
                resolved_receiver.append(wid)
            else:
                resolved_sender.append(wid)
        for wid in receiver_ids:
            meta = prior.get(wid)
            if meta and meta.get("side") == "send":
                resolved_sender.append(wid)
            else:
                resolved_receiver.append(wid)

        rec_report: dict[str, Any] = {
            "id": rid,
            "declared": {"sender": resolved_sender, "receiver": resolved_receiver},
            "valid_windows": [],
            "problems": [],
        }

        for side, ids in (("send", resolved_sender), ("receive", resolved_receiver)):
            for wid in dict.fromkeys(ids):
                declaration = {"recovery_id": rid, "side": side, "window_id": wid}
                declarations.append(declaration)
                meta = prior.get(wid)
                if meta is None:
                    conflict = _conflict(
                        "unknown_inherited_window",
                        f"transcript {rid} declares inheritance of {side} window {wid!r}, "
                        "but it is absent from the validated prior transcript",
                        category="transcript",
                        sources=[f"recovery:{rid}", "transcript.prior_windows"],
                        recovery_id=rid,
                        side=side,
                        window_id=wid,
                    )
                    conflicts.append(conflict)
                    rec_report["problems"].append(conflict)
                    continue

                prior_side = meta.get("side")
                if prior_side is not None and prior_side != side:
                    conflict = _conflict(
                        "recovery_window_side_mismatch",
                        f"window {wid!r} belongs to {prior_side}, but recovery {rid} "
                        f"declares it for {side}",
                        category="transcript",
                        recovery_id=rid,
                        side=side,
                        window_id=wid,
                        sources=[f"recovery:{rid}", f"window:{wid}"],
                    )
                    conflicts.append(conflict)
                    rec_report["problems"].append(conflict)

                prior_key = meta.get("key")
                if (
                    requested_key is not None
                    and prior_key is not None
                    and not key_accepts(prior_key, requested_key)
                ):
                    conflict = _conflict(
                        "inherited_key_mismatch",
                        f"inherited window {wid!r} does not accept the promised key group",
                        category="key",
                        recovery_id=rid,
                        side=side,
                        window_id=wid,
                        sources=[f"recovery:{rid}", f"window:{wid}"],
                    )
                    conflicts.append(conflict)
                    rec_report["problems"].append(conflict)

                rec_report["valid_windows"].append(declaration)

        reports.append(rec_report)

    return declarations, reports


def _parse_endpoint_windows(
    side: str,
    endpoint: dict[str, Any],
    requested_key: KeyRef | None,
    declarations: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_windows = first_present(
        endpoint, "windows", "entry_windows", "intake_windows", "announced_windows"
    ) or []
    parsed: list[dict[str, Any]] = []

    for i, raw_window in enumerate(as_list(raw_windows)):
        window = require_mapping(raw_window, f"{side}.windows[{i}]")
        wid = str(first_present(window, "id", "window_id") or f"{side}-window-{i+1}")
        interval = parse_interval(window, name=f"{side}.window.{wid}")
        key = _key_from_record(window)
        matches_key = key_accepts(key, requested_key)

        recovery_id = first_present(
            window, "recovery_id", "inherited_from_recovery", "inherited_at", "recovery"
        )
        inherited_flag = bool(
            first_present(window, "inherited", "is_inherited", "inherit") or recovery_id is not None
        )

        matching_declarations = [
            d
            for d in declarations
            if d["side"] == side
            and d["window_id"] == wid
            and (recovery_id is None or d["recovery_id"] == str(recovery_id))
        ]
        inheritance_valid = True
        if inherited_flag:
            inheritance_valid = bool(matching_declarations)
            if not inheritance_valid:
                conflict = _conflict(
                    "undeclared_window_inheritance",
                    f"{side} window {wid!r} is marked inherited"
                    + (f" from {recovery_id!r}" if recovery_id is not None else "")
                    + ", but the validated transcript does not declare that inheritance",
                    category="transcript",
                    side=side,
                    window_id=wid,
                    recovery_id=str(recovery_id) if recovery_id is not None else None,
                    sources=[f"window:{wid}", "transcript.recoveries"],
                )
                conflicts.append(conflict)

        usable = matches_key and inheritance_valid and not interval.empty
        parsed.append(
            {
                "id": wid,
                "side": side,
                "interval": interval,
                "key": key,
                "matches_key": matches_key,
                "inherited": inherited_flag or bool(matching_declarations),
                "declared_in": [d["recovery_id"] for d in matching_declarations],
                "inheritance_valid": inheritance_valid,
                "usable": usable,
            }
        )

    return parsed


def _collect_evidence(
    transcript: dict[str, Any], case: dict[str, Any]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    raw = []
    raw.extend(as_list(first_present(transcript, "supplemental_evidence", "late_evidence", "additional_evidence")))
    raw.extend(as_list(first_present(transcript, "time_evidence", "evidence")))
    raw.extend(as_list(first_present(case, "supplemental_evidence", "late_evidence", "time_evidence")))

    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        record = require_mapping(item, f"evidence[{i}]")
        eid = str(first_present(record, "id", "evidence_id", "source") or f"evidence-{i+1}")
        if eid in seen_ids:
            eid = f"{eid}-{i+1}"
        seen_ids.add(eid)
        interval = _extract_time_interval(record, name=f"evidence.{eid}")
        if interval is None:
            raise ApprovalInputError(f"evidence {eid!r} must contain a time interval or timestamp/error")
        subject_value = first_present(record, "subject", "side", "phase", "event", "event_id")
        subject = normalize_subject(subject_value) if subject_value is not None else None
        items.append(
            {
                "id": eid,
                "source": str(first_present(record, "source", "origin") or eid),
                "subject": subject,
                "commitment_id": first_present(record, "commitment_id", "commitment", "promise_id"),
                "key": _key_from_record(record),
                "interval": interval,
                "raw": record,
            }
        )
    return items


def _build_subject_report(
    side: str,
    endpoint: dict[str, Any],
    commitments: dict[str, dict[str, Any]],
    evidence: list[dict[str, Any]],
    requested_key: KeyRef | None,
    windows: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []

    endpoint_interval = _extract_time_interval(endpoint, name=side)
    if endpoint_interval is None:
        observed = first_present(endpoint, "observed_at", "local_time", "timestamp")
        if observed is None:
            raise ApprovalInputError(
                f"{side} requires trusted_interval or observed_at plus clock_error"
            )
        raise ApprovalInputError(f"{side} time interval could not be parsed")

    claims.append(
        {
            "source": f"{side}-clock",
            "interval": endpoint_interval,
            "kind": "clock",
        }
    )

    for cid, commitment in commitments.items():
        c_subject = commitment.get("subject")
        if c_subject is not None and c_subject != side:
            continue
        if commitment.get("interval") is not None:
            claims.append(
                {
                    "source": f"commitment:{cid}",
                    "interval": commitment["interval"],
                    "kind": "commitment",
                    "commitment_id": cid,
                }
            )
        c_key = commitment.get("key")
        if requested_key is not None and c_key is not None and not key_accepts(c_key, requested_key):
            conflicts.append(
                _conflict(
                    "key_commitment_mismatch",
                    f"commitment {cid!r} does not promise the requested key group",
                    category="key",
                    sources=[f"commitment:{cid}"],
                    commitment_id=cid,
                    subject=side,
                )
            )

    for ev in evidence:
        cid = ev.get("commitment_id")
        commitment = None
        if cid is not None:
            commitment = commitments.get(str(cid))
            if commitment is None:
                conflicts.append(
                    _conflict(
                        "unknown_commitment",
                        f"evidence {ev['id']!r} references unknown commitment {cid!r}",
                        category="transcript",
                        sources=[f"evidence:{ev['id']}"],
                        evidence_id=ev["id"],
                        commitment_id=str(cid),
                    )
                )
            else:
                if ev.get("subject") is None:
                    ev["subject"] = commitment.get("subject")
                if commitment.get("subject") is not None and ev.get("subject") is not None:
                    if ev["subject"] != commitment["subject"]:
                        conflicts.append(
                            _conflict(
                                "evidence_subject_mismatch",
                                f"evidence {ev['id']!r} concerns {ev['subject']}, but commitment "
                                f"{cid!r} concerns {commitment['subject']}",
                                category="contradiction",
                                sources=[f"evidence:{ev['id']}", f"commitment:{cid}"],
                                evidence_id=ev["id"],
                                commitment_id=str(cid),
                            )
                        )
                c_key = commitment.get("key")
                e_key = ev.get("key")
                if c_key is not None and e_key is not None and not key_accepts(e_key, c_key):
                    conflicts.append(
                        _conflict(
                            "evidence_key_mismatch",
                            f"late evidence {ev['id']!r} asserts a different key from commitment {cid!r}",
                            category="contradiction",
                            sources=[f"evidence:{ev['id']}", f"commitment:{cid}"],
                            evidence_id=ev["id"],
                            commitment_id=str(cid),
                        )
                    )
                if commitment.get("interval") is not None:
                    if intersect(ev["interval"], commitment["interval"]).empty:
                        conflicts.append(
                            _conflict(
                                "supplemental_time_conflict",
                                f"late time evidence {ev['id']!r} is disjoint from prior commitment {cid!r}",
                                category="evidence",
                                sources=[f"evidence:{ev['id']}", f"commitment:{cid}"],
                                evidence_id=ev["id"],
                                commitment_id=str(cid),
                                evidence_interval=ev["interval"].to_dict(),
                                committed_interval=commitment["interval"].to_dict(),
                            )
                        )

        if ev.get("subject") == side:
            claims.append(
                {
                    "source": f"evidence:{ev['id']}",
                    "interval": ev["interval"],
                    "kind": "evidence",
                    "evidence_id": ev["id"],
                }
            )

    # Pairwise source contradictions.  The explicit commitment/evidence check
    # carries richer provenance; this generic pass also catches clock and
    # independent evidence disagreements.
    for i in range(len(claims)):
        for j in range(i + 1, len(claims)):
            a, b = claims[i], claims[j]
            if intersect(a["interval"], b["interval"]).empty:
                conflicts.append(
                    _conflict(
                        "time_evidence_conflict",
                        f"{side} time evidence sources {a['source']!r} and {b['source']!r} do not overlap",
                        category="evidence",
                        sources=[a["source"], b["source"]],
                        subject=side,
                        intervals=[a["interval"].to_dict(), b["interval"].to_dict()],
                    )
                )

    claim_intervals = [c["interval"] for c in claims]
    try:
        fused = intersect_all(claim_intervals)
    except ValueError:
        fused = Interval.empty_interval()
    consistent = not fused.empty

    source_union = merge_intervals(claim_intervals)
    partial_source = union_minus_intersection(claim_intervals) if not fused.empty else source_union

    usable_windows = [w for w in windows if w["usable"]]
    window_intervals = merge_intervals([w["interval"] for w in usable_windows])
    accepted = merge_intervals(
        [intersect(fused, w["interval"]) for w in usable_windows if not fused.empty]
    )
    accepted = [iv for iv in accepted if not iv.empty]

    has_inheritance_problem = any(
        c.get("side") == side and c.get("code") == "undeclared_window_inheritance"
        for c in conflicts
    )
    if not accepted and not fused.empty and not has_inheritance_problem:
        conflicts.append(
            _conflict(
                "no_accepted_key_window",
                f"{side} has no announced, inheritance-validated window that accepts the key "
                "and intersects the consistent time interval",
                category="constraint",
                side=side,
                sources=[f"{side}.windows", f"{side}-clock"],
            )
        )

    outside_window = subtract_many(fused, window_intervals) if not fused.empty else []
    window_guaranteed = bool(fused and not outside_window)

    ambiguous_periods: list[dict[str, Any]] = []
    for iv in partial_source:
        ambiguous_periods.append(
            {
                "subject": side,
                "interval": iv.to_dict(),
                "reason": "partial_source_coverage",
                "description": "Covered by some but not all time-evidence sources",
            }
        )
    for iv in outside_window:
        ambiguous_periods.append(
            {
                "subject": side,
                "interval": iv.to_dict(),
                "reason": "outside_announced_key_window",
                "description": "Allowed by fused time evidence but not covered by an accepting key window",
            }
        )

    return {
        "subject": side,
        "claims": claims,
        "consistent": consistent,
        "fused_interval": fused,
        "source_union": source_union,
        "partial_source_intervals": partial_source,
        "windows": windows,
        "window_intervals": window_intervals,
        "accepted_intervals": accepted,
        "outside_window_intervals": outside_window,
        "window_guaranteed": window_guaranteed,
        "feasible_intervals": [],
        "delay_excluded_intervals": [],
        "ambiguous_periods": ambiguous_periods,
        "certain_periods": [],
    }


def _find_witness(
    send_interval: Interval, receive_interval: Interval, delay_limit: float | int
) -> dict[str, Any] | None:
    send_shifted = add_intervals(send_interval, Interval(0, delay_limit, True, True))
    receiver_part = intersect(receive_interval, send_shifted)
    if receiver_part.empty:
        return None
    r = sample_point(receiver_part)
    sender_part = intersect(
        send_interval, Interval(r - delay_limit, r, True, True)
    )
    if sender_part.empty:  # pragma: no cover - algebra should prevent this
        return None
    s = sample_point(sender_part)
    return {"send": s, "receive": r, "delay": r - s}


def _delay_report(
    send_report: dict[str, Any],
    receive_report: dict[str, Any],
    delay_limit: float | int,
    conflicts: list[dict[str, Any]],
) -> tuple[list[Interval], list[Interval], dict[str, Any]]:
    s_accepted: list[Interval] = send_report["accepted_intervals"]
    r_accepted: list[Interval] = receive_report["accepted_intervals"]

    s_feasible: list[Interval] = []
    r_feasible: list[Interval] = []
    witness = None

    for s_interval in s_accepted:
        s_plus_delay = add_intervals(s_interval, Interval(0, delay_limit, True, True))
        for r_interval in r_accepted:
            r_part = intersect(r_interval, s_plus_delay)
            if r_part.empty:
                continue
            r_feasible.append(r_part)
            r_minus_delay = add_intervals(r_interval, Interval(-delay_limit, 0, True, True))
            s_part = intersect(s_interval, r_minus_delay)
            if not s_part.empty:
                s_feasible.append(s_part)
            if witness is None:
                witness = _find_witness(s_interval, r_interval, delay_limit)

    s_feasible = merge_intervals(s_feasible)
    r_feasible = merge_intervals(r_feasible)

    if not s_feasible or not r_feasible:
        conflicts.append(
            _conflict(
                "delivery_delay_impossible",
                f"no send/receive pair has 0 <= receive_time-send_time <= {delay_limit}",
                category="constraint",
                sources=["sender.accepted_intervals", "receiver.accepted_intervals", "max_delay"],
                max_delay=delay_limit,
            )
        )

    raw_s: Interval = send_report["fused_interval"]
    raw_r: Interval = receive_report["fused_interval"]
    raw_bounds = None
    guaranteed_nonnegative = False
    guaranteed_within_limit = False
    if raw_s and raw_r and not raw_s.empty and not raw_r.empty:
        raw_min = raw_r.start - raw_s.end
        raw_max = raw_r.end - raw_s.start
        raw_bounds = {"minimum": raw_min, "maximum": raw_max}
        guaranteed_nonnegative = raw_r.start >= raw_s.end
        guaranteed_within_limit = raw_r.end - raw_s.start <= delay_limit

    accepted_bounds = None
    if s_accepted and r_accepted:
        accepted_bounds = {
            "minimum": min(r.start - s.end for s in s_accepted for r in r_accepted),
            "maximum": max(r.end - s.start for s in s_accepted for r in r_accepted),
        }

    report = {
        "max_delay": delay_limit,
        "raw_possible_delay_bounds": raw_bounds,
        "accepted_possible_delay_bounds": accepted_bounds,
        "guaranteed_nonnegative": guaranteed_nonnegative,
        "guaranteed_within_limit": guaranteed_within_limit,
        "guaranteed": guaranteed_nonnegative and guaranteed_within_limit,
        "witness": witness,
    }
    return s_feasible, r_feasible, report


def _interval_dicts(intervals: list[Interval]) -> list[dict[str, Any]]:
    return [iv.to_dict() for iv in intervals]


def _serialize_subject(report: dict[str, Any]) -> dict[str, Any]:
    certain = [
        {"subject": report["subject"], "interval": iv.to_dict()}
        for iv in report["certain_periods"]
    ]
    return {
        "subject": report["subject"],
        "consistent": report["consistent"],
        "fused_interval": report["fused_interval"].to_dict() if report["fused_interval"] else None,
        "source_union": _interval_dicts(report["source_union"]),
        "partial_source_intervals": _interval_dicts(report["partial_source_intervals"]),
        "window_intervals": _interval_dicts(report["window_intervals"]),
        "accepted_intervals": _interval_dicts(report["accepted_intervals"]),
        "feasible_intervals": _interval_dicts(report["feasible_intervals"]),
        "delay_excluded_intervals": _interval_dicts(report["delay_excluded_intervals"]),
        "outside_window_intervals": _interval_dicts(report["outside_window_intervals"]),
        "window_guaranteed": report["window_guaranteed"],
        "certain_periods": certain,
        "ambiguous_periods": report["ambiguous_periods"],
        "windows": [
            {
                "id": w["id"],
                "interval": w["interval"].to_dict(),
                "matches_key": w["matches_key"],
                "usable": w["usable"],
                "inherited": w["inherited"],
                "declared_in": w["declared_in"],
                "inheritance_valid": w["inheritance_valid"],
                "key": w["key"].to_dict() if w["key"] else None,
            }
            for w in report["windows"]
        ],
    }


def _evaluate_with_key(
    case: dict[str, Any],
    requested_key: KeyRef | None,
    initial_conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    max_delay_value = first_present(
        case, "max_delay", "maximum_delay", "max_delivery_delay", "delivery_delay_limit"
    )
    if max_delay_value is None:
        raise ApprovalInputError("max_delay is required")
    max_delay = parse_number(max_delay_value, "max_delay", minimum=0)

    transcript = optional_mapping(case.get("transcript"), "transcript")
    if transcript.get("validated") is False:
        raise TranscriptValidationError(
            "transcript.validated is false; this module only accepts an already validated transcript"
        )

    conflicts: list[dict[str, Any]] = list(initial_conflicts or [])
    commitments = _index_commitments(transcript, conflicts)
    declarations, recovery_reports = _audit_recoveries(
        transcript, commitments, requested_key, conflicts
    )
    evidence = _collect_evidence(transcript, case)

    sender_case = optional_mapping(
        first_present(case, "sender", "sender_endpoint"), "sender"
    )
    receiver_case = optional_mapping(
        first_present(case, "receiver", "receiver_endpoint"), "receiver"
    )
    if not sender_case:
        # Allow flat sender_* convenience fields.
        sender_case = {
            k[len("sender_") :]: v
            for k, v in case.items()
            if k.startswith("sender_") and k != "sender_windows"
        }
        if "sender_windows" in case:
            sender_case["windows"] = case["sender_windows"]
        if "sender_observed_at" in case:
            sender_case["observed_at"] = case["sender_observed_at"]
        if "sender_clock_error" in case:
            sender_case["clock_error"] = case["sender_clock_error"]
    if not receiver_case:
        receiver_case = {
            k[len("receiver_") :]: v
            for k, v in case.items()
            if k.startswith("receiver_") and k != "receiver_windows"
        }
        if "receiver_windows" in case:
            receiver_case["windows"] = case["receiver_windows"]

    sender_windows = _parse_endpoint_windows(
        "send", sender_case, requested_key, declarations, conflicts
    )
    receiver_windows = _parse_endpoint_windows(
        "receive", receiver_case, requested_key, declarations, conflicts
    )

    send_report = _build_subject_report(
        "send", sender_case, commitments, evidence, requested_key, sender_windows, conflicts
    )
    receive_report = _build_subject_report(
        "receive", receiver_case, commitments, evidence, requested_key, receiver_windows, conflicts
    )

    s_feasible, r_feasible, delay_report = _delay_report(
        send_report, receive_report, max_delay, conflicts
    )
    send_report["feasible_intervals"] = s_feasible
    receive_report["feasible_intervals"] = r_feasible

    send_accepted_union = merge_intervals(send_report["accepted_intervals"])
    receive_accepted_union = merge_intervals(receive_report["accepted_intervals"])
    send_report["delay_excluded_intervals"] = [
        piece
        for accepted in send_accepted_union
        for piece in subtract_many(accepted, s_feasible)
    ]
    receive_report["delay_excluded_intervals"] = [
        piece
        for accepted in receive_accepted_union
        for piece in subtract_many(accepted, r_feasible)
    ]

    send_report["certain_periods"] = s_feasible
    receive_report["certain_periods"] = r_feasible

    serialized_sender = _serialize_subject(send_report)
    serialized_receiver = _serialize_subject(receive_report)

    relation_ambiguous_periods: list[dict[str, Any]] = []
    if delay_report["witness"] is not None and not delay_report["guaranteed"]:
        raw_s = send_report["fused_interval"]
        raw_r = receive_report["fused_interval"]
        if raw_s and raw_r and not raw_s.empty and not raw_r.empty:
            for iv in merge_intervals([raw_s, raw_r]):
                relation_ambiguous_periods.append(
                    {
                        "subject": "transfer",
                        "interval": iv.to_dict(),
                        "reason": "delay_relation_not_guaranteed",
                        "description": (
                            "The interval participates in both valid and potentially "
                            "invalid delay pairings"
                        ),
                    }
                )

    certain_periods = serialized_sender["certain_periods"] + serialized_receiver["certain_periods"]
    ambiguous_periods = (
        serialized_sender["ambiguous_periods"]
        + serialized_receiver["ambiguous_periods"]
        + relation_ambiguous_periods
    )

    source_contradictions = [
        c for c in conflicts if c.get("category") != "constraint"
    ]

    has_contradiction = bool(source_contradictions)
    feasible = delay_report["witness"] is not None and not any(
        c.get("code") == "delivery_delay_impossible" for c in conflicts
    )
    robust = (
        not conflicts
        and feasible
        and send_report["window_guaranteed"]
        and receive_report["window_guaranteed"]
        and delay_report["guaranteed"]
    )

    if has_contradiction:
        status = "contradictory"
        decision = "deny"
    elif not feasible:
        status = "infeasible"
        decision = "deny"
    elif robust:
        status = "certain"
        decision = "approve"
    else:
        status = "ambiguous"
        decision = "manual_review"

    guaranteed_periods: list[dict[str, Any]] = []
    if robust:
        guaranteed_periods = [
            {"subject": "send", "interval": serialized_sender["fused_interval"]},
            {"subject": "receive", "interval": serialized_receiver["fused_interval"]},
        ]

    result = {
        "status": status,
        "decision": decision,
        # Security-safe boolean: ambiguity is not auto-approved.  Use
        # ``feasible``/``can_hold`` for the existential interval result.
        "approved": robust,
        "auto_approved": robust,
        "allowed": robust,
        "ok": robust,
        "can_hold": feasible and not has_contradiction,
        "feasible": feasible,
        "certain": robust,
        "requires_review": status == "ambiguous",
        "key": requested_key.to_dict() if requested_key is not None else None,
        "max_delay": max_delay,
        "delay": delay_report,
        "sender": serialized_sender,
        "receiver": serialized_receiver,
        "recoveries": recovery_reports,
        "certain_periods": certain_periods,
        "guaranteed_periods": guaranteed_periods,
        "robust_periods": guaranteed_periods,
        "ambiguous_periods": ambiguous_periods,
        "conflicts": conflicts,
        "source_contradictions": source_contradictions,
        # Flattened interval-only aliases:
        "valid_intervals": [item["interval"] for item in certain_periods],
        "available_intervals": [item["interval"] for item in certain_periods],
        "definite_usable_periods": certain_periods,
        "conflicting_evidence": source_contradictions,
        "contradictions": source_contradictions,
        # Common aliases requested by callers/tests:
        "certain_intervals": certain_periods,
        "ambiguous_intervals": ambiguous_periods,
        "contradictory_evidence": source_contradictions,
    }
    return result


def _collect_declared_key_groups(
    case: dict[str, Any],
) -> tuple[set[Hashable], set[Hashable], set[Hashable]]:
    commitment_groups: set[Hashable] = set()
    transcript = case.get("transcript")
    if isinstance(transcript, dict):
        for c in as_list(transcript.get("commitments")):
            if isinstance(c, dict):
                key = _key_from_record(c)
                if key is not None:
                    commitment_groups.update(key.groups)
                    if key.group is not None:
                        commitment_groups.add(key.group)

    endpoint_groups: dict[str, set[Hashable]] = {}
    for side in ("sender", "receiver"):
        endpoint = optional_mapping(case.get(side), side)
        groups: set[Hashable] = set()
        for w in as_list(first_present(endpoint, "windows", "entry_windows", "intake_windows")):
            if isinstance(w, dict):
                key = _key_from_record(w)
                if key is not None:
                    groups.update(key.groups)
                    if key.group is not None:
                        groups.add(key.group)
        endpoint_groups[side] = groups
    return commitment_groups, endpoint_groups["sender"], endpoint_groups["receiver"]


def _resolve_requested_key(
    case: dict[str, Any],
) -> tuple[KeyRef | None, dict[str, Any] | None]:
    explicit = (
        parse_key_ref(case.get("key") or case.get("key_ref") or case.get("key_commitment"))
        or parse_key_ref(case)
    )
    if explicit is not None and not explicit.is_empty:
        return explicit, None

    commitment_groups, sender_groups, receiver_groups = _collect_declared_key_groups(case)
    common = set(sender_groups) & set(receiver_groups)
    if commitment_groups:
        common = common & commitment_groups if common else set(commitment_groups)
        if sender_groups and receiver_groups:
            common &= sender_groups & receiver_groups

    if len(common) == 1:
        return KeyRef(group=next(iter(common))), None

    if len(common) > 1:
        error = _conflict(
            "ambiguous_key_group",
            "more than one key group is common to the transcript and both endpoints; "
            "the promised key_group must be stated explicitly",
            category="key",
            common_key_groups=sorted(common, key=str),
            sources=["transcript.commitments", "sender.windows", "receiver.windows"],
        )
        return KeyRef(group="__ambiguous_key_group__"), error

    if sender_groups or receiver_groups or commitment_groups:
        error = _conflict(
            "no_common_key_group",
            "sender and receiver do not advertise a common promised key group",
            category="key",
            sender_key_groups=sorted(sender_groups, key=str),
            receiver_key_groups=sorted(receiver_groups, key=str),
            commitment_key_groups=sorted(commitment_groups, key=str),
            sources=["sender.windows", "receiver.windows", "transcript.commitments"],
        )
        return KeyRef(group="__no_common_key_group__"), error

    return None, None


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one offline transfer approval case.

    The input is a JSON-compatible mapping; the return value is also
    JSON-compatible.
    """

    require_mapping(case, "case")
    requested_key, key_error = _resolve_requested_key(case)
    result = _evaluate_with_key(case, requested_key, [key_error] if key_error else None)
    if key_error is not None:
        # The internal sentinel selector must not leak into the public report.
        result["key"] = None
    return result


def approve_transfer(
    transcript: dict[str, Any] | None = None,
    *,
    sender: dict[str, Any] | None = None,
    receiver: dict[str, Any] | None = None,
    max_delay: float | int | None = None,
    delay_limit: float | int | None = None,
    key_group: str | None = None,
    key_ids: list[Any] | tuple[Any, ...] | set[Any] | None = None,
    sender_observed_at: float | int | None = None,
    receiver_observed_at: float | int | None = None,
    sender_clock_error: Any = None,
    receiver_clock_error: Any = None,
    sender_interval: Any = None,
    receiver_interval: Any = None,
    sender_windows: list[dict[str, Any]] | None = None,
    receiver_windows: list[dict[str, Any]] | None = None,
    windows: dict[str, Any] | None = None,
    supplemental_evidence: list[dict[str, Any]] | None = None,
    recoveries: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Convenience wrapper with explicit keyword arguments."""

    case: dict[str, Any] = dict(extra)
    if transcript is not None:
        case["transcript"] = transcript
    if max_delay is not None:
        case["max_delay"] = max_delay
    if delay_limit is not None:
        case["max_delay"] = delay_limit
    if key_group is not None:
        case["key_group"] = key_group
    if key_ids is not None:
        case["key_ids"] = list(key_ids)
    if supplemental_evidence is not None:
        case["supplemental_evidence"] = supplemental_evidence
    if recoveries is not None:
        case.setdefault("transcript", {})["recoveries"] = recoveries

    sender_case = dict(sender or {})
    if sender_observed_at is not None:
        sender_case["observed_at"] = sender_observed_at
    if sender_clock_error is not None:
        sender_case["clock_error"] = sender_clock_error
    if sender_interval is not None:
        sender_case["trusted_interval"] = sender_interval
    if sender_windows is not None:
        sender_case["windows"] = sender_windows
    if windows and "sender" in windows:
        sender_case.setdefault("windows", windows["sender"])
    if sender_case:
        case["sender"] = sender_case

    receiver_case = dict(receiver or {})
    if receiver_observed_at is not None:
        receiver_case["observed_at"] = receiver_observed_at
    if receiver_clock_error is not None:
        receiver_case["clock_error"] = receiver_clock_error
    if receiver_interval is not None:
        receiver_case["trusted_interval"] = receiver_interval
    if receiver_windows is not None:
        receiver_case["windows"] = receiver_windows
    if windows and "receiver" in windows:
        receiver_case.setdefault("windows", windows["receiver"])
    if receiver_case:
        case["receiver"] = receiver_case

    return evaluate_case(case)


# Descriptive aliases for callers that prefer task-language names.
evaluate_transfer = evaluate_case
assess_transfer = evaluate_case
evaluate = evaluate_case
solve = evaluate_case
check_transfer = approve_transfer
check_approval = approve_transfer


class OfflineTransferApproval:
    """Small class-based facade for callers that prefer an object API."""

    evaluate_case = staticmethod(evaluate_case)
    evaluate_transfer = staticmethod(evaluate_case)
    approve_transfer = staticmethod(approve_transfer)
    check_transfer = staticmethod(approve_transfer)
