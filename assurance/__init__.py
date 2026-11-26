"""Assurance-level mutual recognition and proof-chain evaluation.

The canonical entry point is :class:`RecognitionEngine`.  The module-level
:func:`recognize` function is provided for small scripts and tests.

Example:
    >>> from datetime import datetime, timezone, timedelta
    >>> from assurance import RecognitionEngine, VerificationFact, RecognitionRule
    >>> now = datetime(2026, 11, 27, tzinfo=timezone.utc)
    >>> fact = VerificationFact("partner", 3, "hardware_key", now - timedelta(minutes=1))
    >>> rule = RecognitionRule("partner", "us", target_level=3, max_age=timedelta(minutes=5))
    >>> result = RecognitionEngine([rule]).evaluate(fact, 2, at=now)
    >>> result.valid
    True
    >>> result.final_level
    3
"""

from .coercion import AssuranceInputError
from .engine import (
    RecognitionEngine,
    evaluate_assurance,
    find_proof_chain,
    recognize,
    recognize_assurance,
)
from .models import (
    Fact,
    FailureCandidate,
    ProofChain,
    ProofStep,
    RecognitionResult,
    RecognitionRule,
    Scope,
    TimeScope,
    VerificationFact,
)
from .reasons import REASON_MESSAGES_ZH

MutualRecognitionEngine = RecognitionEngine
AssuranceRecognitionEngine = RecognitionEngine
AssuranceMatcher = RecognitionEngine
MutualRecognition = RecognitionEngine

__all__ = [
    "AssuranceInputError",
    "RecognitionEngine",
    "MutualRecognitionEngine",
    "AssuranceRecognitionEngine",
    "AssuranceMatcher",
    "MutualRecognition",
    "VerificationFact",
    "Fact",
    "RecognitionRule",
    "ProofChain",
    "ProofStep",
    "RecognitionResult",
    "FailureCandidate",
    "Scope",
    "TimeScope",
    "recognize",
    "find_proof_chain",
    "evaluate_assurance",
    "recognize_assurance",
    "REASON_MESSAGES_ZH",
]

__version__ = "1.0.0"
