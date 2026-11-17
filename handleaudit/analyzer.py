"""Core offline analyzer: which sensitive handles did a child process inherit?

The analyzer is deterministic and side-effect free.  It replays an ordered
event stream (process generations, handle open/close, cross-process
duplication, inheritable-flag updates, launch begin/commit), joins it with
trusted collector snapshots and a per-child allow policy, and produces a
report that distinguishes *confirmed* inheritance from *unverified* results
when evidence is missing or gapped.

Design rules (see README for the full rationale):

* Handle numeric values are reusable.  Identity is the tuple
  ``(owner_pid, owner_generation, open_generation)``; every ``handle_open``
  starts a new open generation for that value in that process.
* A ``handle_duplicate`` creates a fresh handle instance in the target
  process whose access mask is the *granted* mask carried by the event, never
  the source handle's mask.
* A trusted snapshot attached to a launch is the authoritative record of
  what the child holds at commit.  With no snapshot, only inheritance that
  is independently corroborated (a duplicate event targeting the child) can
  be confirmed; everything else stays ``unverified``.
* Sequence gaps inside the launch window forbid rejecting or confirming
  beyond the evidence: uncorroborated predictions are marked ``unverified``
  and the gap is reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Access mask decoding (synthetic audit vocabulary; bit table is data-driven
# so unknown bits are preserved instead of being silently dropped).
# ---------------------------------------------------------------------------

KNOWN_ACCESS_BITS: dict[int, str] = {
    0x0001: "READ",
    0x0002: "WRITE",
    0x0004: "EXECUTE",
    0x0008: "DELETE",
    0x0010: "DUPLICATE",
    0x0020: "SET_INFORMATION",
    0x0040: "READ_CONTROL",
    0x8000: "GENERIC_ALL",
}


def decode_access_mask(mask: int) -> list[str]:
    """Decode an integer access mask into named rights plus a hex residue."""
    if not isinstance(mask, int) or mask < 0:
        raise ValueError(f"access mask must be a non-negative int, got {mask!r}")
    names: list[str] = []
    residue = mask
    for bit in sorted(KNOWN_ACCESS_BITS):
        if mask & bit:
            names.append(KNOWN_ACCESS_BITS[bit])
            residue &= ~bit
    if residue:
        names.append(f"UNKNOWN_0x{residue:X}")
    return names


# ---------------------------------------------------------------------------
# Internal replay state
# ---------------------------------------------------------------------------


@dataclass
class HandleInstance:
    """One concrete incarnation of a handle value in one process."""

    instance_id: str
    pid: int
    process_generation: int
    handle_value: int
    open_generation: int
    object_id: str
    object_name: str
    object_category: str
    granted_access: int
    inheritable: bool
    opened_seq: int
    closed_seq: int | None = None
    # Provenance chain, oldest ancestor first.  Each node is a compact dict.
    chain: list[dict[str, Any]] = field(default_factory=list)

    def is_open_at(self, seq: int) -> bool:
        return self.opened_seq <= seq and (
            self.closed_seq is None or self.closed_seq > seq
        )

    def inheritable_at(self, seq: int, flag_history: list[tuple[int, bool]]) -> bool:
        """Flag value at a sequence number, replaying flag updates in order."""
        value = self.inheritable
        for change_seq, new_value in flag_history:
            if change_seq <= seq:
                value = new_value
            else:
                break
        return value

    def describe(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "process_generation": self.process_generation,
            "handle": f"0x{self.handle_value:X}",
            "open_generation": self.open_generation,
            "object_id": self.object_id,
            "object_name": self.object_name,
            "object_category": self.object_category,
            "granted_access": f"0x{self.granted_access:X}",
            "granted_rights": decode_access_mask(self.granted_access),
        }


@dataclass
class _Replay:
    events: list[dict[str, Any]]
    snapshots_by_seq: dict[int, dict[str, Any]]
    launches: list[dict[str, Any]]
    policy: dict[str, Any]
    # pid -> max process generation observed
    process_gens: dict[int, int] = field(default_factory=dict)
    # (pid, generation) -> {handle_value: [HandleInstance, ...]}
    handles: dict[tuple[int, int], dict[int, list[HandleInstance]]] = field(
        default_factory=dict
    )
    # instance_id -> flag history [(seq, new_value), ...]
    flag_history: dict[str, list[tuple[int, bool]]] = field(default_factory=dict)
    open_counters: dict[tuple[int, int, int], int] = field(default_factory=dict)
    # Sequence numbers occupied by trusted snapshots: part of the collector
    # stream, so the gap heuristic must not treat them as missing events.
    occupied_seqs: set[int] = field(default_factory=set)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    launch_reports: list[dict[str, Any]] = field(default_factory=list)

    def anomaly(self, kind: str, seq: Any, detail: str) -> None:
        self.anomalies.append({"kind": kind, "seq": seq, "detail": detail})

    def resolve(
        self, pid: int, generation: int, value: int, seq: int
    ) -> HandleInstance | None:
        """Resolve (pid, generation, handle value) to the instance open at seq."""
        instances = self.handles.get((pid, generation), {}).get(value, [])
        alive = [h for h in instances if h.is_open_at(seq)]
        if not alive:
            return None
        if len(alive) > 1:  # pragma: no cover - defensive, cannot occur normally
            self.anomaly(
                "ambiguous_handle",
                seq,
                f"multiple open instances for pid={pid} gen={generation} "
                f"handle=0x{value:X}; using latest",
            )
            alive.sort(key=lambda h: h.opened_seq)
            return alive[-1]
        return alive[0]


# ---------------------------------------------------------------------------
# Replay steps
# ---------------------------------------------------------------------------


def _as_int(value: Any, field_name: str, seq: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _replay_events(state: _Replay) -> None:
    previous_seq: int | None = None
    for event in state.events:
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            state.anomaly("malformed_event", seq, "event missing integer 'seq'")
            continue
        if previous_seq is not None and seq > previous_seq + 1:
            missing = [
                s
                for s in range(previous_seq + 1, seq)
                if s not in state.occupied_seqs
            ]
            if missing:
                state.gaps.append(
                    {
                        "after_seq": previous_seq,
                        "before_seq": seq,
                        "missing_count": len(missing),
                        "missing_seqs": missing,
                        "window": "global",
                    }
                )
        previous_seq = seq
        kind = event.get("type")
        handler = _EVENT_HANDLERS.get(kind)
        if handler is None:
            state.anomaly("unknown_event_type", seq, f"type={kind!r}")
            continue
        handler(state, event, seq)


def _on_process_generation(state: _Replay, event: dict[str, Any], seq: int) -> None:
    pid = event.get("pid")
    generation = event.get("generation")
    if not isinstance(pid, int) or not isinstance(generation, int):
        state.anomaly("malformed_event", seq, "process_generation needs pid/generation")
        return
    prior = state.process_gens.get(pid)
    if prior is not None and generation <= prior:
        state.anomaly(
            "stale_process_generation",
            seq,
            f"pid={pid} generation={generation} is not newer than {prior}",
        )
    state.process_gens[pid] = max(generation, prior if prior is not None else 0)
    state.handles.setdefault((pid, generation), {})


def _on_handle_open(state: _Replay, event: dict[str, Any], seq: int) -> None:
    pid = event.get("pid")
    generation = event.get("process_generation")
    value = event.get("handle_value")
    for name, val in (
        ("pid", pid),
        ("process_generation", generation),
        ("handle_value", value),
    ):
        if not isinstance(val, int) or isinstance(val, bool):
            state.anomaly("malformed_event", seq, f"handle_open needs integer {name}")
            return
    key = (pid, generation, value)
    open_generation = state.open_counters.get(key, 0) + 1
    state.open_counters[key] = open_generation

    if (pid, generation) not in state.process_gens and (
        pid,
        generation,
    ) not in state.handles:
        state.anomaly(
            "unknown_process",
            seq,
            f"handle opened in unregistered pid={pid} generation={generation}",
        )
    previous = state.handles.get((pid, generation), {}).get(value, [])
    if previous and previous[-1].closed_seq is None:
        state.anomaly(
            "handle_value_collision",
            seq,
            f"pid={pid} gen={generation} handle=0x{value:X} reopened before close",
        )

    access = event.get("granted_access", 0)
    if not isinstance(access, int) or isinstance(access, bool) or access < 0:
        state.anomaly("malformed_event", seq, "invalid granted_access; using 0")
        access = 0

    instance = HandleInstance(
        instance_id=f"h_{pid}_{generation}_{value:X}_o{open_generation}",
        pid=pid,
        process_generation=generation,
        handle_value=value,
        open_generation=open_generation,
        object_id=str(event.get("object_id", f"obj-0x{value:X}")),
        object_name=str(event.get("object_name", "unknown-object")),
        object_category=str(event.get("object_category", "unknown")),
        granted_access=access,
        inheritable=bool(event.get("inheritable", False)),
        opened_seq=seq,
    )
    state.handles.setdefault((pid, generation), {}).setdefault(value, []).append(
        instance
    )
    state.flag_history[instance.instance_id] = []


def _on_handle_close(state: _Replay, event: dict[str, Any], seq: int) -> None:
    pid = event.get("pid")
    generation = event.get("process_generation")
    value = event.get("handle_value")
    if not all(isinstance(v, int) for v in (pid, generation, value)):
        state.anomaly("malformed_event", seq, "handle_close needs integer fields")
        return
    instance = state.resolve(pid, generation, value, seq)
    if instance is None:
        state.anomaly(
            "unresolvable_handle",
            seq,
            f"close of unknown/open handle pid={pid} gen={generation} "
            f"handle=0x{value:X}",
        )
        return
    instance.closed_seq = seq


def _on_flag_update(state: _Replay, event: dict[str, Any], seq: int) -> None:
    pid = event.get("pid")
    generation = event.get("process_generation")
    value = event.get("handle_value")
    if not all(isinstance(v, int) for v in (pid, generation, value)):
        state.anomaly("malformed_event", seq, "flag_update needs integer fields")
        return
    instance = state.resolve(pid, generation, value, seq)
    if instance is None:
        state.anomaly(
            "unresolvable_handle",
            seq,
            f"flag update on unknown/open handle pid={pid} gen={generation} "
            f"handle=0x{value:X}",
        )
        return
    state.flag_history[instance.instance_id].append(
        (seq, bool(event.get("inheritable", False)))
    )


def _on_duplicate(state: _Replay, event: dict[str, Any], seq: int) -> None:
    src_pid = event.get("source_pid")
    src_gen = event.get("source_process_generation")
    src_value = event.get("source_handle_value")
    dst_pid = event.get("target_pid")
    dst_gen = event.get("target_process_generation")
    dst_value = event.get("target_handle_value")
    ints = (src_pid, src_gen, src_value, dst_pid, dst_gen, dst_value)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in ints):
        state.anomaly("malformed_event", seq, "handle_duplicate needs integer fields")
        return
    source = state.resolve(src_pid, src_gen, src_value, seq)
    if source is None:
        state.anomaly(
            "unresolvable_handle",
            seq,
            f"duplicate source unknown pid={src_pid} gen={src_gen} "
            f"handle=0x{src_value:X}",
        )
        return
    granted = event.get("granted_access")
    if not isinstance(granted, int) or isinstance(granted, bool) or granted < 0:
        state.anomaly(
            "malformed_event",
            seq,
            "duplicate missing non-negative granted_access; event is required "
            "to carry the actually granted mask",
        )
        return

    target_key = (dst_pid, dst_gen)
    counter_key = (dst_pid, dst_gen, dst_value)
    open_generation = state.open_counters.get(counter_key, 0) + 1
    state.open_counters[counter_key] = open_generation
    prior = state.handles.get(target_key, {}).get(dst_value, [])
    if prior and prior[-1].closed_seq is None:
        state.anomaly(
            "handle_value_collision",
            seq,
            f"duplicate target handle 0x{dst_value:X} already open in "
            f"pid={dst_pid} gen={dst_gen}",
        )

    chain = list(source.chain)
    chain.append(
        {
            "via": "duplicate",
            "seq": seq,
            "source_instance": source.instance_id,
            "source_pid": src_pid,
            "source_handle": f"0x{src_value:X}",
            "source_access": f"0x{source.granted_access:X}",
            "granted_access": f"0x{granted:X}",
        }
    )
    instance = HandleInstance(
        instance_id=f"h_{dst_pid}_{dst_gen}_{dst_value:X}_o{open_generation}",
        pid=dst_pid,
        process_generation=dst_gen,
        handle_value=dst_value,
        open_generation=open_generation,
        object_id=source.object_id,
        object_name=source.object_name,
        object_category=source.object_category,
        granted_access=granted,  # event's granted value, never the source's
        inheritable=bool(event.get("inheritable", False)),
        opened_seq=seq,
        chain=chain,
    )
    state.handles.setdefault(target_key, {}).setdefault(dst_value, []).append(instance)
    state.flag_history[instance.instance_id] = []


def _on_launch_begin(state: _Replay, event: dict[str, Any], seq: int) -> None:
    for name in ("parent_pid", "parent_generation", "child_pid", "child_generation"):
        if not isinstance(event.get(name), int):
            state.anomaly("malformed_event", seq, f"launch_begin needs {name}")
            return
    # The child generation exists from this point; duplicate events can
    # legitimately target it before launch_commit.
    state.process_gens.setdefault(
        event["child_pid"], event["child_generation"]
    )
    state.handles.setdefault(
        (event["child_pid"], event["child_generation"]), {}
    )


def _on_launch_commit(state: _Replay, event: dict[str, Any], seq: int) -> None:
    # Analysis happens in a second pass once every event (including events
    # ordered after a commit) has been replayed.
    return


_EVENT_HANDLERS = {
    "process_generation": _on_process_generation,
    "handle_open": _on_handle_open,
    "handle_close": _on_handle_close,
    "handle_flag_update": _on_flag_update,
    "handle_duplicate": _on_duplicate,
    "launch_begin": _on_launch_begin,
    "launch_commit": _on_launch_commit,
}


# ---------------------------------------------------------------------------
# Launch analysis
# ---------------------------------------------------------------------------


def _window_gap(state: _Replay, begin_seq: int, commit_seq: int) -> dict[str, Any] | None:
    for gap in state.gaps:
        if gap["after_seq"] >= begin_seq and gap["before_seq"] <= commit_seq:
            return gap
    return None


def _belongs_to_other_window(
    state: _Replay, instance: HandleInstance, launch: dict[str, Any]
) -> bool:
    """True if the instance was opened inside a sibling launch window.

    A handle opened between another launch's begin and commit on the same
    parent process belongs to that sibling's analysis, not to this one.
    """
    for other in state.launches:
        if other["launch_id"] == launch["launch_id"]:
            continue
        if (
            other["parent_pid"] == launch["parent_pid"]
            and other["parent_generation"] == launch["parent_generation"]
            and other["begin_seq"] <= instance.opened_seq <= other["commit_seq"]
        ):
            return True
    return False


def _policy_for_child(state: _Replay, child_pid: int) -> dict[str, Any]:
    per_child = state.policy.get("per_child", {})
    entry = per_child.get(str(child_pid)) or per_child.get(child_pid)
    if entry is not None:
        return entry
    return state.policy.get("default", {"allowed_categories": [], "allowed_rights": []})


def _evaluate_policy(
    policy: dict[str, Any], category: str, granted_access: int
) -> dict[str, Any]:
    allowed_categories = set(policy.get("allowed_categories", []))
    allowed_rights = set(policy.get("allowed_rights", []))
    extra_rights = sorted(
        right
        for right in decode_access_mask(granted_access)
        if right not in allowed_rights
    )
    category_allowed = category in allowed_categories
    return {
        "category_allowed": category_allowed,
        "allowed_categories": sorted(allowed_categories),
        "allowed_rights": sorted(allowed_rights),
        "extra_rights": extra_rights,
        "compliant": category_allowed and not extra_rights,
    }


def _parent_candidates(
    state: _Replay,
    launch: dict[str, Any],
    commit_seq: int,
    eval_seq: int | None = None,
) -> list[HandleInstance]:
    """Parent handles predicted to reach the child.

    ``eval_seq`` is the sequence number at which liveness and inheritable
    flags are evaluated; it defaults to the commit sequence.
    """
    eval_seq = commit_seq if eval_seq is None else eval_seq
    parent_key = (launch["parent_pid"], launch["parent_generation"])
    candidates: list[HandleInstance] = []
    for instances in state.handles.get(parent_key, {}).values():
        for instance in instances:
            if not instance.is_open_at(eval_seq):
                continue
            if launch.get("inheritance_mode") == "explicit_list":
                if instance.handle_value in launch.get("explicit_handles", []):
                    candidates.append(instance)
            else:
                if instance.inheritable_at(eval_seq, state.flag_history.get(
                    instance.instance_id, []
                )):
                    candidates.append(instance)
    candidates.sort(key=lambda h: (h.handle_value, h.instance_id))
    return candidates


def _build_chain(
    state: _Replay,
    parent_instance: HandleInstance,
    commit_seq: int,
    mode: str,
) -> list[dict[str, Any]]:
    chain = list(parent_instance.chain)
    chain.append(
        {
            "via": f"inheritance:{mode}",
            "seq": commit_seq,
            "source_instance": parent_instance.instance_id,
            "source_pid": parent_instance.pid,
            "source_handle": f"0x{parent_instance.handle_value:X}",
        }
    )
    return chain


def _analyze_launch(state: _Replay, launch: dict[str, Any]) -> dict[str, Any]:
    begin_seq = launch["begin_seq"]
    commit_seq = launch["commit_seq"]
    child_pid = launch["child_pid"]
    child_gen = launch["child_generation"]
    mode = launch.get("inheritance_mode", "default")
    snapshot_seq = launch.get("snapshot_seq")
    snapshot = (
        state.snapshots_by_seq.get(snapshot_seq)
        if isinstance(snapshot_seq, int)
        else None
    )
    gap = _window_gap(state, begin_seq, commit_seq)
    complete = snapshot is not None and gap is None

    policy = _policy_for_child(state, child_pid)
    inherited: list[dict[str, Any]] = []
    seen_values: set[int] = set()

    if snapshot is not None:
        if not (begin_seq <= snapshot_seq <= commit_seq):
            state.anomaly(
                "snapshot_outside_window",
                snapshot_seq,
                f"launch={launch['launch_id']} snapshot seq {snapshot_seq} not in "
                f"[{begin_seq}, {commit_seq}]",
            )
        # Instances already held by the child at snapshot time, keyed by
        # object identity.  These are usually cross-process duplicates whose
        # child handle value differs from the parent's.
        child_at_snapshot: dict[str, HandleInstance] = {}
        for instances in state.handles.get((child_pid, child_gen), {}).values():
            for instance in instances:
                if (
                    instance.opened_seq <= snapshot_seq
                    and (
                        instance.closed_seq is None
                        or instance.closed_seq > snapshot_seq
                    )
                ):
                    child_at_snapshot.setdefault(instance.object_id, instance)
        for entry in sorted(snapshot.get("handles", []), key=lambda e: e["handle_value"]):
            value = entry["handle_value"]
            parent = state.resolve(
                launch["parent_pid"], launch["parent_generation"], value, commit_seq
            )
            record: dict[str, Any] = {
                "child_handle": f"0x{value:X}",
                "child_pid": child_pid,
                "child_generation": child_gen,
                "object_id": entry.get("object_id"),
                "object_name": entry.get("object_name"),
                "object_category": entry.get("object_category"),
                "granted_access": f"0x{entry['granted_access']:X}",
                "granted_rights": decode_access_mask(entry["granted_access"]),
                "evidence": "snapshot",
                "status": "confirmed",
                "snapshot_seq": snapshot_seq,
                "source_chain": [],
            }
            child_held = child_at_snapshot.get(entry["object_id"])
            if parent is not None:
                record["source_instance"] = parent.instance_id
                record["source_chain"] = _build_chain(state, parent, commit_seq, mode)
                # Cross-check the trusted snapshot against replay state.
                if parent.object_id != record["object_id"]:
                    state.anomaly(
                        "snapshot_object_mismatch",
                        snapshot_seq,
                        f"handle 0x{value:X}: snapshot object {record['object_id']} "
                        f"!= replayed {parent.object_id}",
                    )
            elif child_held is not None and child_held.chain:
                # No same-valued parent handle: trace the child-side instance
                # (e.g. a handle_duplicate under a new value).  The chain
                # preserves the event-granted mask rather than the source mask.
                record["source_instance"] = child_held.instance_id
                record["source_chain"] = list(child_held.chain)
                record["evidence"] = "snapshot+duplicate_chain"
            else:
                record["provenance_note"] = (
                    "snapshot confirms the child holds this handle but no "
                    "matching parent handle instance is replayable; provenance "
                    "is incomplete"
                )
            record["policy"] = _evaluate_policy(
                policy, record["object_category"], entry["granted_access"]
            )
            inherited.append(record)
            seen_values.add(value)

        # Every parent handle open at the snapshot instant is a possible
        # inheritance candidate.  Handles the trusted snapshot does not list
        # are rejected (or held back as unverified when the window is gapped),
        # which also covers non-inheritable handles in default mode and
        # handles whose flag flipped inside the window.  Instances that belong
        # to a different launch window are excluded.
        parent_key = (launch["parent_pid"], launch["parent_generation"])
        all_open_parents: list[HandleInstance] = []
        for instances in state.handles.get(parent_key, {}).values():
            for instance in instances:
                if not instance.is_open_at(snapshot_seq):
                    continue
                if _belongs_to_other_window(state, instance, launch):
                    continue
                all_open_parents.append(instance)
        for parent in sorted(all_open_parents, key=lambda h: (h.handle_value, h.instance_id)):
            if parent.handle_value in seen_values:
                continue
            if launch.get("inheritance_mode") == "explicit_list":
                mode_predicts = parent.handle_value in launch.get(
                    "explicit_handles", []
                )
                reason = "replay_prediction_rejected_by_snapshot"
            else:
                mode_predicts = parent.inheritable_at(
                    snapshot_seq, state.flag_history.get(parent.instance_id, [])
                )
                reason = (
                    "replay_prediction_rejected_by_snapshot"
                    if mode_predicts
                    else "non_inheritable_handle"
                )
            record = {
                "child_handle": f"0x{parent.handle_value:X}",
                "child_pid": child_pid,
                "child_generation": child_gen,
                "object_id": parent.object_id,
                "object_name": parent.object_name,
                "object_category": parent.object_category,
                "granted_access": f"0x{parent.granted_access:X}",
                "granted_rights": decode_access_mask(parent.granted_access),
                "source_instance": parent.instance_id,
                "source_chain": _build_chain(state, parent, commit_seq, mode),
                "snapshot_seq": snapshot_seq,
                "policy": _evaluate_policy(
                    policy, parent.object_category, parent.granted_access
                ),
            }
            if gap is None:
                record["evidence"] = reason
                record["status"] = "not_inherited"
            elif not mode_predicts:
                # The inheritance mode itself excludes this handle at the
                # snapshot instant; an unrelated gap cannot promote it.
                record["evidence"] = reason
                record["status"] = "not_inherited"
            else:
                record["evidence"] = "replay_prediction_gapped_window"
                record["status"] = "unverified"
                record["provenance_note"] = (
                    "sequence gap inside the launch window; the snapshot is the "
                    "only complete authority and this prediction cannot be ruled out"
                )
            inherited.append(record)
    else:
        # No trusted snapshot: confirm only what is independently corroborated
        # by a duplicate event targeting this child generation; never guess.
        child_instances = [
            instance
            for instances in state.handles.get((child_pid, child_gen), {}).values()
            for instance in instances
            if begin_seq <= instance.opened_seq <= commit_seq
        ]
        corroborated = {
            instance.handle_value: instance
            for instance in child_instances
            if any(node.get("via") == "duplicate" for node in instance.chain)
        }
        for parent in _parent_candidates(state, launch, commit_seq):
            if _belongs_to_other_window(state, parent, launch):
                continue
            dup = corroborated.get(parent.handle_value)
            if dup is not None:
                record = {
                    "child_handle": f"0x{dup.handle_value:X}",
                    "child_pid": child_pid,
                    "child_generation": child_gen,
                    "object_id": dup.object_id,
                    "object_name": dup.object_name,
                    "object_category": dup.object_category,
                    # Duplicated handles carry the event's granted mask.
                    "granted_access": f"0x{dup.granted_access:X}",
                    "granted_rights": decode_access_mask(dup.granted_access),
                    "source_instance": parent.instance_id,
                    "source_chain": list(dup.chain)
                    + [
                        {
                            "via": f"inheritance:{mode}",
                            "seq": commit_seq,
                            "source_instance": parent.instance_id,
                        }
                    ],
                    "evidence": "duplicate_event",
                    "status": "confirmed",
                    "policy": _evaluate_policy(
                        policy, dup.object_category, dup.granted_access
                    ),
                }
            else:
                record = {
                    "child_handle": f"0x{parent.handle_value:X}",
                    "child_pid": child_pid,
                    "child_generation": child_gen,
                    "object_id": parent.object_id,
                    "object_name": parent.object_name,
                    "object_category": parent.object_category,
                    "granted_access": f"0x{parent.granted_access:X}",
                    "granted_rights": decode_access_mask(parent.granted_access),
                    "source_instance": parent.instance_id,
                    "source_chain": _build_chain(state, parent, commit_seq, mode),
                    "evidence": "replay_prediction_no_snapshot",
                    "status": "unverified",
                    "provenance_note": (
                        "no trusted snapshot and no corroborating duplicate "
                        "event; inheritance is predicted but not proven"
                    ),
                    "policy": _evaluate_policy(
                        policy, parent.object_category, parent.granted_access
                    ),
                }
            inherited.append(record)
            seen_values.add(parent.handle_value)

        # Child-held duplicated handles whose value is not among the parent
        # candidates (e.g. duplicated under a different value): still evidence.
        for value, dup in sorted(corroborated.items()):
            if value in seen_values:
                continue
            inherited.append(
                {
                    "child_handle": f"0x{dup.handle_value:X}",
                    "child_pid": child_pid,
                    "child_generation": child_gen,
                    "object_id": dup.object_id,
                    "object_name": dup.object_name,
                    "object_category": dup.object_category,
                    "granted_access": f"0x{dup.granted_access:X}",
                    "granted_rights": decode_access_mask(dup.granted_access),
                    "source_chain": list(dup.chain),
                    "evidence": "duplicate_event",
                    "status": "confirmed",
                    "policy": _evaluate_policy(
                        policy, dup.object_category, dup.granted_access
                    ),
                }
            )

    violations = [
        {
            "child_handle": item["child_handle"],
            "object_id": item["object_id"],
            "object_category": item["object_category"],
            "granted_rights": item["granted_rights"],
            "extra_rights": item["policy"]["extra_rights"],
            "category_allowed": item["policy"]["category_allowed"],
            "status": item["status"],
            "evidence": item["evidence"],
        }
        for item in inherited
        if item["status"] == "confirmed" and not item["policy"]["compliant"]
    ]
    first_violation = min(
        violations,
        key=lambda v: (int(v["child_handle"], 16), v["object_id"]),
        default=None,
    )

    return {
        "launch_id": launch["launch_id"],
        "parent": {"pid": launch["parent_pid"], "generation": launch["parent_generation"]},
        "child": {"pid": child_pid, "generation": child_gen},
        "window": {"begin_seq": begin_seq, "commit_seq": commit_seq},
        "inheritance_mode": mode,
        "explicit_handles": [
            f"0x{h:X}" for h in launch.get("explicit_handles", [])
        ],
        "snapshot": {
            "present": snapshot is not None,
            "snapshot_seq": snapshot_seq if snapshot is not None else None,
        },
        "gap_in_window": gap,
        "completeness": "complete" if complete else "incomplete",
        "inherited_handles": inherited,
        "confirmed_count": sum(1 for i in inherited if i["status"] == "confirmed"),
        "unverified_count": sum(1 for i in inherited if i["status"] == "unverified"),
        "not_inherited_count": sum(
            1 for i in inherited if i["status"] == "not_inherited"
        ),
        "policy_violations": violations,
        "first_noncompliant_inheritance": first_violation,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_events(
    events: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]] | None = None,
    launches: Iterable[dict[str, Any]] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay events and analyze every launch.

    ``events`` need not be pre-sorted; they are ordered by ``seq``.
    ``snapshots`` are trusted collector records keyed by ``snapshot_seq``.
    ``launches`` pair begin/commit sequence numbers with the inheritance mode
    and the snapshot sequence claimed by the collector.
    """
    ordered = sorted(events, key=lambda e: e.get("seq", 0))
    snapshots_by_seq: dict[int, dict[str, Any]] = {}
    for snap in snapshots or []:
        seq = snap.get("snapshot_seq")
        if not isinstance(seq, int):
            continue
        snapshots_by_seq[seq] = snap

    state = _Replay(
        events=ordered,
        snapshots_by_seq=snapshots_by_seq,
        launches=list(launches or []),
        policy=policy or {},
        occupied_seqs={
            seq for seq in snapshots_by_seq if isinstance(seq, int)
        },
    )
    _replay_events(state)

    # Annotate gaps with the launch windows that contain them.
    for gap in state.gaps:
        windows = [
            launch["launch_id"]
            for launch in state.launches
            if gap["after_seq"] >= launch["begin_seq"]
            and gap["before_seq"] <= launch["commit_seq"]
        ]
        gap["window"] = windows[0] if windows else "global"
        gap["launch_windows"] = windows

    missing_snapshot_launches: list[dict[str, Any]] = []
    for launch in state.launches:
        report = _analyze_launch(state, launch)
        state.launch_reports.append(report)
        if not report["snapshot"]["present"]:
            missing_snapshot_launches.append(
                {"launch_id": launch["launch_id"], "claimed_snapshot_seq":
                 launch.get("snapshot_seq")}
            )

    all_violations = [
        (report["window"]["commit_seq"], report["launch_id"], violation)
        for report in state.launch_reports
        for violation in report["policy_violations"]
    ]
    all_violations.sort(key=lambda triple: (triple[0], int(triple[2]["child_handle"], 16)))
    first_overall = None
    if all_violations:
        commit_seq, launch_id, violation = all_violations[0]
        first_overall = {"launch_id": launch_id, "at_seq": commit_seq, **violation}

    return {
        "schema_version": 1,
        "event_count": len(ordered),
        "snapshot_count": len(snapshots_by_seq),
        "launch_count": len(state.launch_reports),
        "sequence_gaps": state.gaps,
        "anomalies": state.anomalies,
        "launches_missing_snapshot": missing_snapshot_launches,
        "first_noncompliant_inheritance": first_overall,
        "launches": state.launch_reports,
    }


def analyze_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    """Convenience wrapper for the on-disk sample/dataset schema."""
    return analyze_events(
        events=dataset.get("events", []),
        snapshots=dataset.get("snapshots", []),
        launches=dataset.get("launches", []),
        policy=dataset.get("policy", {}),
    )
