"""Reusable scenario definitions for ``Envelope.verify`` threshold tests.

This module is test-only evidence code.  It does **not** import or modify any
production verification implementation; it constructs inputs and calls the
public :meth:`securesystemslib.dsse.Envelope.verify` API.

The scenarios are shared by two entry points so that the unittest integration
and the local replay harness execute exactly the same code and assertions:

* ``tests/test_dsse.py`` -- unittest integration
* ``tools/local_verify.py`` -- local replay/verification entry point

Contract under test (baseline of ``securesystemslib/dsse.py``):

1. A signature is only considered when a candidate ``Key`` has the same
   ``keyid`` as the ``Signature``.  Unrelated keyids are skipped without ever
   calling ``verify_signature``.
2. For a matching keyid, candidate keys are tried in list order.  The first
   candidate whose ``verify_signature`` returns is accepted; an
   ``UnverifiedSignatureError`` causes the next candidate to be tried.
3. Acceptance is counted per **unique accepted keyid**.  Duplicate key
   objects (same keyid) are never counted as independent signatures.
4. The outer signature loop stops as soon as the threshold of accepted
   unique keyids is reached.
5. Invalid thresholds raise ``ValueError`` before any signature is inspected;
   an unmet threshold raises ``VerificationError``.  Only
   ``UnverifiedSignatureError`` (and its ``VerificationError`` subclass) is
   caught inside the candidate loop; other exceptions propagate unchanged.
"""

from __future__ import annotations

import base64
import copy
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from securesystemslib.dsse import Envelope
from securesystemslib.exceptions import (
    UnverifiedSignatureError,
    VerificationError,
)
from securesystemslib.signer import CryptoSigner, Key, Signature

PEMS_DIR = Path(__file__).parent / "data" / "pems"

# Fixed, synthetic payload/payload_type shared by the double-based scenarios.
PAYLOAD = b"hello world"
PAYLOAD_TYPE = "http://example.com/HelloWorld"

# Exact contract strings emitted by the baseline implementation.
THRESHOLD_ZERO_MSG = "Threshold must be greater than 0"
KEYS_BELOW_THRESHOLD_MSG = "Number of keys can't be less than threshold"
UNMET_MSG_TEMPLATE = "Accepted signatures do not match threshold, Found: {found}, Expected {expected}"


class Checker(Protocol):
    """Minimal assertion surface shared by the unittest and replay adapters."""

    def assert_equal(self, actual: Any, expected: Any, msg: str) -> None: ...

    def assert_true(self, value: bool, msg: str) -> None: ...

    def assert_in(self, member: Any, container: Any, msg: str) -> None: ...

    def assert_raises(
        self, exception: type[BaseException], msg: str
    ) -> AbstractContextManager[Any]: ...


@dataclass
class Call:
    """One recorded ``verify_signature`` invocation on a key double."""

    key_keyid: str
    signature_keyid: str
    outcome: str  # "accepted" or "rejected"
    pae: bytes


class RecordingKey(Key):
    """Countable ``Key`` test double with an explicit accept/reject rule.

    The double performs no cryptography.  It accepts signatures whose keyid is
    listed in ``accept_sigs`` and raises ``fail_with`` for every other
    signature.  Every invocation is appended to ``calls`` so tests can prove
    which candidates were tried, in which order and with which PAE bytes.

    Args:
        keyid: keyid reported by the double (used for envelope matching).
        accept_sigs: signature keyids for which verification succeeds.
        fail_with: exception raised on rejection; defaults to the failure
            type that ``Envelope.verify`` catches and continues on.
        label: human-readable name used in evidence output.
    """

    def __init__(
        self,
        keyid: str,
        accept_sigs: list[str] | None = None,
        fail_with: type[BaseException] = UnverifiedSignatureError,
        label: str | None = None,
    ):
        super().__init__(keyid, "fake-keytype", "fake-scheme", {"public": keyid})
        self.accept_sigs = set(accept_sigs or [])
        self.fail_with = fail_with
        self.label = label or keyid
        self.calls: list[Call] = []

    @classmethod
    def from_dict(cls, keyid: str, key_dict: dict[str, Any]) -> "RecordingKey":
        return cls(keyid, list(key_dict.get("accept_sigs", [])))

    def to_dict(self) -> dict[str, Any]:
        return self._to_dict()

    def verify_signature(self, signature: Signature, data: bytes) -> None:
        if signature.keyid in self.accept_sigs:
            self.calls.append(
                Call(self.keyid, signature.keyid, "accepted", bytes(data))
            )
            return

        self.calls.append(
            Call(self.keyid, signature.keyid, "rejected", bytes(data))
        )
        raise self.fail_with(
            f"recording key {self.label!r} rejects signature {signature.keyid!r}"
        )

    def call_keyids(self) -> list[str]:
        """keyids of the signatures this key was asked to verify, in order."""
        return [call.signature_keyid for call in self.calls]

    def outcomes(self) -> list[str]:
        return [call.outcome for call in self.calls]


# ---------------------------------------------------------------------------
# Input builders
# ---------------------------------------------------------------------------


def make_signature(keyid: str, sig_hex: str | None = None) -> Signature:
    """Create a synthetic signature; default content derives from the keyid."""
    if sig_hex is None:
        sig_hex = f"sig-{keyid}".encode("utf-8").hex()
    return Signature(keyid, sig_hex)


def build_envelope(
    signatures: list[Signature],
    payload: bytes = PAYLOAD,
    payload_type: str = PAYLOAD_TYPE,
) -> Envelope:
    """Build an envelope directly, preserving signature insertion order."""
    return Envelope(
        payload,
        payload_type,
        {signature.keyid: signature for signature in signatures},
    )


