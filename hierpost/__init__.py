"""hierpost — hierarchical integer post-processing for noisy frequency tables.

Public API
----------
``Node``              one hierarchy node (id, parent, weight, noisy, leaf_index)
``Hierarchy``         validated tree, constructed from ``Node`` objects or a dict
``HierarchyError``    raised for an inconsistent hierarchy description
``solve``             solve a ``Hierarchy`` -> ``PostprocessResult``
``solve_spec``        solve the plain-dict form directly
``PostprocessResult`` counts, total_cost, leaf_vector, level_totals, measured_cost

The module only consumes noisy measurements, per-node positive integer
weights and the public root total.  It has no access to, and never reconstructs,
individual-level records.
"""

from .model import Hierarchy, HierarchyError, Node
from .solver import PostprocessResult, solve, solve_spec

__all__ = [
    "Hierarchy",
    "HierarchyError",
    "Node",
    "PostprocessResult",
    "solve",
    "solve_spec",
]

__version__ = "1.0.0"
