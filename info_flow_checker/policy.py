"""Disclosure policy labels for a circuit."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .errors import LimitExceededError, PolicyValidationError

MAX_PUBLIC_BITS = 8
MAX_SECRET_BITS = 12


def _names(values: Any, field_name: str, *, allow_none: bool = False) -> tuple[str, ...] | None:
    if values is None:
        if allow_none:
            return None
        return ()
    if isinstance(values, str):
        raise PolicyValidationError(f"{field_name} must be an iterable of names, not a string")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise PolicyValidationError(f"{field_name} must be an iterable of names") from exc

    result: list[str] = []
    seen: set[str] = set()
    for value in iterator:
        if not isinstance(value, str) or not value:
            raise PolicyValidationError(f"{field_name} contains invalid name {value!r}")
        if value in seen:
            raise PolicyValidationError(f"{field_name} contains duplicate label {value!r}")
        seen.add(value)
        result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class Policy:
    """Label inputs and outputs.

    Parameters
    ----------
    public_inputs:
        Public circuit input names.  At most eight.
    secret_inputs:
        Secret circuit input names.  At most twelve.
    summary_outputs:
        Output names that the caller is approved to disclose.  Two runs are
        compared only when these values agree.
    external_outputs:
        Actual externally published output names, in first-difference order.
        ``None`` means every circuit output in circuit-defined order; ``()``
        means no external bits.
    """

    public_inputs: Sequence[str] = ()
    secret_inputs: Sequence[str] = ()
    summary_outputs: Sequence[str] = ()
    external_outputs: Sequence[str] | None = None

    def __post_init__(self) -> None:
        public = _names(self.public_inputs, "public_inputs") or ()
        secret = _names(self.secret_inputs, "secret_inputs") or ()
        summaries = _names(self.summary_outputs, "summary_outputs") or ()
        external = _names(
            self.external_outputs, "external_outputs", allow_none=True
        )

        if len(public) > MAX_PUBLIC_BITS:
            raise LimitExceededError(
                f"at most {MAX_PUBLIC_BITS} public inputs are supported, got {len(public)}"
            )
        if len(secret) > MAX_SECRET_BITS:
            raise LimitExceededError(
                f"at most {MAX_SECRET_BITS} secret inputs are supported, got {len(secret)}"
            )
        overlap = set(public) & set(secret)
        if overlap:
            raise PolicyValidationError(
                "an input cannot be both public and secret: " + ", ".join(sorted(overlap))
            )

        object.__setattr__(self, "public_inputs", public)
        object.__setattr__(self, "secret_inputs", secret)
        object.__setattr__(self, "summary_outputs", summaries)
        object.__setattr__(self, "external_outputs", external)

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | "Policy") -> "Policy":
        if isinstance(data, Policy):
            return data
        if not hasattr(data, "get"):
            raise PolicyValidationError("policy must be a Policy or mapping")
        return cls(
            public_inputs=data.get("public_inputs", data.get("public", ())),
            secret_inputs=data.get("secret_inputs", data.get("secret", ())),
            summary_outputs=data.get(
                "summary_outputs", data.get("approved_outputs", data.get("summaries", ()))
            ),
            external_outputs=data.get(
                "external_outputs", data.get("outputs", data.get("external", None))
            ),
        )