def envelope_dict(
    signatures: list[Signature],
    payload: bytes = PAYLOAD,
    payload_type: str = PAYLOAD_TYPE,
) -> dict[str, Any]:
    """Build a serializable envelope dict (the ``from_dict`` input shape)."""
    return {
        "payload": base64.standard_b64encode(payload).decode("utf-8"),
        "payloadType": payload_type,
        "signatures": [
            {
                "keyid": signature.keyid,
                "sig": base64.standard_b64encode(
                    bytes.fromhex(signature.signature)
                ).decode("utf-8"),
            }
            for signature in signatures
        ],
    }


def build_envelope_via_dict(
    signatures: list[Signature],
    payload: bytes = PAYLOAD,
    payload_type: str = PAYLOAD_TYPE,
) -> Envelope:
    """Round-trip a freshly built dict through ``Envelope.from_dict``."""
    return Envelope.from_dict(
        envelope_dict(signatures, payload, payload_type)
    )


def expect_raises(
    checker: Checker,
    exception: type[BaseException],
    callable_: Callable[..., Any],
    *args: Any,
    msg_contains: str | None = None,
    label: str,
    **kwargs: Any,
) -> Any:
    """Assert that ``callable_`` raises ``exception``; return the context."""
    with checker.assert_raises(exception, label) as context:
        callable_(*args, **kwargs)
    if msg_contains is not None:
        checker.assert_in(
            msg_contains,
            str(context.exception),
            f"{label}: exception message mentions {msg_contains!r}",
        )
    return context


@dataclass
class ScenarioResult:
    """Evidence record returned by every scenario."""

    name: str
    seed: dict[str, Any]
    expected: dict[str, Any]
    observed: dict[str, Any]


@dataclass
class Scenario:
    name: str
    description: str
    func: Callable[[Checker], ScenarioResult]


def _key_seed(keys: list[RecordingKey]) -> list[dict[str, Any]]:
    return [
        {
            "label": key.label,
            "keyid": key.keyid,
            "accept_sigs": sorted(key.accept_sigs),
            "fail_with": key.fail_with.__name__,
        }
        for key in keys
    ]


def _call_log(keys: list[RecordingKey]) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    for key in keys:
        for call in key.calls:
            log.append(
                {
                    "key": key.label,
                    "keyid": call.key_keyid,
                    "signature": call.signature_keyid,
                    "outcome": call.outcome,
                }
            )
    return log


# ---------------------------------------------------------------------------
# C01: keyid matching and unique-accepted-keyid threshold contract
# ---------------------------------------------------------------------------


def c01_unrelated_keyid_is_never_called(checker: Checker) -> ScenarioResult:
    """Unrelated keyids gate verification: no call, threshold unmet."""
    sig_a = make_signature("a")
    sig_b = make_signature("b")
    envelope = build_envelope([sig_a, sig_b])

    # Key "x" matches neither signature keyid.
    unrelated = RecordingKey("x", accept_sigs=["a"], label="x-unrelated")
    expect_raises(
        checker,
        VerificationError,
        envelope.verify,
        [unrelated],
        1,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=0, expected=1),
        label="C01 unrelated keyid cannot reach threshold",
    )
    checker.assert_equal(
        len(unrelated.calls),
        0,
        "C01 key with unrelated keyid is never asked to verify",
    )

    # A matching keyid that still rejects produces the same unmet threshold.
    matching_reject = RecordingKey("b", accept_sigs=[], label="b-rejects")
    envelope_2 = build_envelope([sig_a, sig_b])
    expect_raises(
        checker,
        VerificationError,
        envelope_2.verify,
        [matching_reject],
        1,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=0, expected=1),
        label="C01 matching keyid with failed verification",
    )
    checker.assert_equal(
        matching_reject.call_keyids(),
        ["b"],
        "C01 matching keyid is tried exactly once against its signature",
    )
    checker.assert_equal(
        matching_reject.outcomes(),
        ["rejected"],
        "C01 rejection outcome recorded",
    )

    return ScenarioResult(
        name="c01_unrelated_keyid_is_never_called",
        seed={
            "signature_keyids": ["a", "b"],
            "keys": _key_seed([unrelated, matching_reject]),
            "threshold": 1,
        },
        expected={
            "unrelated_call_count": 0,
            "matching_calls": ["b"],
            "error": "VerificationError Found: 0, Expected 1",
        },
        observed={
            "unrelated_call_count": len(unrelated.calls),
            "matching_calls": matching_reject.call_keyids(),
            "call_log": _call_log([unrelated, matching_reject]),
        },
    )


