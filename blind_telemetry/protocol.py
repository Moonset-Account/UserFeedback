"""Device, registrar, and collector roles for the blind-credential flow."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable

from .encoding import (
    MAX_TICKET_LENGTH,
    HashToUnitResult,
    hash_to_unit,
    measurement_to_bytes,
    period_to_bytes,
    ticket_to_bytes,
)
from .errors import (
    DuplicateReportError,
    InvalidBlindReplyError,
    InvalidCredentialError,
    PeriodConflictError,
    UnknownDeviceError,
)
from .randomness import RandomSource, random_unit_factor, system_random_source
from .rsa import RsaPrivateKey, RsaPublicKey, validate_unit


@dataclass(frozen=True)
class PreparedBlindCredential:
    """Device-private state.  It contains the ticket and unblinding factor."""

    period: int
    ticket: bytes
    encoded_message: bytes
    hash_value: int
    hash_counter: int
    blinding_factor: int
    blinded_message: int


@dataclass(frozen=True)
class BlindReply:
    """Registrar response.  It never sees the ticket or blinding factor."""

    device_id: str
    period: int
    blinded_message: int
    blinded_signature: int


@dataclass(frozen=True)
class Credential:
    """A verified, deblinded RSA signature over period and ticket."""

    period: int
    ticket: bytes
    signature: int
    hash_counter: int

    def to_report(self, measurement: bytes | bytearray | memoryview | str | None = b"") -> "Report":
        return Report(
            period=self.period,
            ticket=self.ticket,
            signature=self.signature,
            measurement=measurement_to_bytes(measurement),
        )


@dataclass(frozen=True)
class Report:
    """Anonymous report presented only to the public-key collector."""

    period: int
    ticket: bytes | str
    signature: int
    measurement: bytes | bytearray | memoryview | str | None = b""

    def __post_init__(self) -> None:
        period_to_bytes(self.period)  # validation only
        object.__setattr__(self, "ticket", ticket_to_bytes(self.ticket))
        object.__setattr__(self, "measurement", measurement_to_bytes(self.measurement))


@dataclass(frozen=True)
class Receipt:
    period: int
    ticket: bytes
    sequence: int


@dataclass(frozen=True)
class AcceptedReport:
    report: Report
    receipt: Receipt
    hash_counter: int


class Device:
    """Creates a ticket, blinds its hash, and later unblinds the reply."""

    def __init__(
        self,
        device_id: str,
        public_key: RsaPublicKey,
        *,
        rng: RandomSource | None = None,
        ticket_length: int = 16,
    ) -> None:
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id must be a nonempty string")
        if not isinstance(public_key, RsaPublicKey):
            raise TypeError("Device requires an RsaPublicKey")
        if (
            isinstance(ticket_length, bool)
            or not isinstance(ticket_length, int)
            or not 0 < ticket_length <= MAX_TICKET_LENGTH
        ):
            raise ValueError("ticket_length must be a positive supported integer")

        self.device_id = device_id
        self.public_key = public_key
        self.ticket_length = ticket_length
        self._rng = rng if rng is not None else system_random_source()

    def prepare(
        self,
        period: int,
        ticket: bytes | bytearray | memoryview | str | None = None,
        *,
        blinding_factor: int | None = None,
    ) -> PreparedBlindCredential:
        """Build the device-private state and the blind integer to register."""

        if ticket is None:
            ticket_raw = self._rng.randbytes(self.ticket_length)
        else:
            ticket_raw = ticket_to_bytes(ticket)

        hashed: HashToUnitResult = hash_to_unit(
            self.public_key.n, period, ticket_raw
        )

        if blinding_factor is None:
            factor = random_unit_factor(self.public_key.n, self._rng)
        else:
            factor = validate_unit(
                blinding_factor, self.public_key.n, name="blinding_factor"
            )

        blinded_message = (
            hashed.value * pow(factor, self.public_key.e, self.public_key.n)
        ) % self.public_key.n
        blinded_message = validate_unit(
            blinded_message, self.public_key.n, name="blinded_message"
        )

        return PreparedBlindCredential(
            period=period,
            ticket=ticket_raw,
            encoded_message=hashed.encoded_message,
            hash_value=hashed.value,
            hash_counter=hashed.counter,
            blinding_factor=factor,
            blinded_message=blinded_message,
        )

    def unblind(self, prepared: PreparedBlindCredential, reply: BlindReply) -> Credential:
        """Remove the blinding factor and verify the resulting signature."""

        if reply.device_id != self.device_id:
            raise InvalidBlindReplyError("reply device_id does not match this device")
        if reply.period != prepared.period:
            raise InvalidBlindReplyError("reply period does not match prepared state")
        if reply.blinded_message != prepared.blinded_message:
            raise InvalidBlindReplyError(
                "reply blinded_message does not match prepared state"
            )
        if (
            isinstance(reply.blinded_signature, bool)
            or not isinstance(reply.blinded_signature, int)
            or not 0 <= reply.blinded_signature < self.public_key.n
        ):
            raise InvalidBlindReplyError("blinded signature is not canonical")

        inverse_factor = pow(prepared.blinding_factor, -1, self.public_key.n)
        signature = (reply.blinded_signature * inverse_factor) % self.public_key.n

        if not self.public_key.verify_signature_integer(
            signature, prepared.hash_value
        ):
            raise InvalidCredentialError("deblinded RSA signature did not verify")

        return Credential(
            period=prepared.period,
            ticket=prepared.ticket,
            signature=signature,
            hash_counter=prepared.hash_counter,
        )

    def request_credential(
        self,
        registrar: "Registrar",
        period: int,
        *,
        ticket: bytes | bytearray | memoryview | str | None = None,
        blinding_factor: int | None = None,
    ) -> tuple[PreparedBlindCredential, Credential]:
        """Convenience helper for a normal synchronous issuance."""

        prepared = self.prepare(period, ticket, blinding_factor=blinding_factor)
        reply = registrar.issue(
            self.device_id, prepared.period, prepared.blinded_message
        )
        credential = self.unblind(prepared, reply)
        return prepared, credential


class Registrar:
    """Holds the RSA private key and enforces one blind per device/period."""

    def __init__(
        self,
        private_key: RsaPrivateKey,
        *,
        registered_devices: Iterable[str] | None = (),
    ) -> None:
        if not isinstance(private_key, RsaPrivateKey):
            raise TypeError("Registrar requires an RsaPrivateKey")
        self.private_key = private_key
        self._registered: set[str] = set(registered_devices or ())
        self._replies: dict[tuple[str, int], BlindReply] = {}
        self._lock = threading.Lock()
        self.sign_operations = 0

    def register_device(self, device_id: str) -> None:
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id must be a nonempty string")
        with self._lock:
            self._registered.add(device_id)

    def is_registered(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._registered

    def issued_count(self) -> int:
        with self._lock:
            return len(self._replies)

    def get_reply(self, device_id: str, period: int) -> BlindReply | None:
        with self._lock:
            return self._replies.get((device_id, period))

    def issue(
        self, device_id: str, period: int, blinded_message: int
    ) -> BlindReply:
        """Idempotently sign one blind integer for a registered device/period."""

        if not isinstance(device_id, str) or not device_id:
            raise UnknownDeviceError("device_id must be a nonempty string")
        if not self.is_registered(device_id):
            raise UnknownDeviceError(f"device {device_id!r} is not registered")

        # Validate period canonicalization and the RSA integer before taking
        # the idempotency lock.
        period_to_bytes(period)
        validate_unit(blinded_message, self.private_key.n, name="blinded_message")

        key = (device_id, period)
        with self._lock:
            existing = self._replies.get(key)
            if existing is not None:
                if existing.blinded_message == blinded_message:
                    # Exact retry: return the very same original answer.
                    return existing
                raise PeriodConflictError(device_id, period)

            blinded_signature = self.private_key.sign_unit(blinded_message)
            reply = BlindReply(
                device_id=device_id,
                period=period,
                blinded_message=blinded_message,
                blinded_signature=blinded_signature,
            )
            self._replies[key] = reply
            self.sign_operations += 1
            return reply


class Collector:
    """Public-key-only report verifier and atomic exactly-once redeemer."""

    def __init__(self, public_key: RsaPublicKey) -> None:
        if not isinstance(public_key, RsaPublicKey):
            raise TypeError("Collector requires an RsaPublicKey only")
        self.public_key = public_key
        self._accepted: dict[tuple[int, bytes], AcceptedReport] = {}
        self._lock = threading.Lock()
        self._next_sequence = 1

    def verify_report(self, report: Report) -> HashToUnitResult:
        if not isinstance(report, Report):
            raise TypeError("report must be a Report instance")

        hashed = hash_to_unit(self.public_key.n, report.period, report.ticket)
        if not self.public_key.verify_signature_integer(
            report.signature, hashed.value
        ):
            raise InvalidCredentialError(
                "report signature is not valid for its period and ticket"
            )
        return hashed

    def receive(self, report: Report) -> Receipt:
        """Verify and atomically redeem ``(period, ticket)`` exactly once."""

        hashed = self.verify_report(report)
        key = (report.period, bytes(report.ticket))

        with self._lock:
            existing = self._accepted.get(key)
            if existing is not None:
                raise DuplicateReportError(
                    period=report.period,
                    ticket=report.ticket,
                    original_sequence=existing.receipt.sequence,
                )

            receipt = Receipt(
                period=report.period,
                ticket=bytes(report.ticket),
                sequence=self._next_sequence,
            )
            self._next_sequence += 1
            self._accepted[key] = AcceptedReport(
                report=report, receipt=receipt, hash_counter=hashed.counter
            )
            return receipt

    def accepted_count(self) -> int:
        with self._lock:
            return len(self._accepted)

    def receipt_for(
        self, period: int, ticket: bytes | bytearray | memoryview | str
    ) -> Receipt | None:
        key = (period, ticket_to_bytes(ticket))
        with self._lock:
            accepted = self._accepted.get(key)
            return accepted.receipt if accepted else None

    def report_for(
        self, period: int, ticket: bytes | bytearray | memoryview | str
    ) -> Report | None:
        key = (period, ticket_to_bytes(ticket))
        with self._lock:
            accepted = self._accepted.get(key)
            return accepted.report if accepted else None
