"""Conventional solution entry point re-exporting the public API."""

from assurance import *  # noqa: F401,F403
from assurance import (
    AssuranceInputError,
    RecognitionEngine,
    RecognitionResult,
    RecognitionRule,
    VerificationFact,
    evaluate_assurance,
    find_proof_chain,
    recognize,
)

__all__ = [
    "AssuranceInputError",
    "RecognitionEngine",
    "VerificationFact",
    "RecognitionRule",
    "RecognitionResult",
    "recognize",
    "find_proof_chain",
    "evaluate_assurance",
]
