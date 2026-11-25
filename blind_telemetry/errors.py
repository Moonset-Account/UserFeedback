"""Exception types for the blind telemetry demonstration."""

from __future__ import annotations


class BlindTelemetryError(Exception):
    """Base class for all expected protocol errors."""


class EncodingError(BlindTelemetryError, ValueError):
    """A period/ticket encoding is malformed or too long."""


class HashToUnitError(BlindTelemetryError):
    """No counter produced an invertible residue within the retry limit."""


class RandomSourceError(BlindTelemetryError):
    """The injected random source did not return a usable invertible factor."""


class RsaParameterError(BlindTelemetryError, ValueError):
    """The demonstration RSA key parameters are invalid."""


class BlindMessageError(BlindTelemetryError, ValueError):
    """A blind integer is outside the canonical RSA modulus or not a unit."""


class InvalidBlindReplyError(BlindTelemetryError):
    """A registrar reply does not match the device's blinded request."""


class UnknownDeviceError(BlindTelemetryError):
    """The registrar does not have the device identifier in its registry."""


class PeriodConflictError(BlindTelemetryError):
    """A different blind value was already accepted for this device/period."""

    def __init__(self, device_id: str, period: int) -> None:
        self.device_id = device_id
        self.period = period
        super().__init__(
            f"device {device_id!r} already used period {period} with another blind message"
        )


class InvalidCredentialError(BlindTelemetryError, ValueError):
    """The deblinded RSA credential or report failed public verification."""


class DuplicateReportError(BlindTelemetryError):
    """A report with the same (period, ticket) was already redeemed."""

    def __init__(self, period: int, ticket: bytes, original_sequence: int) -> None:
        self.period = period
        self.ticket = bytes(ticket)
        self.original_sequence = original_sequence
        super().__init__(
            f"report for period {period}, ticket {ticket.hex()} was already accepted "
            f"as sequence {original_sequence}"
        )