def c01_unique_keyid_count_not_candidate_objects(
    checker: Checker,
) -> ScenarioResult:
    """Two key objects with one keyid count once; threshold 2 stays unmet."""
    sig_a = make_signature("a")

    # Every trial gets fresh candidate objects so the recorded call log
    # reflects that trial alone instead of accumulating across trials.
    first = RecordingKey("a", accept_sigs=["a"], label="a-first")
    second = RecordingKey("a", accept_sigs=["a"], label="a-second")
    candidates = [first, second]

    envelope = build_envelope([sig_a])
    accepted = envelope.verify(candidates, 1)
    checker.assert_equal(
        sorted(accepted.keys()),
        ["a"],
        "C01 threshold 1 accepted under keyid a",
    )
    checker.assert_equal(
        len(accepted),
        1,
        "C01 one unique keyid accepted despite two candidate objects",
    )
    checker.assert_true(
        accepted["a"] is first,
        "C01 first accepting candidate is the returned key",
    )
    checker.assert_equal(
        first.call_keyids(),
        ["a"],
        "C01 candidate loop breaks on first success, second object untouched",
    )
    checker.assert_equal(
        len(second.calls),
        0,
        "C01 second same-keyid candidate is not tried after success",
    )

    # Threshold 2 with only one unique accepted keyid must fail even though
    # the key list contains two candidate objects.
    first_t2 = RecordingKey("a", accept_sigs=["a"], label="a-first-t2")
    second_t2 = RecordingKey("a", accept_sigs=["a"], label="a-second-t2")
    envelope_2 = build_envelope([sig_a])
    expect_raises(
        checker,
        VerificationError,
        envelope_2.verify,
        [first_t2, second_t2],
        2,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=1, expected=2),
        label="C01 candidate objects are not counted as independent signatures",
    )

    return ScenarioResult(
        name="c01_unique_keyid_count_not_candidate_objects",
        seed={
            "signature_keyids": ["a"],
            "keys": _key_seed(candidates + [first_t2, second_t2]),
            "thresholds": [1, 2],
        },
        expected={
            "accepted_at_threshold_1": ["a"],
            "first_calls": ["a"],
            "second_calls": [],
            "threshold_2_error": "VerificationError Found: 1, Expected 2",
        },
        observed={
            "accepted_at_threshold_1": sorted(accepted.keys()),
            "first_calls": first.call_keyids(),
            "second_calls": second.call_keyids(),
            "call_log": _call_log(candidates),
        },
    )


def c01_threshold_validation(checker: Checker) -> ScenarioResult:
    """Invalid thresholds raise ValueError before any signature is inspected."""
    sig_a = make_signature("a")
    key = RecordingKey("a", accept_sigs=["a"], label="a-ok")
    envelope = build_envelope([sig_a])

    expect_raises(
        checker,
        ValueError,
        envelope.verify,
        [key],
        0,
        msg_contains=THRESHOLD_ZERO_MSG,
        label="C01 threshold zero",
    )
    envelope_2 = build_envelope([sig_a])
    expect_raises(
        checker,
        ValueError,
        envelope_2.verify,
        [key],
        -3,
        msg_contains=THRESHOLD_ZERO_MSG,
        label="C01 negative threshold",
    )
    envelope_3 = build_envelope([sig_a])
    expect_raises(
        checker,
        ValueError,
        envelope_3.verify,
        [key],
        2,
        msg_contains=KEYS_BELOW_THRESHOLD_MSG,
        label="C01 more threshold than keys",
    )
    checker.assert_equal(
        len(key.calls),
        0,
        "C01 threshold validation happens before signature iteration",
    )

    return ScenarioResult(
        name="c01_threshold_validation",
        seed={
            "signature_keyids": ["a"],
            "keys": _key_seed([key]),
            "thresholds": [0, -3, 2],
            "key_count": 1,
        },
        expected={
            "zero_or_negative": f"ValueError {THRESHOLD_ZERO_MSG}",
            "keys_below_threshold": f"ValueError {KEYS_BELOW_THRESHOLD_MSG}",
            "verify_signature_calls": 0,
        },
        observed={
            "verify_signature_calls": len(key.calls),
            "call_log": _call_log([key]),
        },
    )


# ---------------------------------------------------------------------------
# C02: same-keyid candidate handling, failover and ordering
# ---------------------------------------------------------------------------


def c02_failover_to_later_candidate(checker: Checker) -> ScenarioResult:
    """Failing first candidate, later same-keyid candidate succeeds."""
    sig_a = make_signature("a")
    bad = RecordingKey("a", accept_sigs=[], label="a-bad")
    good = RecordingKey("a", accept_sigs=["a"], label="a-good")

    envelope = build_envelope([sig_a])
    accepted = envelope.verify([bad, good], 1)
    checker.assert_true(
        accepted["a"] is good,
        "C02 later candidate succeeds after first candidate rejects",
    )
    checker.assert_equal(
        bad.outcomes(),
        ["rejected"],
        "C02 first candidate rejection is swallowed",
    )
    checker.assert_equal(
        good.outcomes(),
        ["accepted"],
        "C02 second candidate acceptance recorded",
    )

    # Swap candidate order: the accepting candidate is now first and the bad
    # one must never be tried.
    bad_2 = RecordingKey("a", accept_sigs=[], label="a-bad-2")
    good_2 = RecordingKey("a", accept_sigs=["a"], label="a-good-2")
    envelope_2 = build_envelope([sig_a])
    accepted_2 = envelope_2.verify([good_2, bad_2], 1)
    checker.assert_true(
        accepted_2["a"] is good_2,
        "C02 first accepting candidate wins in swapped order",
    )
    checker.assert_equal(
        len(bad_2.calls),
        0,
        "C02 remaining candidates skipped once a candidate accepts",
    )
    checker.assert_equal(
        good_2.call_keyids(),
        ["a"],
        "C02 accepting candidate called once",
    )

    return ScenarioResult(
        name="c02_failover_to_later_candidate",
        seed={
            "signature_keyids": ["a"],
            "keys": _key_seed([bad, good, bad_2, good_2]),
            "candidate_orders": [["a-bad", "a-good"], ["a-good-2", "a-bad-2"]],
        },
        expected={
            "first_order_calls": [
                ("a-bad", "a", "rejected"),
                ("a-good", "a", "accepted"),
            ],
            "second_order_bad_calls": 0,
        },
        observed={
            "first_order_log": _call_log([bad, good]),
            "second_order_log": _call_log([good_2, bad_2]),
        },
    )


