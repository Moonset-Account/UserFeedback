"""Rule engine: does the log prove "the validated object is the committed one"?

Every verdict is one of three values:

* VIOLATION  -- the happens-before graph and the memory model prove the rule
                was broken;
* UNDECIDABLE -- no ordering/identity evidence exists, often because a sync
                 record is missing. The verifier never guesses;
* (implicitly) PASS -- no finding was produced for the rule.

All checks use graph reachability and model facts only. The arrival order of
events is never read, so two identical histories with shuffled records yield
byte-identical reports.
"""

from .events import (
    Event,
    BIND,
    CANCEL,
    HANDLE_CREATE,
    RETRY,
    SYNC,
    VALIDATE_OK,
    WRITE_COMMIT,
)
from .findings import AuditReport, Finding, Verdict
from .model import FileServiceModel
from .order import OrderGraph


class Verifier:
    def __init__(self, events: list[Event]):
        self.events = events
        self.graph = OrderGraph(events)
        self.model = FileServiceModel(events, self.graph)
        self.findings: list[Finding] = []
        self._creates: dict[str, list[Event]] = {}
        for e in events:
            if e.type == HANDLE_CREATE and e.handle:
                self._creates.setdefault(e.handle, []).append(e)

    # ------------------------------------------------------------- utilities
    def _dangling_note(self, event: Event) -> list[str]:
        notes = []
        for ref, label in self.graph.dangling_for(event):
            notes.append(
                f"missing synchronization record: {label} edge refers to "
                f"unknown event {ref!r}"
            )
        return notes

    def _order_unknown(self, a: Event, b: Event) -> bool:
        """True when neither direction between a and b is provable."""
        return not self.graph.hb(a.event_id, b.event_id) and not self.graph.hb(
            b.event_id, a.event_id
        )

    def _check_handle_exists(self, e: Event) -> bool:
        if not e.handle or e.handle in self._creates:
            return True
        self.findings.append(
            Finding(
                rule_id="unknown_handle",
                verdict=Verdict.VIOLATION,
                events=[e.event_id],
                message=(
                    f"{e.type} event {e.event_id} references handle "
                    f"{e.handle!r} without any HANDLE_CREATE record"
                ),
            )
        )
        return False

    # ------------------------------------------------------------- structure
    def _structural(self) -> None:
        for chain in self.graph.cycles:
            self.findings.append(
                Finding(
                    rule_id="hb_cycle",
                    verdict=Verdict.VIOLATION,
                    events=list(chain),
                    message=(
                        "synchronization/thread edges form a cycle, so a "
                        "consistent execution order does not exist"
                    ),
                    evidence=[" -> ".join(chain)],
                )
            )
        for thread, seq, ids in self.graph.thread_seq_conflicts:
            self.findings.append(
                Finding(
                    rule_id="seq_conflict",
                    verdict=Verdict.UNDECIDABLE,
                    events=list(ids),
                    message=(
                        f"events {ids} share thread={thread!r} seq={seq}; "
                        "thread-internal order cannot be reconstructed"
                    ),
                    unknown_reason="duplicate (thread, seq)",
                )
            )
        for ev, ref, label in self.graph.dangling_edges:
            self.findings.append(
                Finding(
                    rule_id="missing_sync_record",
                    verdict=Verdict.UNDECIDABLE,
                    events=[ev.event_id],
                    message=(
                        f"event {ev.event_id} has a {label} edge to {ref!r}, "
                        "but that predecessor event is absent from the log"
                    ),
                    unknown_reason=(
                        f"dangling {label} edge; ordering across this "
                        "synchronization point is unprovable"
                    ),
                )
            )

    # ------------------------------------------------------------------ bind
    def _check_bind(self, e: Event) -> None:
        if not self._check_handle_exists(e):
            return
        if e.object_no is None or e.object_gen is None:
            self.findings.append(
                Finding(
                    rule_id="bind_missing_identity",
                    verdict=Verdict.VIOLATION,
                    events=[e.event_id],
                    message=(
                        f"BIND {e.event_id} does not name both object_no and "
                        "object_gen; a handle cannot be authorized without a "
                        "stable object identity"
                    ),
                )
            )
            return
        create = self._creates[e.handle][-1]
        if not self.graph.hb(create.event_id, e.event_id):
            if self._order_unknown(create, e):
                self.findings.append(
                    Finding(
                        rule_id="bind_create_unordered",
                        verdict=Verdict.UNDECIDABLE,
                        events=[create.event_id, e.event_id],
                        message=(
                            f"cannot prove HANDLE_CREATE {create.event_id} "
                            f"precedes BIND {e.event_id}"
                        ),
                        evidence=self.graph.explain(
                            create.event_id, e.event_id
                        ),
                        unknown_reason="no ordering edge between the events",
                    )
                )
            else:
                self.findings.append(
                    Finding(
                        rule_id="bind_before_create",
                        verdict=Verdict.VIOLATION,
                        events=[e.event_id, create.event_id],
                        message=(
                            f"BIND {e.event_id} is ordered before "
                            f"HANDLE_CREATE {create.event_id}"
                        ),
                        evidence=self.graph.explain(
                            e.event_id, create.event_id
                        ),
                    )
                )

    # -------------------------------------------------------------- validate
    def _check_validate(self, e: Event) -> None:
        if not self._check_handle_exists(e):
            return
        if e.object_no is None or e.object_gen is None:
            self.findings.append(
                Finding(
                    rule_id="validate_missing_identity",
                    verdict=Verdict.VIOLATION,
                    events=[e.event_id],
                    message=(
                        f"VALIDATE_OK {e.event_id} does not name both "
                        "object_no and object_gen"
                    ),
                )
            )
            return
        hs = self.model.handle(e.handle)
        binds = [
            b
            for b in self.events
            if b.type == BIND and b.handle == e.handle
        ]
        prior_binds = [
            b for b in binds if self.graph.hb(b.event_id, e.event_id)
        ]
        if not prior_binds:
            unordered = [b for b in binds if self._order_unknown(b, e)]
            if unordered:
                self.findings.append(
                    Finding(
                        rule_id="validate_bind_unordered",
                        verdict=Verdict.UNDECIDABLE,
                        events=[e.event_id]
                        + [b.event_id for b in unordered],
                        message=(
                            f"cannot prove any BIND precedes VALIDATE_OK "
                            f"{e.event_id}"
                        ),
                        unknown_reason="no ordering edge between the events",
                    )
                )
            else:
                verdict = Verdict.UNDECIDABLE if self.graph.dangling_for(e) \
                    else Verdict.VIOLATION
                self.findings.append(
                    Finding(
                        rule_id="validate_without_bind",
                        verdict=verdict,
                        events=[e.event_id],
                        message=(
                            f"VALIDATE_OK {e.event_id} for handle "
                            f"{e.handle!r} has no BIND record"
                        ),
                        evidence=self._dangling_note(e),
                        unknown_reason=(
                            "binding record may be missing with the lost "
                            "sync log"
                            if verdict == Verdict.UNDECIDABLE
                            else None
                        ),
                    )
                )
            return

        identity = (e.object_no, e.object_gen)
        matching = [
            b for b in prior_binds
            if (b.object_no, b.object_gen) == identity
        ]
        if not matching:
            self.findings.append(
                Finding(
                    rule_id="validate_identity_mismatch",
                    verdict=Verdict.VIOLATION,
                    events=[prior_binds[-1].event_id, e.event_id],
                    message=(
                        f"VALIDATE_OK {e.event_id} claims "
                        f"{e.object_no}@gen{e.object_gen} but every prior "
                        f"BIND of handle {e.handle!r} names a different "
                        "object identity"
                    ),
                    evidence=self.graph.explain(
                        prior_binds[-1].event_id, e.event_id
                    ),
                )
            )
            return

        # Stale generation: a rebind to a different identity happened after
        # the bind that matches this validation, yet the validation still
        # asserts the superseded identity.
        stale = next(
            (
                b
                for b in prior_binds
                if (b.object_no, b.object_gen) != identity
                and any(
                    self.graph.hb(m.event_id, b.event_id) for m in matching
                )
            ),
            None,
        )
        if stale is not None:
            self.findings.append(
                Finding(
                    rule_id="validate_stale_generation",
                    verdict=Verdict.VIOLATION,
                    events=[stale.event_id, e.event_id],
                    message=(
                        f"VALIDATE_OK {e.event_id} revalidates old "
                        f"{e.object_no}@gen{e.object_gen} after handle "
                        f"{e.handle!r} was rebound to "
                        f"{stale.object_no}@gen{stale.object_gen} by BIND "
                        f"{stale.event_id}"
                    ),
                    evidence=self.graph.explain(
                        stale.event_id, e.event_id
                    ),
                )
            )

    # ------------------------------------------------------------------ commit
    def _prior_binds(self, handle: str, event: Event) -> list[Event]:
        return [
            b
            for b in self.events
            if b.type == BIND
            and b.handle == handle
            and self.graph.hb(b.event_id, event.event_id)
        ]

    def _current_binding_at(
        self, event: Event
    ) -> tuple[Event | None, bool]:
        """(latest proven BIND at this point, ambiguous?).

        Ambiguous means two mutually unordered BIND events are both maximal
        under happens-before, so the object identity current at ``event``
        cannot be determined from the evidence.
        """
        prior = self._prior_binds(event.handle or "", event)
        if not prior:
            return None, False
        maximal = [
            b
            for b in prior
            if not any(
                other.event_id != b.event_id
                and self.graph.hb(b.event_id, other.event_id)
                for other in prior
            )
        ]
        identities = {
            (b.object_no, b.object_gen) for b in maximal
        }
        if len(identities) > 1:
            return None, True
        return maximal[0], False

    def _check_commit(self, e: Event) -> None:
        if not self._check_handle_exists(e):
            return

        all_binds = [
            b
            for b in self.events
            if b.type == BIND and b.handle == e.handle
        ]
        prior_binds = self._prior_binds(e.handle or "", e)
        unordered_binds = [b for b in all_binds if self._order_unknown(b, e)]
        validations = self.model.validations_for(e.handle, e.task or "")
        dangling = bool(self.graph.dangling_for(e))

        # ---- Step 0: concurrent rebinds make the current binding unknown --
        # Candidate attachments are every BIND not proven *after* the commit.
        # If two maximal candidates name different identities and are not
        # mutually ordered, no record says which object the handle named at
        # commit time.
        candidate_binds = [
            b for b in all_binds
            if not self.graph.hb(e.event_id, b.event_id)
        ]
        if candidate_binds:
            maximal = [
                b for b in candidate_binds
                if not any(
                    o.event_id != b.event_id
                    and self.graph.hb(b.event_id, o.event_id)
                    for o in candidate_binds
                )
            ]
            identities = {
                (b.object_no, b.object_gen) for b in maximal
            }
            if len(identities) > 1:
                self.findings.append(
                    Finding(
                        rule_id="commit_binding_ambiguous",
                        verdict=Verdict.UNDECIDABLE,
                        events=[e.event_id]
                        + [b.event_id for b in maximal],
                        message=(
                            "mutually unordered BIND events compete as the "
                            f"current binding of handle {e.handle!r}; the "
                            "object named at WRITE_COMMIT "
                            f"{e.event_id} cannot be determined"
                        ),
                        unknown_reason=(
                            "concurrent rebinds without a synchronization "
                            "edge between them or to the commit"
                        ),
                    )
                )
                return

        # ---- Step 1: determine the committed object identity --------------
        # Explicit fields win; otherwise inherit the handle's current binding
        # where that is uniquely provable.
        if e.object_no is None or e.object_gen is None:
            if prior_binds:
                current, _ = self._current_binding_at(e)
                e = Event(
                    event_id=e.event_id,
                    type=e.type,
                    thread=e.thread,
                    seq=e.seq,
                    task=e.task,
                    handle=e.handle,
                    object_no=current.object_no,
                    object_gen=current.object_gen,
                    sync_id=e.sync_id,
                    sync_op=e.sync_op,
                    depends_on=e.depends_on,
                )
            elif not validations:
                # Nothing in the log names an object for this commit.
                self._no_identity_finding(e, unordered_binds, dangling)
                return
            # Validations exist: inherit the identity they assert (checked
            # for consistency below).
            identities = {
                (v.object_no, v.object_gen) for v in validations
            }
            if len(identities) == 1:
                obj, gen = next(iter(identities))
                e = Event(
                    event_id=e.event_id,
                    type=e.type,
                    thread=e.thread,
                    seq=e.seq,
                    task=e.task,
                    handle=e.handle,
                    object_no=obj,
                    object_gen=gen,
                    sync_id=e.sync_id,
                    sync_op=e.sync_op,
                    depends_on=e.depends_on,
                )

        identity = (e.object_no, e.object_gen)

        # ---- Step 2: a same-task VALIDATE_OK is the authorization ---------
        if not validations:
            # Only an edge attached to the commit itself that names a lost
            # predecessor can make the missing authorization unprovable.
            # BIND events that merely live on another thread are complete
            # log evidence that this task never validated the handle.
            if dangling:
                self._unknown_commit(
                    e,
                    "commit_no_validation",
                    "no VALIDATE_OK for this task/handle is present, and a "
                    "synchronization predecessor of the commit itself is "
                    "missing",
                )
            else:
                self.findings.append(
                    Finding(
                        rule_id="commit_without_validation",
                        verdict=Verdict.VIOLATION,
                        events=[e.event_id],
                        message=(
                            f"WRITE_COMMIT {e.event_id} by task {e.task!r} "
                            f"has no VALIDATE_OK on handle {e.handle!r}; the "
                            "committed object was never authorized for this "
                            "task"
                        ),
                    )
                )
            return

        matching = [
            v for v in validations
            if (v.object_no, v.object_gen) == identity
        ]
        if not matching:
            self.findings.append(
                Finding(
                    rule_id="commit_wrong_generation",
                    verdict=Verdict.VIOLATION,
                    events=[e.event_id] + [v.event_id for v in validations],
                    message=(
                        f"task {e.task!r} validated other object identity on "
                        f"handle {e.handle!r}, never "
                        f"{e.object_no}@gen{e.object_gen} committed by "
                        f"{e.event_id}"
                    ),
                )
            )
            return

        before_commit = [
            v for v in matching if self.graph.hb(v.event_id, e.event_id)
        ]
        if not before_commit:
            unordered = [v for v in matching if self._order_unknown(v, e)]
            if unordered:
                self._unknown_commit(
                    e,
                    "commit_validation_unordered",
                    "matching VALIDATE_OK exists but no edge proves it "
                    "happened before WRITE_COMMIT " + e.event_id,
                    extra=[v.event_id for v in unordered],
                )
            else:
                self.findings.append(
                    Finding(
                        rule_id="commit_validation_not_before",
                        verdict=Verdict.VIOLATION,
                        events=[e.event_id]
                        + [v.event_id for v in matching],
                        message=(
                            "every matching VALIDATE_OK is ordered after "
                            f"WRITE_COMMIT {e.event_id}"
                        ),
                    )
                )
            return

        # ---- Step 3: the binding must name the same stable object ---------
        # A conflicting BIND is one whose different identity is the handle's
        # last proven attachment before the commit. An older BIND that was
        # itself superseded by a matching one is not a conflict.
        prior_matching = [
            b for b in prior_binds
            if (b.object_no, b.object_gen) == identity
        ]
        conflicting: list[Event] = []
        if not prior_matching:
            conflicting = [
                b for b in prior_binds
                if (b.object_no, b.object_gen) != identity
            ]
        if conflicting:
            latest_conflict = max(
                conflicting,
                key=lambda b: sum(
                    1 for o in conflicting
                    if o.event_id != b.event_id
                    and self.graph.hb(o.event_id, b.event_id)
                ),
            )
            self.findings.append(
                Finding(
                    rule_id="commit_unbound_identity",
                    verdict=Verdict.VIOLATION,
                    events=[
                        latest_conflict.event_id, e.event_id
                    ] + [v.event_id for v in before_commit],
                    message=(
                        f"WRITE_COMMIT {e.event_id} targets "
                        f"{e.object_no}@gen{e.object_gen} but BIND "
                        f"{latest_conflict.event_id}, proven before the "
                        f"commit, binds handle {e.handle!r} to "
                        f"{latest_conflict.object_no}@gen"
                        f"{latest_conflict.object_gen}"
                    ),
                    evidence=self.graph.explain(
                        latest_conflict.event_id, e.event_id
                    ),
                )
            )
            return

        # BIND events exist but none is ordered relative to the commit: the
        # validation still proves task authorization, but handle/object
        # attachment cannot be fully confirmed. Record UNDECIDABLE without
        # blocking the authorization-chain checks below.
        bind_warning: Finding | None = None
        if not prior_binds and unordered_binds:
            bind_warning = Finding(
                rule_id="commit_bind_unordered",
                verdict=Verdict.UNDECIDABLE,
                events=[e.event_id] + [b.event_id for b in unordered_binds],
                message=(
                    f"BIND of handle {e.handle!r} is not ordered relative to "
                    f"WRITE_COMMIT {e.event_id}; validation proves task "
                    "authorization but handle attachment is unprovable"
                ),
                unknown_reason="no ordering edge between BIND and commit",
            )
        elif not all_binds and dangling:
            bind_warning = Finding(
                rule_id="commit_no_bind_evidence",
                verdict=Verdict.UNDECIDABLE,
                events=[e.event_id],
                message=(
                    f"no BIND record for handle {e.handle!r} and a "
                    "synchronization predecessor of the commit is missing"
                ),
                evidence=self._dangling_note(e),
                unknown_reason="BIND may be among the missing sync logs",
            )

        # ---- Step 4: validate -> cancel/rebind/retry -> commit intact -----
        chain_findings = self._authorization_chain_findings(e, before_commit)
        if bind_warning is not None and not chain_findings:
            self.findings.append(bind_warning)
        self.findings.extend(chain_findings)

    def _no_identity_finding(
        self,
        event: Event,
        unordered_binds: list[Event],
        dangling: bool,
    ) -> None:
        if unordered_binds or dangling:
            self.findings.append(
                Finding(
                    rule_id="commit_identity_unknown",
                    verdict=Verdict.UNDECIDABLE,
                    events=[event.event_id]
                    + [b.event_id for b in unordered_binds],
                    message=(
                        f"WRITE_COMMIT {event.event_id} names no object "
                        "identity and no ordered BIND/VALIDATE_OK supplies "
                        "one"
                    ),
                    evidence=self._dangling_note(event),
                    unknown_reason=(
                        "the records establishing the committed object are "
                        "missing or unordered"
                    ),
                )
            )
        else:
            self.findings.append(
                Finding(
                    rule_id="commit_unbound_handle",
                    verdict=Verdict.VIOLATION,
                    events=[event.event_id],
                    message=(
                        f"WRITE_COMMIT {event.event_id} uses handle "
                        f"{event.handle!r} that was never bound to an object"
                    ),
                )
            )

    def _authorization_chain_findings(
        self, commit: Event, candidates: list[Event]
    ) -> list[Finding]:
        """Validate -> (cancel/rebind/retry) -> commit must stay intact.

        Three-valued per invalidation event X (cancel / rebind / retry):
          * hb(validation, X) and hb(X, commit)      -> proven invalidation
          * hb(X, commit) but X || validation         -> UNDECIDABLE
          * X after commit, or before validation      -> irrelevant
        """
        proven: list[Finding] = []
        undecidable: list[Finding] = []

        for v in candidates:
            cancel = self._intervening_cancel(v, commit)
            rebind = self._rebound_between(v, commit)
            retry = self._retried_between(v, commit)
            if cancel is None and rebind is None and retry is None:
                if self._unordered_invalidation(v, commit):
                    # This candidate might have been voided; keep checking the
                    # others before concluding anything.
                    undecidable.append(
                        self._unordered_finding(v, commit)
                    )
                else:
                    # No event can invalidate this authorization: proven good.
                    return []

        proven = self._proven_invalidation_findings(
            candidates[0], commit
        )
        if proven:
            return proven
        # No surviving authorization and no proven invalidation: every
        # candidate is shadowed by an event whose position relative to the
        # validation cannot be established.
        return undecidable or [
            Finding(
                rule_id="commit_authorization_unprovable",
                verdict=Verdict.UNDECIDABLE,
                events=[commit.event_id]
                + [v.event_id for v in candidates],
                message=(
                    "no VALIDATE_OK can be proven valid at WRITE_COMMIT "
                    f"{commit.event_id}"
                ),
                unknown_reason="invalidation events lack ordering evidence",
            )
        ]

    def _unordered_invalidation(
        self, validation: Event, commit: Event
    ) -> bool:
        """An invalidation event before the commit but unordered relative to
        the validation, so it is unknown whether it voided this one."""
        for c in self.model.cancels_for(commit.handle or ""):
            if self.graph.hb(c.event_id, commit.event_id) and                     self._order_unknown(c, validation):
                return True
        for b in self.events:
            if (
                b.type == BIND
                and b.handle == validation.handle
                and (b.object_no, b.object_gen)
                != (validation.object_no, validation.object_gen)
                and self.graph.hb(b.event_id, commit.event_id)
                and self._order_unknown(b, validation)
            ):
                return True
        for r in self.model.retries_for(
            commit.handle or "", commit.task or ""
        ):
            if self.graph.hb(r.event_id, commit.event_id) and                     self._order_unknown(r, validation):
                return True
        return False

    def _unordered_finding(
        self, validation: Event, commit: Event
    ) -> Finding:
        return Finding(
            rule_id="commit_invalidation_unordered",
            verdict=Verdict.UNDECIDABLE,
            events=[validation.event_id, commit.event_id],
            message=(
                f"a cancel/rebind/retry is proven before WRITE_COMMIT "
                f"{commit.event_id} but has no ordering edge relative to "
                f"VALIDATE_OK {validation.event_id}; whether the "
                "authorization survived cannot be decided"
            ),
            unknown_reason=(
                "invalidation event and validation are concurrent in the "
                "happens-before graph"
            ),
        )

    def _proven_invalidation_findings(
        self, v: Event, commit: Event
    ) -> list[Finding]:
        out: list[Finding] = []
        # Retry subsumes the cancel that triggered it; report retry first.
        retry = self._retried_between(v, commit)
        rebind = self._rebound_between(v, commit)
        cancel = self._intervening_cancel(v, commit)
        if retry is not None:
            out.append(
                Finding(
                    rule_id="commit_after_retry_without_revalidation",
                    verdict=Verdict.VIOLATION,
                    events=[v.event_id, retry.event_id, commit.event_id],
                    message=(
                        f"RETRY {retry.event_id} sits between validation "
                        f"{v.event_id} and WRITE_COMMIT {commit.event_id} "
                        "but no VALIDATE_OK follows the retry"
                    ),
                    evidence=(
                        self.graph.explain(v.event_id, retry.event_id)
                        + self.graph.explain(
                            retry.event_id, commit.event_id
                        )[1:]
                    ),
                )
            )
            return out
        if rebind is not None:
            out.append(
                Finding(
                    rule_id="commit_stale_rebind",
                    verdict=Verdict.VIOLATION,
                    events=[v.event_id, rebind.event_id, commit.event_id],
                    message=(
                        f"handle was rebound to "
                        f"{rebind.object_no}@gen{rebind.object_gen} after "
                        f"validation {v.event_id}; WRITE_COMMIT "
                        f"{commit.event_id} is not covered by a validation "
                        "of the rebound identity"
                    ),
                    evidence=(
                        self.graph.explain(v.event_id, rebind.event_id)
                        + self.graph.explain(
                            rebind.event_id, commit.event_id
                        )[1:]
                    ),
                )
            )
            return out
        if cancel is not None:
            out.append(
                Finding(
                    rule_id="commit_after_cancel",
                    verdict=Verdict.VIOLATION,
                    events=[v.event_id, cancel.event_id, commit.event_id],
                    message=(
                        f"validation {v.event_id} was canceled by "
                        f"{cancel.event_id} before WRITE_COMMIT "
                        f"{commit.event_id}; no fresh validation follows the "
                        "cancel"
                    ),
                    evidence=(
                        self.graph.explain(v.event_id, cancel.event_id)
                        + self.graph.explain(
                            cancel.event_id, commit.event_id
                        )[1:]
                    ),
                )
            )
        return out

    def _intervening_cancel(
        self, validation: Event, commit: Event
    ) -> Event | None:
        for c in self.model.cancels_for(commit.handle or ""):
            if self.graph.hb(
                validation.event_id, c.event_id
            ) and self.graph.hb(c.event_id, commit.event_id):
                return c
        return None

    def _rebound_between(self, validation: Event, commit: Event) -> Event | None:
        for b in self.events:
            if (
                b.type == BIND
                and b.handle == validation.handle
                and (b.object_no, b.object_gen)
                != (validation.object_no, validation.object_gen)
                and self.graph.hb(validation.event_id, b.event_id)
                and self.graph.hb(b.event_id, commit.event_id)
            ):
                return b
        return None

    def _retried_between(
        self, validation: Event, commit: Event
    ) -> Event | None:
        """A RETRY strictly between validation and commit that is not healed
        by a later, commit-ordered validation of the committed identity."""
        for r in self.model.retries_for(
            commit.handle or "", commit.task or ""
        ):
            if not self.graph.hb(validation.event_id, r.event_id):
                continue
            if not self.graph.hb(r.event_id, commit.event_id):
                continue
            healed = any(
                self.graph.hb(r.event_id, v.event_id)
                and self.graph.hb(v.event_id, commit.event_id)
                and (v.object_no, v.object_gen)
                == (commit.object_no, commit.object_gen)
                for v in self.model.validations_for(
                    commit.handle or "", commit.task or ""
                )
            )
            if not healed:
                return r
        return None

    def _unknown_commit(
        self,
        commit: Event,
        rule_id: str,
        message: str,
        extra: list[str] | None = None,
    ) -> None:
        events = [commit.event_id] + (extra or [])
        self.findings.append(
            Finding(
                rule_id=rule_id,
                verdict=Verdict.UNDECIDABLE,
                events=events,
                message=message,
                evidence=self._dangling_note(commit),
                unknown_reason=(
                    "no happens-before evidence; required synchronization "
                    "record is missing"
                ),
            )
        )

    # ---------------------------------------------------------------- cancel
    def _check_cancel(self, e: Event) -> None:
        if not self._check_handle_exists(e):
            return
        # Cancellation is a valid lifecycle transition at any point after
        # creation; only the create order is worth checking.
        create = self._creates[e.handle][-1]
        if not self.graph.hb(create.event_id, e.event_id):
            if self._order_unknown(create, e):
                self.findings.append(
                    Finding(
                        rule_id="cancel_create_unordered",
                        verdict=Verdict.UNDECIDABLE,
                        events=[create.event_id, e.event_id],
                        message=(
                            f"cannot prove HANDLE_CREATE {create.event_id} "
                            f"precedes CANCEL {e.event_id}"
                        ),
                        unknown_reason="no ordering edge between the events",
                    )
                )
            else:
                self.findings.append(
                    Finding(
                        rule_id="cancel_before_create",
                        verdict=Verdict.VIOLATION,
                        events=[e.event_id, create.event_id],
                        message=(
                            f"CANCEL {e.event_id} is ordered before "
                            f"HANDLE_CREATE {create.event_id}"
                        ),
                    )
                )

    # ----------------------------------------------------------------- retry
    def _check_retry(self, e: Event) -> None:
        if not self._check_handle_exists(e):
            return
        cancels = self.model.cancels_for(e.handle or "")
        if not cancels:
            verdict = Verdict.UNDECIDABLE if self.graph.dangling_for(e) \
                else Verdict.VIOLATION
            self.findings.append(
                Finding(
                    rule_id="retry_without_cancel",
                    verdict=verdict,
                    events=[e.event_id],
                    message=(
                        f"RETRY {e.event_id} has no preceding CANCEL on "
                        f"handle {e.handle!r}; retry must follow cancellation"
                    ),
                    evidence=self._dangling_note(e),
                    unknown_reason=(
                        "CANCEL record may be among the missing sync logs"
                        if verdict == Verdict.UNDECIDABLE
                        else None
                    ),
                )
            )
            return
        if any(self.graph.hb(c.event_id, e.event_id) for c in cancels):
            return
        unordered = [c for c in cancels if self._order_unknown(c, e)]
        if unordered:
            self.findings.append(
                Finding(
                    rule_id="retry_cancel_unordered",
                    verdict=Verdict.UNDECIDABLE,
                    events=[e.event_id] + [c.event_id for c in unordered],
                    message=(
                        f"cannot prove any CANCEL precedes RETRY {e.event_id}"
                    ),
                    evidence=self._dangling_note(e),
                    unknown_reason="no ordering edge between retry and cancel",
                )
            )
        else:
            self.findings.append(
                Finding(
                    rule_id="retry_before_cancel",
                    verdict=Verdict.VIOLATION,
                    events=[e.event_id, cancels[0].event_id],
                    message=(
                        f"RETRY {e.event_id} is ordered before every CANCEL "
                        f"of handle {e.handle!r}"
                    ),
                )
            )

    # ------------------------------------------------------------------- run
    def run(self) -> AuditReport:
        self._structural()
        for e in self.events:
            if e.type == BIND:
                self._check_bind(e)
            elif e.type == VALIDATE_OK:
                self._check_validate(e)
            elif e.type == WRITE_COMMIT:
                self._check_commit(e)
            elif e.type == CANCEL:
                self._check_cancel(e)
            elif e.type == RETRY:
                self._check_retry(e)
            # SYNC and HANDLE_CREATE only contribute edges/state.

        # Deterministic ordering for stable diffs across shuffled inputs.
        self.findings.sort(
            key=lambda f: (
                f.verdict.value != "VIOLATION",
                f.rule_id,
                tuple(f.events),
                f.message,
            )
        )

        summary: dict[str, int] = {
            Verdict.VIOLATION.value: 0,
            Verdict.UNDECIDABLE.value: 0,
        }
        for f in self.findings:
            summary[f.verdict.value] = summary.get(f.verdict.value, 0) + 1

        # Aggregation: a proven violation dominates any unrelated
        # undecidable records; UNDECIDABLE only wins when nothing was proven.
        if summary.get(Verdict.VIOLATION.value):
            verdict = Verdict.VIOLATION
        elif summary.get(Verdict.UNDECIDABLE.value):
            verdict = Verdict.UNDECIDABLE
        else:
            verdict = Verdict.PASS

        structural = [
            f"cycle: {' -> '.join(chain)}" for chain in self.graph.cycles
        ] + [
            f"dangling {label} edge on {ev.event_id} -> {ref}"
            for ev, ref, label in self.graph.dangling_edges
        ]

        stats = {
            "events": len(self.events),
            "handles": len(self.model.handles),
            "objects": len(self.model.objects),
            "hb_edges": sum(
                len(preds) for preds in self.graph._pred.values()
            ),
        }
        return AuditReport(
            verdict=verdict,
            findings=self.findings,
            summary=summary,
            stats=stats,
            structural_issues=structural,
        )


def audit(events: list[Event]) -> AuditReport:
    return Verifier(events).run()
