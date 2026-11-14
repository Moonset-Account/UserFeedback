"""Local quarantine state module (SEC-20261114-018).

Models per-recipient email quarantine:

* Delivery records are located by (email, recipient) and carry the current
  content version plus the set of hit quarantine reasons.
* Approval events must match the same recipient and the same version, and a
  release task becomes releasable only when valid approvals cover all still
  active reasons.
* Appending new reasons (or bumping the version) sends an existing release
  task back to waiting-for-review, because coverage is computed against the
  current record.
* Release requests are idempotent per request_id; a second independent
  request_id is recorded but never emits a duplicate downstream
  confirmation.  Downstream confirmations themselves are deduplicated.
* Operators can inspect per-recipient waiting / releasable / released state
  for one email.

State is held in memory only; the downstream mail system is simulated by
confirmation records kept inside the store.
"""

WAITING = "waiting"
RELEASABLE = "releasable"
RELEASED = "released"


class QuarantineError(Exception):
    """Raised for invalid operations against the quarantine store."""


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise QuarantineError("%s must be a non-empty string" % field)
    return value


def _require_reasons(reasons):
    if not isinstance(reasons, (list, tuple)) or not reasons:
        raise QuarantineError("reasons must be a non-empty list")
    seen = []
    for reason in reasons:
        if not isinstance(reason, str) or not reason.strip():
            raise QuarantineError("each reason must be a non-empty string")
        if reason in seen:
            raise QuarantineError("duplicate reason: %r" % reason)
        seen.append(reason)
    return frozenset(seen)


class _Record(object):
    __slots__ = ("version", "reasons")


class _Approval(object):
    __slots__ = ("version", "reasons")


class _ReleaseRequest(object):
    __slots__ = ("version", "executed", "confirmation_id")