def c02_all_candidates_fail(checker: Checker) -> ScenarioResult:
    """Every same-keyid candidate is tried; unmet threshold raises."""
    sig_a = make_signature("a")
    first = RecordingKey("a", accept_sigs=[], label="a-fail-1")
    second = RecordingKey("a", accept_sigs=[], label="a-fail-2")
    envelope = build_envelope([sig_a])

    expect_raises(
        checker,
        VerificationError,
        envelope.verify,
        [first, second],
        1,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=0, expected=1),
        label="C02 all candidates reject",
    )
    checker.assert_equal(
        first.call_keyids(),
        ["a"],
        "C02 first rejecting candidate was tried",
    )
    checker.assert_equal(
        second.call_keyids(),
        ["a"],
        "C02 failover continues to the second candidate",
    )
    checker.assert_equal(
        [call.outcome for call in first.calls + second.calls],
        ["rejected", "rejected"],
        "C02 both candidates recorded rejections",
    )

    return ScenarioResult(
        name="c02_all_candidates_fail",
        seed={
            "signature_keyids": ["a"],
            "keys": _key_seed([first, second]),
            "threshold": 1,
        },
        expected={"calls_per_candidate": ["a", "a"], "accepted": []},
        observed={
            "call_log": _call_log([first, second]),
            "first_calls": first.call_keyids(),
            "second_calls": second.call_keyids(),
        },
    )


def c02_duplicate_candidate_object(checker: Checker) -> ScenarioResult:
    """The same key object listed twice is invoked once and counted once."""
    sig_a = make_signature("a")
    good = RecordingKey("a", accept_sigs=["a"], label="a-dup")
    envelope = build_envelope([sig_a])

    accepted = envelope.verify([good, good], 1)
    checker.assert_equal(
        len(accepted),
        1,
        "C02 duplicate object yields one accepted keyid",
    )
    checker.assert_equal(
        len(good.calls),
        1,
        "C02 duplicate candidate slot not reached after first acceptance",
    )

    # Fresh object for the second trial so the invocation count stays
    # attributable to this trial alone.
    good_t2 = RecordingKey("a", accept_sigs=["a"], label="a-dup-t2")
    envelope_2 = build_envelope([sig_a])
    expect_raises(
        checker,
        VerificationError,
        envelope_2.verify,
        [good_t2, good_t2],
        2,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=1, expected=2),
        label="C02 duplicate object cannot satisfy threshold 2",
    )

    return ScenarioResult(
        name="c02_duplicate_candidate_object",
        seed={
            "signature_keyids": ["a"],
            "keys": _key_seed([good, good_t2]),
            "candidate_list": ["a-dup", "a-dup"],
            "thresholds": [1, 2],
        },
        expected={
            "accepted_at_threshold_1": 1,
            "verify_signature_invocations": 1,
            "threshold_2_error": "VerificationError Found: 1, Expected 2",
        },
        observed={
            "accepted_at_threshold_1": len(accepted),
            "verify_signature_invocations": len(good.calls),
            "call_log": _call_log([good]),
        },
    )


def c02_signature_insertion_order(checker: Checker) -> ScenarioResult:
    """Signature processing follows insertion order on both build paths."""
    sigs = {keyid: make_signature(keyid) for keyid in ["a", "b", "c"]}
    keys = [
        RecordingKey("a", accept_sigs=["a"], label="key-a"),
        RecordingKey("b", accept_sigs=["b"], label="key-b"),
        RecordingKey("c", accept_sigs=["c"], label="key-c"),
    ]
    key_by_label = {key.label: key for key in keys}

    # Direct construction with insertion order a, b, c.
    envelope = build_envelope([sigs["a"], sigs["b"], sigs["c"]])
    accepted = envelope.verify(keys, 2)
    checker.assert_equal(
        sorted(accepted.keys()),
        ["a", "b"],
        "C02 first two inserted signatures meet threshold 2",
    )
    checker.assert_equal(
        key_by_label["key-c"].calls,
        [],
        "C02 later signature not inspected once threshold reached",
    )
    direct_order = [entry["key"] for entry in _call_log(keys)]

    # Reversed insertion order via the serialization/from_dict path.  Each
    # trial gets a freshly built dict so inputs never alias each other.
    reversed_keys = [
        RecordingKey("c", accept_sigs=["c"], label="rev-c"),
        RecordingKey("b", accept_sigs=["b"], label="rev-b"),
        RecordingKey("a", accept_sigs=["a"], label="rev-a"),
    ]
    envelope_rev = build_envelope_via_dict(
        [sigs["c"], sigs["b"], sigs["a"]]
    )
    accepted_rev = envelope_rev.verify(reversed_keys, 2)
    checker.assert_equal(
        sorted(accepted_rev.keys()),
        ["b", "c"],
        "C02 reversed insertion order changes accepted prefix",
    )
    rev_log = _call_log(reversed_keys)
    checker.assert_equal(
        [entry["key"] for entry in rev_log],
        ["rev-c", "rev-b"],
        "C02 from_dict preserves serialized signature order",
    )
    rev_by_label = {key.label: key for key in reversed_keys}
    checker.assert_equal(
        rev_by_label["rev-a"].calls,
        [],
        "C02 last inserted signature skipped after threshold",
    )

    return ScenarioResult(
        name="c02_signature_insertion_order",
        seed={
            "direct_order": ["a", "b", "c"],
            "reversed_order": ["c", "b", "a"],
            "keys": _key_seed(keys + reversed_keys),
            "threshold": 2,
        },
        expected={
            "direct_call_order": ["key-a", "key-b"],
            "reversed_call_order": ["rev-c", "rev-b"],
        },
        observed={
            "direct_call_order": direct_order,
            "reversed_call_order": [entry["key"] for entry in rev_log],
        },
    )


# ---------------------------------------------------------------------------
# C03: stop position, exception types and per-trial input independence
# ---------------------------------------------------------------------------


