"""Partner-facing pseudonym registry with merge / split support.

Contract
--------
* ``login(partner, institution, subject_id)`` carries the source institution
  and the *stable original-subject number* from the identity source.
* A partner receives an opaque handle (pseudonym) per original subject.  A
  handle is issued once and is **never renamed**.
* ``merge(a, b)`` makes two original subjects resolve as one internal identity
  ("cluster").  While merged, a partner sees one handle for that identity: the
  earliest handle it already knew, or a new one.  Every handle keeps the
  original subject whose login created it (``initiator``), so the merge can be
  undone exactly.
* ``split(subject)`` gives every active binding back to its initiating
  original subject.  A subject that had no binding of its own before the merge
  gets a fresh handle on its next login.  Bindings imported without provenance
  ("legacy") cannot be attributed and are **suspended**, never guessed; an
  operator may ``adopt_legacy`` them explicitly.
* Partner exports/replays contain only that partner's own opaque handles plus
  generic status/event labels: no internal account or cluster id, no
  original-subject number and no global sequence number.  Two partners'
  exports therefore have no identifier in common.

All public methods are serialised by a re-entrant lock, so simultaneous
registrations from two partners cannot interleave a read-modify-write.
"""

from __future__ import annotations

import functools
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

HANDLE_PREFIX = "psn_"
ACCOUNT_PREFIX = "acct_"
CLUSTER_PREFIX = "grp_"
SUBJECT_PREFIX = "sub_"
# Subject numbers must not collide with the registry's own opaque universe.
RESERVED_PREFIXES = (ACCOUNT_PREFIX, CLUSTER_PREFIX, HANDLE_PREFIX,
                     SUBJECT_PREFIX)

# Binding event -> the generic label a partner may see about its own handle.
_PARTNER_EVENT_LABELS = {
    "issued": "registered",
    "seen": "seen",
    "merged": "identity-merged",
    "restored": "restored",
    "suspended": "suspended",
    "adopted": "adopted",
    "imported-legacy": "imported-legacy",
}