class QuarantineStore(object):
    """In-memory quarantine state for (email, recipient) pairs."""

    def __init__(self):
        self._records = {}           # (email, recipient) -> _Record
        self._approvals = {}         # (email, recipient) -> {approval_id: _Approval}
        self._release_requests = {}  # (email, recipient) -> {request_id: _ReleaseRequest}
        self._confirmations = {}     # (email, recipient) -> {confirmation_id: dict}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _key(email_id, recipient):
        return (
            _require_text(email_id, "email"),
            _require_text(recipient, "recipient"),
        )

    def _get_record(self, key):
        record = self._records.get(key)
        if record is None:
            raise QuarantineError(
                "no quarantine record for email=%r recipient=%r" % key
            )
        return record

    def _covered_reasons(self, key):
        """Reasons covered by approvals valid for the current version."""
        record = self._records[key]
        covered = set()
        for approval in self._approvals.get(key, {}).values():
            if approval.version == record.version:
                covered |= approval.reasons
        return covered

    def _executed_versions(self, key):
        return {
            request.version
            for request in self._release_requests.get(key, {}).values()
            if request.executed
        }

    def _state(self, key):
        record = self._records[key]
        if record.version in self._executed_versions(key):
            return RELEASED
        if not (record.reasons - self._covered_reasons(key)):
            return RELEASABLE
        return WAITING

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def record_delivery(self, email_id, recipient, version, reasons):
        """Record (or update) a quarantined delivery for one recipient.

        ``reasons`` is the full current set of hit quarantine reasons.
        Appending new reasons or changing the version makes any pending
        release task fall back to ``waiting``, because approvals are
        version-scoped and must cover all currently active reasons.
        """
        key = self._key(email_id, recipient)
        version = _require_text(version, "version")
        reason_set = _require_reasons(reasons)
        previous = self._records.get(key)
        record = _Record()
        record.version = version
        record.reasons = reason_set
        self._records[key] = record
        return {
            "email": key[0],
            "recipient": key[1],
            "version": version,
            "reasons": sorted(reason_set),
            "previous_version": previous.version if previous else None,
            "state": self._state(key),
        }

    def submit_approval(self, email_id, recipient, approval_id, version, reasons):
        """Record an approval event for one recipient and one version.

        The approval is rejected unless a record exists and ``version``
        matches the record's current version.  Re-submitting the same
        ``approval_id`` with the same payload is an idempotent replay; the
        same id with a different payload is a conflict.
        """
        key = self._key(email_id, recipient)
        approval_id = _require_text(approval_id, "approval_id")
        version = _require_text(version, "version")
        reason_set = _require_reasons(reasons)
        record = self._get_record(key)
        approvals = self._approvals.setdefault(key, {})
        if approval_id in approvals:
            existing = approvals[approval_id]
            if existing.version != version or existing.reasons != reason_set:
                raise QuarantineError(
                    "approval %r already exists with a different payload"
                    % approval_id
                )
            return self._approval_view(key, approval_id, replay=True)
        if version != record.version:
            raise QuarantineError(
                "stale approval version %r: current version is %r"
                % (version, record.version)
            )
        approval = _Approval()
        approval.version = version
        approval.reasons = reason_set
        approvals[approval_id] = approval
        return self._approval_view(key, approval_id, replay=False)

    def _approval_view(self, key, approval_id, replay):
        record = self._records[key]
        covered = self._covered_reasons(key)
        return {
            "email": key[0],
            "recipient": key[1],
            "approval_id": approval_id,
            "version": record.version,
            "covered_reasons": sorted(covered & record.reasons),
            "missing_reasons": sorted(record.reasons - covered),
            "state": self._state(key),
            "idempotent_replay": replay,
        }

    def release(self, email_id, recipient, request_id):
        """Request release of one recipient's quarantined copy.

        * Replaying an already-seen ``request_id`` returns the recorded
          outcome without any new downstream effect (idempotent retry).
        * A new ``request_id`` while releasable executes the release and
          emits exactly one downstream confirmation.
        * A new ``request_id`` after the copy is already released is a
          second independent application: it is recorded, but no duplicate
          downstream confirmation is emitted.
        """
        key = self._key(email_id, recipient)
        request_id = _require_text(request_id, "request_id")
        record = self._get_record(key)
        requests = self._release_requests.setdefault(key, {})
        if request_id in requests:
            request = requests[request_id]
            return {
                "email": key[0],
                "recipient": key[1],
                "request_id": request_id,
                "state": self._state(key),
                "executed": request.executed,
                "confirmation_id": request.confirmation_id,
                "downstream_confirmation_sent": False,
                "duplicate_of_prior_release": not request.executed,
                "idempotent_replay": True,
            }
        state = self._state(key)
        if state == WAITING:
            missing = sorted(record.reasons - self._covered_reasons(key))
            raise QuarantineError(
                "not releasable: missing approval coverage for %r" % missing
            )
        request = _ReleaseRequest()
        request.version = record.version
        if state == RELEASABLE:
            confirmation_id = "cfm:%s:%s:%s" % (key[0], key[1], record.version)
            request.executed = True
            request.confirmation_id = confirmation_id
            self._confirmations.setdefault(key, {})[confirmation_id] = {
                "confirmation_id": confirmation_id,
                "request_id": request_id,
                "version": record.version,
                "acknowledged": False,
            }
            sent = True
            duplicate = False
        else:  # already RELEASED by an earlier independent request
            request.executed = False
            request.confirmation_id = None
            sent = False
            duplicate = True
        requests[request_id] = request
        return {
            "email": key[0],
            "recipient": key[1],
            "request_id": request_id,
            "state": self._state(key),
            "executed": request.executed,
            "confirmation_id": request.confirmation_id,
            "downstream_confirmation_sent": sent,
            "duplicate_of_prior_release": duplicate,
            "idempotent_replay": False,
        }

    def confirm_downstream(self, email_id, recipient, confirmation_id):
        """Record a downstream release confirmation exactly once."""
        key = self._key(email_id, recipient)
        confirmation_id = _require_text(confirmation_id, "confirmation_id")
        self._get_record(key)
        confirmation = self._confirmations.get(key, {}).get(confirmation_id)
        if confirmation is None:
            raise QuarantineError(
                "unknown confirmation id %r" % confirmation_id
            )
        if confirmation["acknowledged"]:
            return {
                "email": key[0],
                "recipient": key[1],
                "confirmation_id": confirmation_id,
                "recorded": False,
                "duplicate": True,
                "acknowledged_count": 1,
            }
        confirmation["acknowledged"] = True
        return {
            "email": key[0],
            "recipient": key[1],
            "confirmation_id": confirmation_id,
            "recorded": True,
            "duplicate": False,
            "acknowledged_count": 1,
        }

    # ------------------------------------------------------------------
    # operator views
    # ------------------------------------------------------------------
    def mail_status(self, email_id):
        """Per-recipient waiting/releasable/released view for one email."""
        email_id = _require_text(email_id, "email")
        recipients = {}
        for key in sorted(self._records):
            if key[0] != email_id:
                continue
            record = self._records[key]
            covered = self._covered_reasons(key)
            recipients[key[1]] = {
                "state": self._state(key),
                "version": record.version,
                "reasons": sorted(record.reasons),
                "covered_reasons": sorted(covered & record.reasons),
                "missing_reasons": sorted(record.reasons - covered),
                "approvals": sorted(self._approvals.get(key, {})),
                "release_requests": [
                    {
                        "request_id": request_id,
                        "version": request.version,
                        "executed": request.executed,
                    }
                    for request_id, request in sorted(
                        self._release_requests.get(key, {}).items()
                    )
                ],
                "confirmations": [
                    {
                        "confirmation_id": item["confirmation_id"],
                        "acknowledged": item["acknowledged"],
                    }
                    for item in self._confirmations.get(key, {}).values()
                ],
            }
        if not recipients:
            raise QuarantineError("unknown email %r" % email_id)
        return {"email": email_id, "recipients": recipients}