def c03_stop_position_at_threshold(checker: Checker) -> ScenarioResult:
    """The signature loop stops exactly when the threshold is reached."""
    sigs = [make_signature(keyid) for keyid in ["a", "b", "c", "d"]]

    def fresh_keys(prefix: str) -> list[RecordingKey]:
        return [
            RecordingKey(
                keyid,
                accept_sigs=[keyid],
                label=f"{prefix}-{keyid}",
            )
            for keyid in ["a", "b", "c", "d"]
        ]

    # Threshold 3: the fourth signature must never be inspected.
    keys_3 = fresh_keys("t3")
    by_label_3 = {key.label: key for key in keys_3}
    envelope = build_envelope(sigs)
    accepted = envelope.verify(keys_3, 3)
    checker.assert_equal(
        sorted(accepted.keys()),
        ["a", "b", "c"],
        "C03 threshold 3 accepts exactly the first three keyids",
    )
    checker.assert_equal(
        len(by_label_3["t3-d"].calls),
        0,
        "C03 loop stops at the signature that reaches the threshold",
    )
    checker.assert_equal(
        [
            by_label_3[f"t3-{keyid}"].call_keyids()
            for keyid in ["a", "b", "c"]
        ],
        [["a"], ["b"], ["c"]],
        "C03 each earlier key called exactly once",
    )

    # Threshold 1 on a fresh set: only the first signature is inspected.
    keys_1 = fresh_keys("t1")
    by_label_1 = {key.label: key for key in keys_1}
    envelope_1 = build_envelope(sigs)
    accepted_1 = envelope_1.verify(keys_1, 1)
    checker.assert_equal(
        sorted(accepted_1.keys()),
        ["a"],
        "C03 threshold 1 stops after the first signature",
    )
    checker.assert_equal(
        [
            len(by_label_1[f"t1-{keyid}"].calls)
            for keyid in ["b", "c", "d"]
        ],
        [0, 0, 0],
        "C03 no later key is invoked once threshold 1 is reached",
    )

    # Threshold equal to the number of signatures: every one is inspected.
    keys_4 = fresh_keys("t4")
    envelope_4 = build_envelope(sigs)
    accepted_4 = envelope_4.verify(keys_4, 4)
    checker.assert_equal(
        len(accepted_4),
        4,
        "C03 threshold equal to signatures inspects every signature",
    )
    checker.assert_equal(
        [len(key.calls) for key in keys_4],
        [1, 1, 1, 1],
        "C03 all four keys invoked exactly once",
    )

    # A signature with no matching key consumes no candidate calls but also
    # contributes no acceptance.
    extra_keys = [
        RecordingKey("a", accept_sigs=["a"], label="x-a"),
        RecordingKey("b", accept_sigs=["b"], label="x-b"),
    ]
    envelope_gap = build_envelope(
        [make_signature("nomatch"), sigs[0], sigs[1]]
    )
    accepted_gap = envelope_gap.verify(extra_keys, 2)
    checker.assert_equal(
        sorted(accepted_gap.keys()),
        ["a", "b"],
        "C03 unmatched signature does not block later acceptance",
    )

    return ScenarioResult(
        name="c03_stop_position_at_threshold",
        seed={
            "signature_keyids": ["a", "b", "c", "d"],
            "thresholds": [3, 1, 4],
            "candidate_keys_per_trial": "fresh per trial, one key per keyid",
        },
        expected={
            "threshold_3_accepted": ["a", "b", "c"],
            "threshold_3_d_calls": 0,
            "threshold_1_accepted": ["a"],
            "threshold_1_later_calls": [0, 0, 0],
            "threshold_4_accepted_count": 4,
            "threshold_4_calls_per_key": [1, 1, 1, 1],
        },
        observed={
            "threshold_3_accepted": sorted(accepted.keys()),
            "threshold_3_d_calls": len(by_label_3["t3-d"].calls),
            "threshold_1_accepted": sorted(accepted_1.keys()),
            "threshold_1_later_calls": [
                len(by_label_1[f"t1-{keyid}"].calls)
                for keyid in ["b", "c", "d"]
            ],
            "threshold_4_accepted_count": len(accepted_4),
            "threshold_4_calls_per_key": [len(key.calls) for key in keys_4],
            "threshold_3_call_log": _call_log(keys_3),
            "gap_call_log": _call_log(extra_keys),
        },
    )


