"""depmatcher – release-pipeline artifact dependency matching.

Public API:

* Model objects: :class:`Artifact`, :class:`TestPackage`,
  :class:`SlotRequirement`, :class:`Exclusion`, constraints
  (:class:`MatchValue`, :class:`MatchAnyOf`, :class:`MatchAny`) and
  :class:`DimensionError`.
* Rules: :class:`RuleBook`.
* Matching: :class:`Inventory`, :class:`DependencyMatcher`,
  :class:`MatchResult`, :class:`SlotShortage`, :class:`Contention`.
* Replay: :class:`Binding`, :class:`BindingRegistry`, :class:`ReplayResult`.
"""

from .binding import Binding, BindingRegistry, ReplayResult
from .matcher import (
    Contention,
    DependencyMatcher,
    Inventory,
    MatchResult,
    SlotShortage,
)
from .model import (
    Artifact,
    DimensionError,
    Exclusion,
    MatchAny,
    MatchAnyOf,
    MatchValue,
    SlotRequirement,
    TestPackage,
)
from .rules import FEATURES, RuleBook

__all__ = [
    "Artifact",
    "Binding",
    "BindingRegistry",
    "Contention",
    "DependencyMatcher",
    "DimensionError",
    "Exclusion",
    "FEATURES",
    "Inventory",
    "MatchAny",
    "MatchAnyOf",
    "MatchResult",
    "MatchValue",
    "ReplayResult",
    "RuleBook",
    "SlotRequirement",
    "SlotShortage",
    "TestPackage",
]