def _token(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _require_text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string")
    return value


def validate_subject_id(subject_id: object) -> str:
    """Validate a stable original-subject number supplied by a login."""
    subject_id = _require_text(subject_id, "subject_id")
    if subject_id.startswith(RESERVED_PREFIXES):
        raise ValueError(
            f"subject_id {subject_id!r} must not look like an internal identifier")
    return subject_id


def _locked(func):
    """Serialise access; public methods are short, so one lock is enough."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return func(self, *args, **kwargs)
    return wrapper


@dataclass
class Account:
    """One original identity.  ``internal_id`` never leaves the registry."""

    internal_id: str
    subject_id: str


@dataclass
class Cluster:
    """Internal identity: one account, or several merged accounts."""

    id: str
    members: List[str]                # internal account ids, merge order
    state: str                        # "single" | "merged" | "split"
    history: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class Binding:
    """One partner-visible handle plus the provenance needed to undo a merge.

    ``initiator`` is the original-subject number whose login created the
    binding.  ``None`` marks a legacy row without provenance.
    """

    handle: str
    partner: str
    institution: str
    cluster_id: Optional[str]
    origin_account: str               # internal account that held it at creation
    initiator: Optional[str]          # original-subject number, or None
    created_seq: int
    created_phase: str                # "single" | "merged" | "legacy"
    active: bool = True
    legacy: bool = False
    events: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class SplitReport:
    """Operator-facing result of splitting a merged identity."""

    subject_ids: List[str]
    members: List[str]
    restored: Dict[str, List[str]]
    suspended: List[str]

    def as_dict(self) -> Dict[str, object]:
        return {
            "subject_ids": list(self.subject_ids),
            "members": list(self.members),
            "restored": {k: list(v) for k, v in self.restored.items()},
            "suspended": list(self.suspended),
        }


class PseudonymRegistry:
    """Issues per-partner pseudonyms for stable original subjects."""

    def __init__(self) -> None:
        self._seq = 0
        self._accounts: Dict[str, Account] = {}
        self._by_subject: Dict[str, str] = {}
        self._clusters: Dict[str, Cluster] = {}
        self._account_cluster: Dict[str, str] = {}
        self._bindings: Dict[Tuple[str, str], Binding] = {}
        self._log: List[Dict[str, object]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _new_handle(self) -> str:
        while True:
            handle = _token(HANDLE_PREFIX)
            if not any(h == handle for (_p, h) in self._bindings):
                return handle

    def _account(self, internal_id: str) -> Account:
        try:
            return self._accounts[internal_id]
        except KeyError:
            raise KeyError(f"unknown internal account {internal_id!r}") from None

    def _account_for_subject(self, subject_id: str) -> Account:
        internal_id = self._by_subject.get(subject_id)
        if internal_id is None:
            raise KeyError(f"unknown original subject {subject_id!r}")
        return self._accounts[internal_id]

    def _cluster_of(self, account: Account) -> Cluster:
        return self._clusters[self._account_cluster[account.internal_id]]

    def _cluster_bindings(self, cluster_id: str, partner: str,
                          active_only: bool = True) -> List[Binding]:
        found = [b for (_p, _h), b in self._bindings.items()
                 if b.cluster_id == cluster_id and b.partner == partner
                 and (b.active or not active_only)]
        found.sort(key=lambda b: b.created_seq)
        return found

    def _canonical(self, cluster_id: str, partner: str) -> Optional[Binding]:
        """The handle a partner sees for a merged identity.

        Earliest binding wins; bindings with provenance (non-legacy) are
        preferred over legacy rows so an unattributable row cannot become the
        merged identity's face while a real one exists.
        """
        candidates = self._cluster_bindings(cluster_id, partner)
        if not candidates:
            return None
        attributable = [b for b in candidates if not b.legacy]
        return (attributable or candidates)[0]

    def _audit(self, action: str, *, partner: str = "", institution: str = "",
               subject: Optional[str] = None, handle: Optional[str] = None,
               phase: str = "") -> int:
        seq = self._next_seq()
        self._log.append({
            "seq": seq,
            "action": action,
            "partner": partner,
            "institution": institution,
            "subject": subject,
            "handle": handle,
            "phase": phase,
        })
        return seq

    def _new_binding(self, partner: str, institution: str, account: Account,
                     cluster: Cluster, initiator: str,
                     phase: str) -> Binding:
        binding = Binding(
            handle=self._new_handle(),
            partner=partner,
            institution=institution,
            cluster_id=cluster.id,
            origin_account=account.internal_id,
            initiator=initiator,
            created_seq=self._next_seq(),
            created_phase=phase,
        )
        binding.events.append({
            "event": "issued",
            "phase": phase,
            "institution": institution,
            "initiator": initiator,
            "seq": binding.created_seq,
        })
        self._bindings[(partner, binding.handle)] = binding
        self._audit("binding-created", partner=partner, institution=institution,
                    subject=initiator, handle=binding.handle, phase=phase)
        return binding

    # ------------------------------------------------------------------ #
    # Enrolment and login
    # ------------------------------------------------------------------ #
    @_locked
    def enroll(self, subject_id: str, institution: str = "") -> str:
        """Create the account for an original subject.  Returns internal id."""
        subject_id = validate_subject_id(subject_id)
        if subject_id in self._by_subject:
            raise ValueError(f"subject {subject_id!r} is already enrolled")
        internal_id = _token(ACCOUNT_PREFIX)
        cluster = Cluster(
            id=_token(CLUSTER_PREFIX), members=[internal_id], state="single",
            history=[{"event": "enrolled", "subject": subject_id}])
        self._clusters[cluster.id] = cluster
        self._accounts[internal_id] = Account(internal_id, subject_id)
        self._by_subject[subject_id] = internal_id
        self._account_cluster[internal_id] = cluster.id
        self._audit("enrolled", institution=institution, subject=subject_id,
                    phase="single")
        return internal_id

    @_locked
    def login(self, partner: str, institution: str, subject_id: str) -> str:
        """Log in as ``subject_id`` from ``institution``; return the handle.

        The subject is enrolled on first login.  While its identity is merged,
        the partner sees the merged identity's canonical handle; otherwise it
        sees the handle owned by this subject.  A new binding records the
        initiating subject.
        """
        partner = _require_text(partner, "partner")
        institution = _require_text(institution, "institution")
        subject_id = validate_subject_id(subject_id)
        if subject_id not in self._by_subject:
            self.enroll(subject_id, institution=institution)
        account = self._account_for_subject(subject_id)
        cluster = self._cluster_of(account)
        binding = self._resolve_binding(partner, institution, subject_id,
                                        account, cluster)
        binding.events.append({
            "event": "seen",
            "phase": cluster.state,
            "institution": institution,
            "viewer": subject_id,
            "seq": self._next_seq(),
        })
        self._audit("login", partner=partner, institution=institution,
                    subject=subject_id, handle=binding.handle,
                    phase=cluster.state)
        return binding.handle

    def _resolve_binding(self, partner: str, institution: str,
                         subject_id: str, account: Account,
                         cluster: Cluster) -> Binding:
        if cluster.state == "merged":
            canonical = self._canonical(cluster.id, partner)
            if canonical is not None:
                return canonical
            return self._new_binding(partner, institution, account, cluster,
                                     subject_id, phase="merged")
        own = [b for b in self._cluster_bindings(cluster.id, partner)
               if b.initiator == subject_id and not b.legacy]
        if own:
            return own[0]
        canonical = self._canonical(cluster.id, partner)
        if canonical is not None:
            # A single identity with a legacy-only binding keeps serving it.
            return canonical
        return self._new_binding(partner, institution, account, cluster,
                                 subject_id, phase="single")

    @_locked
    def register(self, partner: str, institution: str,
                 subject_id: str) -> Dict[str, object]:
        """Explicit registration; same resolution rules as :meth:`login`."""
        subject_id = validate_subject_id(subject_id)
        handle = self.login(partner, institution, subject_id)
        binding = self._bindings[(partner, handle)]
        return {
            "partner": partner,
            "subject": subject_id,
            "institution": institution,
            "handle": handle,
            "created_seq": binding.created_seq,
            "created_phase": binding.created_phase,
            "status": self._binding_status(binding),
        }

    # ------------------------------------------------------------------ #
    # Merge / split
    # ------------------------------------------------------------------ #
    @_locked
    def merge(self, subject_a: str, subject_b: str) -> str:
        """Merge two original subjects into one internal identity.

        Existing handles are not renamed and keep their initiator, so a later
        split can restore each one exactly.  Returns the internal cluster id
        (operator use only; never exported).
        """
        subject_a = validate_subject_id(subject_a)
        subject_b = validate_subject_id(subject_b)
        if subject_a == subject_b:
            raise ValueError("cannot merge a subject with itself")
        acc_a = self._account_for_subject(subject_a)
        acc_b = self._account_for_subject(subject_b)
        cluster_a = self._cluster_of(acc_a)
        cluster_b = self._cluster_of(acc_b)
        if cluster_a.id == cluster_b.id:
            raise ValueError(
                f"{subject_a!r} and {subject_b!r} are already one identity")

        seq = self._next_seq()
        members = list(dict.fromkeys(cluster_a.members + cluster_b.members))
        subjects = sorted(self._accounts[m].subject_id for m in members)
        merged = Cluster(
            id=_token(CLUSTER_PREFIX), members=members, state="merged",
            history=[{"event": "merged", "subjects": subjects,
                      "sources": [cluster_a.id, cluster_b.id], "seq": seq}])
        self._clusters[merged.id] = merged
        for old in (cluster_a, cluster_b):
            old.state = "split"
            old.history.append({"event": "merged-into", "cluster": merged.id,
                                "seq": seq})
        for internal_id in members:
            self._account_cluster[internal_id] = merged.id
        for binding in self._bindings.values():
            if binding.active and binding.cluster_id in (cluster_a.id,
                                                          cluster_b.id):
                binding.cluster_id = merged.id
                binding.events.append({"event": "merged", "seq": seq})
        self._audit("merged", subject=subject_a, phase="merged")
        return merged.id

    @_locked
    def split(self, subject_id: str) -> SplitReport:
        """Split the merged identity containing ``subject_id`` back apart.

        Every active binding returns only to the original subject that
        initiated it.  Bindings without provenance are suspended.
        """
        subject_id = validate_subject_id(subject_id)
        account = self._account_for_subject(subject_id)
        cluster = self._cluster_of(account)
        if cluster.state != "merged":
            raise ValueError(
                f"subject {subject_id!r} is not merged; nothing to split")

        members = list(cluster.members)
        member_subject = {m: self._accounts[m].subject_id for m in members}
        subject_member = {s: m for m, s in member_subject.items()}
        seq = self._next_seq()

        cluster.state = "split"
        cluster.history.append({"event": "split", "seq": seq})

        fresh: Dict[str, str] = {}
        for internal_id in members:
            single = Cluster(
                id=_token(CLUSTER_PREFIX), members=[internal_id],
                state="single",
                history=[{"event": "split-from", "cluster": cluster.id,
                          "seq": seq}])
            self._clusters[single.id] = single
            self._account_cluster[internal_id] = single.id
            fresh[internal_id] = single.id

        restored: Dict[str, List[str]] = {s: [] for s in member_subject.values()}
        suspended: List[str] = []

        for binding in self._bindings.values():
            if not binding.active or binding.cluster_id != cluster.id:
                continue
            owner = (subject_member.get(binding.initiator)
                     if binding.initiator else None)
            if binding.legacy or owner is None:
                binding.active = False
                binding.cluster_id = None
                binding.events.append({"event": "suspended", "seq": seq,
                                       "reason": "unassignable"})
                suspended.append(binding.handle)
                self._audit("binding-suspended", partner=binding.partner,
                            institution=binding.institution,
                            subject=binding.initiator,
                            handle=binding.handle, phase="split")
                continue
            binding.cluster_id = fresh[owner]
            binding.events.append({"event": "restored", "seq": seq,
                                   "subject": binding.initiator})
            restored[binding.initiator].append(binding.handle)
            self._audit("binding-restored", partner=binding.partner,
                        institution=binding.institution,
                        subject=binding.initiator, handle=binding.handle,
                        phase="split")

        self._audit("split", subject=subject_id, phase="split")
        return SplitReport(
            subject_ids=sorted(member_subject.values()),
            members=members,
            restored=restored,
            suspended=suspended,
        )

    # ------------------------------------------------------------------ #
    # Legacy (unattributable) bindings
    # ------------------------------------------------------------------ #
    @_locked
    def import_legacy_binding(self, partner: str, internal_id: str,
                              institution: str = "legacy") -> str:
        """Import a pre-provenance binding for an account.

        The row has no initiating subject, so any split suspends it instead of
        guessing which member it belongs to.  Idempotent for the same
        partner/account pair.
        """
        partner = _require_text(partner, "partner")
        account = self._account(internal_id)
        cluster = self._cluster_of(account)
        for binding in self._cluster_bindings(cluster.id, partner):
            if binding.origin_account == account.internal_id:
                if not binding.legacy:
                    raise ValueError(
                        f"partner {partner!r} already has a regular binding "
                        f"for {internal_id!r}")
                return binding.handle
            raise ValueError(
                f"partner {partner!r} already has a binding on this identity")
        seq = self._next_seq()
        binding = Binding(
            handle=self._new_handle(),
            partner=partner,
            institution=institution,
            cluster_id=cluster.id,
            origin_account=account.internal_id,
            initiator=None,
            created_seq=seq,
            created_phase="legacy",
            legacy=True,
        )
        binding.events.append({"event": "imported-legacy", "seq": seq})
        self._bindings[(partner, binding.handle)] = binding
        self._audit("binding-imported", partner=partner,
                    institution=institution, subject=None,
                    handle=binding.handle, phase="legacy")
        return binding.handle

    @_locked
    def adopt_legacy(self, partner: str, handle: str, subject_id: str) -> None:
        """Explicitly attribute a suspended legacy binding to a subject.

        The registry never guesses this itself; an operator supplies the
        subject after an out-of-band check.
        """
        subject_id = validate_subject_id(subject_id)
        binding = self._bindings.get((partner, handle))
        if binding is None:
            raise KeyError(f"unknown binding {handle!r} for partner {partner!r}")
        if not binding.legacy:
            raise ValueError(f"binding {handle!r} is not legacy")
        if binding.active:
            raise ValueError(f"binding {handle!r} is not suspended")
        account = self._account_for_subject(subject_id)
        cluster = self._cluster_of(account)
        if self._cluster_bindings(cluster.id, partner):
            raise ValueError(
                f"partner {partner!r} already has an active binding for "
                f"{subject_id!r}")
        binding.legacy = False
        binding.active = True
        binding.initiator = subject_id
        binding.origin_account = account.internal_id
        binding.cluster_id = cluster.id
        seq = self._next_seq()
        binding.events.append({"event": "adopted", "seq": seq,
                               "subject": subject_id})
        self._audit("binding-adopted", partner=partner, subject=subject_id,
                    handle=handle, phase="single")

    # ------------------------------------------------------------------ #
    # Status and operator queries
    # ------------------------------------------------------------------ #
    def _binding_status(self, binding: Binding) -> str:
        """Partner-visible status: active, merged (folded in), or suspended."""
        if not binding.active:
            return "suspended"
        if binding.cluster_id is not None:
            cluster = self._clusters[binding.cluster_id]
            if cluster.state == "merged":
                canonical = self._canonical(cluster.id, binding.partner)
                if canonical is not None and canonical.handle != binding.handle:
                    return "merged"
        return "active"

    @_locked
    def explain(self, partner: str, handle: str) -> Dict[str, object]:
        """Full lineage of one handle (operator view).

        Includes the owning original-subject number, so it must never be
        handed to a partner; use :meth:`explain_for_partner` for that.
        """
        binding = self._bindings.get((partner, handle))
        if binding is None:
            raise KeyError(f"unknown handle {handle!r} for partner {partner!r}")
        cluster = (self._clusters.get(binding.cluster_id)
                   if binding.cluster_id else None)
        return {
            "handle": binding.handle,
            "partner": binding.partner,
            "status": self._binding_status(binding),
            "legacy": binding.legacy,
            "initiator": binding.initiator,
            "created_seq": binding.created_seq,
            "created_phase": binding.created_phase,
            "current_phase": cluster.state if cluster else "suspended",
            "events": [dict(e) for e in binding.events],
        }

    @_locked
    def explain_for_partner(self, partner: str,
                            handle: str) -> Dict[str, object]:
        """Partner-safe lineage: handle, status, generic event timeline."""
        binding = self._bindings.get((partner, handle))
        if binding is None:
            raise KeyError(f"unknown handle {handle!r} for partner {partner!r}")
        return {
            "handle": binding.handle,
            "status": self._binding_status(binding),
            "timeline": self._partner_timeline(binding),
        }

    def _partner_timeline(self, binding: Binding) -> List[str]:
        timeline: List[str] = []
        for event in binding.events:
            label = _PARTNER_EVENT_LABELS.get(event["event"])
            if label is None:
                continue
            if not timeline or timeline[-1] != label:
                timeline.append(label)
        return timeline

    @_locked
    def handle_for(self, partner: str, subject_id: str) -> Optional[str]:
        """Current handle a partner would see for a subject, if any."""
        account = self._account_for_subject(subject_id)
        cluster = self._cluster_of(account)
        if cluster.state == "merged":
            canonical = self._canonical(cluster.id, partner)
            return canonical.handle if canonical else None
        own = [b for b in self._cluster_bindings(cluster.id, partner)
               if b.initiator == subject_id and not b.legacy]
        if own:
            return own[0].handle
        canonical = self._canonical(cluster.id, partner)
        return canonical.handle if canonical else None

    @_locked
    def active_handles(self, partner: str) -> List[str]:
        _require_text(partner, "partner")
        return sorted(h for (p, h), b in self._bindings.items()
                      if p == partner and b.active)

    @_locked
    def suspended_handles(self, partner: str) -> List[str]:
        _require_text(partner, "partner")
        return sorted(h for (p, h), b in self._bindings.items()
                      if p == partner and not b.active)

    @_locked
    def account_id(self, subject_id: str) -> str:
        """Internal account id (operator use only; never exported)."""
        return self._account_for_subject(
            validate_subject_id(subject_id)).internal_id

    @_locked
    def subjects(self) -> List[str]:
        return sorted(self._by_subject)

    @_locked
    def merged_subjects(self) -> List[List[str]]:
        """Currently merged subject groups (operator view)."""
        groups = []
        for cluster in self._clusters.values():
            if cluster.state == "merged":
                groups.append(sorted(self._accounts[m].subject_id
                                     for m in cluster.members))
        return sorted(groups)

    # ------------------------------------------------------------------ #
    # Partner-safe exports and replay
    # ------------------------------------------------------------------ #
    @_locked
    def export_for_partner(self, partner: str) -> List[Dict[str, object]]:
        """Data one partner may receive.

        Only this partner's own opaque handles plus status and generic event
        labels: no internal account/cluster id, no original-subject number,
        no global sequence number, no other partner's handle.  Two partners'
        exports therefore share no identifier.
        """
        _require_text(partner, "partner")
        rows: List[Dict[str, object]] = []
        for (p, handle), binding in sorted(self._bindings.items()):
            if p != partner:
                continue
            rows.append({
                "handle": handle,
                "status": self._binding_status(binding),
                "lineage": self._partner_timeline(binding),
            })
        return rows

    @_locked
    def export_json(self, partner: str) -> str:
        """JSON serialisation of :meth:`export_for_partner`."""
        return json.dumps(self.export_for_partner(partner), sort_keys=True)

    @_locked
    def replay(self, partner: str,
               only_registrations: bool = False) -> List[Dict[str, object]]:
        """What this partner saw, in order, one row per registration/login.

        With ``only_registrations=True`` only moments at which a new handle was
        created are returned.  The handle is exactly what the partner saw at
        that moment.  Rows carry no internal ids, no subject numbers and no
        global sequence number; ``step`` is a per-partner ordinal.
        """
        _require_text(partner, "partner")
        rows: List[Dict[str, object]] = []
        step = 0
        for entry in self._log:
            if entry["partner"] != partner:
                continue
            if entry["action"] not in ("login", "binding-created"):
                continue
            if only_registrations and entry["action"] != "binding-created":
                continue
            step += 1
            rows.append({
                "step": step,
                "action": ("registered" if entry["action"] == "binding-created"
                           else "seen"),
                "handle": entry["handle"],
                "phase": entry["phase"],
                "institution": entry["institution"],
            })
        return rows

    @_locked
    def audit_log(self) -> List[Dict[str, object]]:
        """Full operator audit log, including subject numbers and internals."""
        return [dict(row) for row in self._log]
