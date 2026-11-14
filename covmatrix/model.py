"""Data model for the coverage matrix.

All types are plain immutable value objects so analysis results are
deterministic and easy to assert on in tests.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple


@dataclass(frozen=True)
class Entry:
    """A call entry point whose verification coverage is being audited."""

    id: str
    name: str
    module: str


@dataclass(frozen=True)
class Checkpoint:
    """A code checkpoint touched by the patch under review."""

    id: str
    module: str
    description: str = ""


@dataclass(frozen=True)
class Trace:
    """One observed test run exported from the local test records.

    A single test may hit several checkpoints; ``checkpoints`` keeps them
    all and the analyzer records each hit separately.
    """

    test_id: str
    build: str
    entry_id: str
    checkpoints: Tuple[str, ...]


@dataclass
class CallGraph:
    """Directed module call graph. Cycles are allowed and expected."""

    modules: Set[str] = field(default_factory=set)
    adjacency: Dict[str, List[str]] = field(default_factory=dict)

    def callees(self, module: str) -> List[str]:
        return self.adjacency.get(module, [])
