"""专网隧道提案协商模块。"""

from __future__ import annotations

from .errors import (
    NegotiationError,
    NoCommonProposalError,
    PolicyRejectionError,
    UnknownAlgorithmError,
)
from .models import PairDiagnostic, Proposal, RejectedSuite, SelectedSuite
from .negotiation import negotiate, negotiate_rekey, validate_proposal
from .policy import (
    DEFAULT_AEAD_ENCRYPTIONS,
    DEFAULT_STRENGTHS,
    Policy,
    default_policy,
)

__all__ = [
    "DEFAULT_AEAD_ENCRYPTIONS",
    "DEFAULT_STRENGTHS",
    "NegotiationError",
    "NoCommonProposalError",
    "PairDiagnostic",
    "Policy",
    "PolicyRejectionError",
    "Proposal",
    "RejectedSuite",
    "SelectedSuite",
    "UnknownAlgorithmError",
    "default_policy",
    "negotiate",
    "negotiate_rekey",
    "validate_proposal",
]
