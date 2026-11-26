"""Compatibility module name for the mutual-recognition engine."""

from assurance import *  # noqa: F401,F403
from assurance import (  # explicit for static readers
    RecognitionEngine,
    RecognitionResult,
    RecognitionRule,
    VerificationFact,
    find_proof_chain,
    recognize,
)

MutualRecognitionEngine = RecognitionEngine
MutualRecognitionResult = RecognitionResult

__all__ = [
    "RecognitionEngine",
    "MutualRecognitionEngine",
    "VerificationFact",
    "RecognitionRule",
    "RecognitionResult",
    "MutualRecognitionResult",
    "recognize",
    "find_proof_chain",
]