def c03_exception_types(checker: Checker) -> ScenarioResult:
    """Verify distinguishes swallowed failures, propagated errors and unmet."""
    sig_a = make_signature("a")

    # UnverifiedSignatureError is the caught, continue-with-next-candidate type.
    rejecting = RecordingKey(
        "a",
        accept_sigs=[],
        fail_with=UnverifiedSignatureError,
        label="a-unverified",
    )
    accepting = RecordingKey("a", accept_sigs=["a"], label="a-accept")
    envelope = build_envelope([sig_a])
    accepted = envelope.verify([rejecting, accepting], 1)
    checker.assert_equal(
        sorted(accepted.keys()),
        ["a"],
        "C03 UnverifiedSignatureError is swallowed and failover proceeds",
    )
    checker.assert_equal(
        rejecting.outcomes(),
        ["rejected"],
        "C03 swallowed failure recorded on the rejecting candidate",
    )

    # VerificationError is a subclass of UnverifiedSignatureError: it is
    # caught inside the candidate loop as well.
    verr_key = RecordingKey(
        "a",
        accept_sigs=[],
        fail_with=VerificationError,
        label="a-verification-error",
    )
    envelope_2 = build_envelope([sig_a])
    expect_raises(
        checker,
        VerificationError,
        envelope_2.verify,
        [verr_key],
        1,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=0, expected=1),
        label="C03 VerificationError treated as catchable candidate failure",
    )

    # Any other exception is not part of the verify contract and must
    # propagate unchanged instead of being converted.
    crash_key = RecordingKey(
        "a",
        accept_sigs=[],
        fail_with=RuntimeError,
        label="a-runtime-error",
    )
    envelope_3 = build_envelope([sig_a])
    context = expect_raises(
        checker,
        RuntimeError,
        envelope_3.verify,
        [crash_key],
        1,
        label="C03 non-UnverifiedSignatureError propagates unchanged",
    )
    checker.assert_true(
        not isinstance(context.exception, VerificationError),
        "C03 propagated exception is not rewritten as VerificationError",
    )

    # Final unmet threshold message and type contract.
    envelope_4 = build_envelope([make_signature("lonely")])
    expect_raises(
        checker,
        VerificationError,
        envelope_4.verify,
        [RecordingKey("other", accept_sigs=[], label="other")],
        1,
        msg_contains=UNMET_MSG_TEMPLATE.format(found=0, expected=1),
        label="C03 unmet threshold raises VerificationError with contract message",
    )

    return ScenarioResult(
        name="c03_exception_types",
        seed={
            "signature_keyids": ["a"],
            "keys": _key_seed([rejecting, accepting, verr_key, crash_key]),
        },
        expected={
            "swallowed": "UnverifiedSignatureError",
            "also_swallowed": "VerificationError (subclass)",
            "propagated": "RuntimeError",
            "unmet_message": UNMET_MSG_TEMPLATE.format(found=0, expected=1),
        },
        observed={
            "rejecting_outcomes": rejecting.outcomes(),
            "call_log": _call_log([rejecting, accepting, verr_key, crash_key]),
            "propagated_type": type(context.exception).__name__,
        },
    )


def c03_independent_inputs_per_trial(checker: Checker) -> ScenarioResult:
    """Repeated trials use independent, freshly constructed input dicts."""
    sig_a = make_signature("a")
    sig_b = make_signature("b")
    key_a = RecordingKey("a", accept_sigs=["a"], label="indep-a")
    key_b = RecordingKey("b", accept_sigs=["b"], label="indep-b")
    keys = [key_a, key_b]

    # A serialization template is deep-copied per trial; from_dict mutates
    # (consumes) the dict it receives, so sharing one dict would corrupt it.
    template = envelope_dict([sig_a, sig_b])
    trial_1_envelope = Envelope.from_dict(copy.deepcopy(template))
    trial_2_envelope = Envelope.from_dict(copy.deepcopy(template))

    accepted_1 = trial_1_envelope.verify(keys, 2)
    accepted_2 = trial_2_envelope.verify(keys, 2)
    checker.assert_equal(
        sorted(accepted_1.keys()),
        ["a", "b"],
        "C03 first independent trial reaches threshold 2",
    )
    checker.assert_equal(
        sorted(accepted_2.keys()),
        ["a", "b"],
        "C03 second independent trial reaches threshold 2",
    )
    checker.assert_true(
        trial_1_envelope is not trial_2_envelope,
        "C03 trials build distinct envelope objects",
    )
    checker.assert_true(
        trial_1_envelope.signatures["a"]
        is not trial_2_envelope.signatures["a"],
        "C03 trials do not share signature objects",
    )
    checker.assert_equal(
        template["signatures"],
        envelope_dict([sig_a, sig_b])["signatures"],
        "C03 template stays intact because each trial consumed a deep copy",
    )
    # Every verify_signature call received the independently computed PAE of
    # its own envelope, and both envelopes agree on the PAE bytes.
    checker.assert_equal(
        trial_1_envelope.pae(),
        trial_2_envelope.pae(),
        "C03 PAE is deterministic across independent trials",
    )
    checker.assert_true(
        all(call.pae == trial_1_envelope.pae() for key in keys for call in key.calls),
        "C03 each key double received the envelope PAE",
    )

    # Demonstrate the consumption hazard: reusing one dict for a second
    # from_dict raises KeyError because Signature.from_dict pops its fields.
    consumed = copy.deepcopy(template)
    Envelope.from_dict(consumed)
    expect_raises(
        checker,
        KeyError,
        Envelope.from_dict,
        consumed,
        label="C03 reusing a consumed dict fails, proving copies are required",
    )
    # The untouched template still supports fresh trials.
    retry_envelope = Envelope.from_dict(copy.deepcopy(template))
    checker.assert_equal(
        list(retry_envelope.signatures.keys()),
        ["a", "b"],
        "C03 fresh deep copy of the template remains replayable",
    )

    return ScenarioResult(
        name="c03_independent_inputs_per_trial",
        seed={
            "template_signature_keyids": ["a", "b"],
            "keys": _key_seed(keys),
            "threshold": 2,
            "trials": 2,
        },
        expected={
            "accepted_per_trial": ["a", "b"],
            "reused_consumed_dict": "KeyError",
            "template_replayable": True,
        },
        observed={
            "trial_1_accepted": sorted(accepted_1.keys()),
            "trial_2_accepted": sorted(accepted_2.keys()),
            "call_log": _call_log(keys),
            "pae_hex": trial_1_envelope.pae().hex(),
        },
    )


# ---------------------------------------------------------------------------
# C04: real local keys -- genuine signatures and message/type changes
# ---------------------------------------------------------------------------


def load_real_signers() -> list[tuple[str, CryptoSigner]]:
    """Load the rsa/ecdsa/ed25519 signers shipped as local PEM fixtures."""
    signers: list[tuple[str, CryptoSigner]] = []
    for keytype in ["rsa", "ecdsa", "ed25519"]:
        pem_path = PEMS_DIR / f"{keytype}_private.pem"
        private_key = load_pem_private_key(pem_path.read_bytes(), None)
        signers.append((keytype, CryptoSigner(private_key)))
    return signers


