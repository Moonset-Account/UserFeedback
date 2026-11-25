"""Executable local demonstration of issuance, replay, and concurrency cases.

Run with::

    python3 -m blind_telemetry.demo

All data are synthetic and all randomness is scripted for reproducibility.
"""

from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

from .errors import (
    DuplicateReportError,
    InvalidCredentialError,
    PeriodConflictError,
)
from .protocol import Collector, Device, Registrar
from .randomness import ScriptedRandomSource
from .rsa import DEMO_KEY_PAIR

TICKET_16 = b"0123456789abcdef"


def main() -> int:
    keys = DEMO_KEY_PAIR
    public = keys.public_key
    registrar = Registrar(
        keys.private_key,
        registered_devices={"device-demo", "factor-a", "factor-b"},
    )
    collector = Collector(public)

    print("== Blind telemetry demo ==")
    print(
        f"RSA demo parameters: n={public.n}, e={public.e} "
        "(textbook key; not safe for production)"
    )

    # 1. Normal issuance plus exact idempotent retry.
    device = Device(
        "device-demo",
        public,
        rng=ScriptedRandomSource(ticket=TICKET_16, factors=[42]),
        ticket_length=len(TICKET_16),
    )
    prepared = device.prepare(period=1)
    first_reply = registrar.issue(
        device.device_id, prepared.period, prepared.blinded_message
    )
    retry_reply = registrar.issue(
        device.device_id, prepared.period, prepared.blinded_message
    )
    assert retry_reply is first_reply
    assert registrar.sign_operations == 1

    credential = device.unblind(prepared, first_reply)
    report = credential.to_report(measurement="temperature_c=21.5")
    receipt = collector.receive(report)
    print(
        "normal report accepted: "
        f"period={receipt.period}, sequence={receipt.sequence}, "
        f"ticket={receipt.ticket.hex()}"
    )
    print("identical registrar retry returned the original BlindReply object")

    # A different blind integer for the same device/period is rejected.
    try:
        registrar.issue("device-demo", 1, 7)
    except PeriodConflictError:
        print("different blind for same device/period: rejected with conflict")
    else:
        raise AssertionError("different blind for same period must be rejected")

    # 2. Two distinct injected blinding factors yield one usable signature.
    device_a = Device(
        "factor-a",
        public,
        rng=ScriptedRandomSource(ticket=TICKET_16, factors=[2]),
        ticket_length=len(TICKET_16),
    )
    device_b = Device(
        "factor-b",
        public,
        rng=ScriptedRandomSource(ticket=TICKET_16, factors=[3]),
        ticket_length=len(TICKET_16),
    )
    state_a = device_a.prepare(period=9)
    state_b = device_b.prepare(period=9)
    assert state_a.blinding_factor != state_b.blinding_factor
    assert state_a.blinded_message != state_b.blinded_message

    reply_a = registrar.issue("factor-a", 9, state_a.blinded_message)
    reply_b = registrar.issue("factor-b", 9, state_b.blinded_message)
    cred_a = device_a.unblind(state_a, reply_a)
    cred_b = device_b.unblind(state_b, reply_b)
    assert cred_a.signature == cred_b.signature
    assert public.verify_signature_integer(cred_a.signature, state_a.hash_value)
    print(
        "two injected factors "
        f"r={state_a.blinding_factor},{state_b.blinding_factor} produced "
        f"different blind texts but the same valid signature {cred_a.signature}"
    )

    # 3. Same-period duplicate and cross-period replay.
    try:
        collector.receive(report)
    except DuplicateReportError as exc:
        print(f"same-period duplicate report: rejected (sequence {exc.original_sequence})")
    else:
        raise AssertionError("same-period duplicate must be rejected")

    cross_period = dataclasses.replace(report, period=2)
    try:
        collector.receive(cross_period)
    except InvalidCredentialError:
        print("cross-period replay under period=2: rejected by public-key verification")
    else:
        raise AssertionError("cross-period replay must not verify")

    # 4. Two concurrent copies of one fresh report: exactly one acceptance.
    concurrent_ticket = b"c" * 16
    concurrent_device = Device(
        "device-demo",
        public,
        rng=ScriptedRandomSource(ticket=concurrent_ticket, factors=[11]),
        ticket_length=len(concurrent_ticket),
    )
    concurrent_state = concurrent_device.prepare(period=3)
    concurrent_reply = registrar.issue(
        "device-demo", 3, concurrent_state.blinded_message
    )
    concurrent_credential = concurrent_device.unblind(
        concurrent_state, concurrent_reply
    )
    concurrent_report = concurrent_credential.to_report(measurement="counter=1")

    barrier = threading.Barrier(2)

    def worker() -> tuple[str, int]:
        barrier.wait()
        try:
            accepted = collector.receive(concurrent_report)
            return "accepted", accepted.sequence
        except DuplicateReportError as exc:
            return "duplicate", exc.original_sequence

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        concurrent_outcomes = [future.result() for future in futures]

    accepted = [item for item in concurrent_outcomes if item[0] == "accepted"]
    duplicates = [item for item in concurrent_outcomes if item[0] == "duplicate"]
    assert len(accepted) == 1
    assert len(duplicates) == 1
    assert collector.accepted_count() == 2  # period 1 and period 3 only
    print(f"concurrent double submission outcomes: {concurrent_outcomes}")
    print(f"collector accepted exactly {collector.accepted_count()} distinct reports")
    print("demo completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