def _keyid_prefix(keyid: str) -> str:
    return f"{keyid[:12]}...(truncated)"


def c04_real_key_roundtrip(checker: Checker) -> ScenarioResult:
    """Genuine signatures from rsa, ecdsa and ed25519 verify against keys."""
    signers = load_real_signers()
    envelope = build_envelope([])
    public_keys = []
    for _keytype, signer in signers:
        envelope.sign(signer)
        public_keys.append(signer.public_key)

    accepted = envelope.verify(public_keys, 3)
    expected_keyids = [signer.public_key.keyid for _keytype, signer in signers]
    checker.assert_equal(
        sorted(accepted.keys()),
        sorted(expected_keyids),
        "C04 all three real signatures accepted by their public keys",
    )

    accepted_two = envelope.verify(public_keys, 2)
    checker.assert_equal(
        len(accepted_two),
        2,
        "C04 threshold 2 returns two unique accepted keyids",
    )

    # Serialization round trip must preserve verifiability with fresh inputs.
    serialized = envelope.to_dict()
    restored = Envelope.from_dict(copy.deepcopy(serialized))
    restored_accepted = restored.verify(public_keys, 3)
    checker.assert_equal(
        sorted(restored_accepted.keys()),
        sorted(expected_keyids),
        "C04 envelope remains verifiable after to_dict/from_dict round trip",
    )

    return ScenarioResult(
        name="c04_real_key_roundtrip",
        seed={
            "real_keys": [
                {
                    "keytype": keytype,
                    "scheme": signer.public_key.scheme,
                    "keyid": _keyid_prefix(signer.public_key.keyid),
                }
                for keytype, signer in signers
            ],
            "thresholds": [3, 2],
        },
        expected={
            "accepted_keyids": "all three signer keyids",
            "threshold_2_count": 2,
            "round_trip_verifiable": True,
        },
        observed={
            "accepted_count": len(accepted),
            "accepted_keyid_prefixes": [
                _keyid_prefix(keyid) for keyid in sorted(accepted)
            ],
            "round_trip_accepted_count": len(restored_accepted),
        },
    )


def c04_modified_payload_fails(checker: Checker) -> ScenarioResult:
    """Changing envelope payload invalidates every genuine signature."""
    signers = load_real_signers()
    envelope = build_envelope([])
    public_keys = []
    for _keytype, signer in signers:
        envelope.sign(signer)
        public_keys.append(signer.public_key)

    tampered = copy.deepcopy(envelope)
    tampered.payload = b"tampered payload"
    context = expect_raises(
        checker,
        VerificationError,
        tampered.verify,
        public_keys,
        1,
        label="C04 modified payload",
    )
    checker.assert_true(
        isinstance(context.exception, UnverifiedSignatureError),
        "C04 envelope-level failure is VerificationError (UnverifiedSignatureError)",
    )

    restored = copy.deepcopy(envelope)
    accepted = restored.verify(public_keys, 1)
    checker.assert_equal(
        len(accepted),
        1,
        "C04 untouched copy still verifies after tamper trial",
    )

    return ScenarioResult(
        name="c04_modified_payload_fails",
        seed={
            "original_payload": PAYLOAD.decode("utf-8"),
            "tampered_payload": "tampered payload",
            "threshold": 1,
        },
        expected={"tampered": "VerificationError", "restored_accepted": 1},
        observed={
            "tampered_exception": type(context.exception).__name__,
            "restored_accepted": len(accepted),
        },
    )


def c04_modified_payload_type_fails(checker: Checker) -> ScenarioResult:
    """Changing payload_type changes PAE and invalidates signatures."""
    _keytype, signer = load_real_signers()[2]  # ed25519
    envelope = build_envelope([])
    envelope.sign(signer)

    tampered = copy.deepcopy(envelope)
    tampered.payload_type = "http://example.com/Evil"
    expect_raises(
        checker,
        VerificationError,
        tampered.verify,
        [signer.public_key],
        1,
        label="C04 modified payload_type",
    )

    restored = copy.deepcopy(envelope)
    accepted = restored.verify([signer.public_key], 1)
    checker.assert_equal(
        sorted(accepted.keys()),
        [signer.public_key.keyid],
        "C04 original payload_type verifies",
    )
    checker.assert_true(
        tampered.pae() != envelope.pae(),
        "C04 payload_type change produces a different PAE",
    )

    return ScenarioResult(
        name="c04_modified_payload_type_fails",
        seed={
            "keytype": "ed25519",
            "original_payload_type": PAYLOAD_TYPE,
            "tampered_payload_type": "http://example.com/Evil",
        },
        expected={"tampered": "VerificationError", "pae_changed": True},
        observed={
            "original_pae_hex": envelope.pae().hex(),
            "tampered_pae_hex": tampered.pae().hex(),
            "restored_accepted": [
                _keyid_prefix(keyid) for keyid in sorted(accepted.keys())
            ],
        },
    )


def c04_tampered_signature_fails(checker: Checker) -> ScenarioResult:
    """Mutating or swapping signature bytes fails real verification."""
    signers = load_real_signers()
    ed_signer = signers[2][1]
    rsa_signer = signers[0][1]
    envelope = build_envelope([])
    envelope.sign(ed_signer)
    original_keyid = ed_signer.public_key.keyid

    # Flip a byte inside the signature while keeping the same keyid.
    flipped = copy.deepcopy(envelope)
    raw = bytearray.fromhex(flipped.signatures[original_keyid].signature)
    raw[0] ^= 0x01
    flipped.signatures[original_keyid].signature = raw.hex()
    expect_raises(
        checker,
        VerificationError,
        flipped.verify,
        [ed_signer.public_key],
        1,
        label="C04 bit-flipped signature",
    )

    # Swap in another key's signature bytes under the ed25519 keyid.
    swapped = copy.deepcopy(envelope)
    foreign = rsa_signer.sign(envelope.pae())
    swapped.signatures[original_keyid] = Signature(original_keyid, foreign.signature)
    expect_raises(
        checker,
        VerificationError,
        swapped.verify,
        [ed_signer.public_key],
        1,
        label="C04 foreign signature under matching keyid",
    )

    restored = copy.deepcopy(envelope)
    accepted = restored.verify([ed_signer.public_key], 1)
    checker.assert_equal(
        sorted(accepted.keys()),
        [original_keyid],
        "C04 unmodified signature still verifies",
    )

    return ScenarioResult(
        name="c04_tampered_signature_fails",
        seed={
            "victim_keyid": _keyid_prefix(original_keyid),
            "mutations": ["bit-flip first signature byte", "rsa signature swap"],
        },
        expected={"tampered": "VerificationError", "restored_accepted": 1},
        observed={
            "restored_accepted": [
                _keyid_prefix(keyid) for keyid in sorted(accepted.keys())
            ]
        },
    )


def c04_same_keyid_different_key_material(checker: Checker) -> ScenarioResult:
    """A matching keyid alone does not verify against foreign key material."""
    signer = CryptoSigner.generate_ed25519()
    # Second, independently generated key forced to advertise the same keyid.
    spoof = CryptoSigner.generate_ed25519(keyid=signer.public_key.keyid)
    checker.assert_true(
        signer.public_key.keyval != spoof.public_key.keyval,
        "C04 generated keys hold different public material",
    )
    checker.assert_equal(
        signer.public_key.keyid,
        spoof.public_key.keyid,
        "C04 test seeds identical advertised keyids",
    )

    envelope = build_envelope([])
    envelope.sign(signer)
    expect_raises(
        checker,
        VerificationError,
        envelope.verify,
        [spoof.public_key],
        1,
        label="C04 same keyid, foreign key material",
    )

    accepted = envelope.verify([signer.public_key], 1)
    checker.assert_equal(
        sorted(accepted.keys()),
        [signer.public_key.keyid],
        "C04 genuine key still verifies the signature",
    )

    return ScenarioResult(
        name="c04_same_keyid_different_key_material",
        seed={
            "keytype": "ed25519",
            "advertised_keyid": _keyid_prefix(signer.public_key.keyid),
            "generated_keys": 2,
        },
        expected={
            "spoof_result": "VerificationError",
            "genuine_result": "accepted",
        },
        observed={
            "spoof_keyid": _keyid_prefix(spoof.public_key.keyid),
            "genuine_accepted": [
                _keyid_prefix(keyid) for keyid in sorted(accepted.keys())
            ],
        },
    )


SCENARIOS: dict[str, list[Scenario]] = {
    "C01": [
        Scenario(
            "c01_unrelated_keyid_is_never_called",
            "Unrelated keyid is skipped without verify_signature calls; "
            "rejecting match yields unmet threshold",
            c01_unrelated_keyid_is_never_called,
        ),
        Scenario(
            "c01_unique_keyid_count_not_candidate_objects",
            "Acceptance counts unique keyids, not candidate objects",
            c01_unique_keyid_count_not_candidate_objects,
        ),
        Scenario(
            "c01_threshold_validation",
            "Zero/negative threshold and threshold above key count raise "
            "ValueError before signature iteration",
            c01_threshold_validation,
        ),
    ],
    "C02": [
        Scenario(
            "c02_failover_to_later_candidate",
            "Same keyid candidates: first fails, later succeeds; order swap",
            c02_failover_to_later_candidate,
        ),
        Scenario(
            "c02_all_candidates_fail",
            "All same-keyid candidates are tried and rejection is terminal",
            c02_all_candidates_fail,
        ),
        Scenario(
            "c02_duplicate_candidate_object",
            "Repeated candidate object is invoked once and counted once",
            c02_duplicate_candidate_object,
        ),
        Scenario(
            "c02_signature_insertion_order",
            "Signature insertion order drives processing on direct and "
            "serialized build paths",
            c02_signature_insertion_order,
        ),
    ],
    "C03": [
        Scenario(
            "c03_stop_position_at_threshold",
            "Outer loop stops at the signature reaching the threshold",
            c03_stop_position_at_threshold,
        ),
        Scenario(
            "c03_exception_types",
            "Swallowed UnverifiedSignatureError, propagated RuntimeError "
            "and unmet-threshold VerificationError contract",
            c03_exception_types,
        ),
        Scenario(
            "c03_independent_inputs_per_trial",
            "Deep-copied per-trial inputs; consumed-dict reuse raises KeyError",
            c03_independent_inputs_per_trial,
        ),
    ],
    "C04": [
        Scenario(
            "c04_real_key_roundtrip",
            "RSA/ECDSA/Ed25519 genuine signatures verify, incl. dict round trip",
            c04_real_key_roundtrip,
        ),
        Scenario(
            "c04_modified_payload_fails",
            "Payload change invalidates signatures, untouched copy verifies",
            c04_modified_payload_fails,
        ),
        Scenario(
            "c04_modified_payload_type_fails",
            "payload_type change alters PAE and invalidates signatures",
            c04_modified_payload_type_fails,
        ),
        Scenario(
            "c04_tampered_signature_fails",
            "Bit-flipped and swapped signature bytes fail verification",
            c04_tampered_signature_fails,
        ),
        Scenario(
            "c04_same_keyid_different_key_material",
            "Matching keyid with foreign key material does not verify",
            c04_same_keyid_different_key_material,
        ),
    ],
}
