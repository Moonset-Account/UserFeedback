"""Offline Linux-capability state推演模块（非 root 主体，固定十二项能力全集）。

公共 API:
    CAP_NAMES, ALL_MASK, NAME_TO_BIT          - capsim.caps
    to_mask, to_names                         - capsim.caps
    CapState                                  - capsim.state
    plain_exec, file_cap_exec                 - capsim.transitions
    lower_bounding, raise_ambient             - capsim.transitions
    apply_operation, TransitionError          - capsim.transitions
    Edge, Graph, analyze                      - capsim.graph
"""
from .caps import ALL_MASK, CAP_NAMES, NAME_TO_BIT, to_mask, to_names
from .state import CapState
from .transitions import (
    TransitionError,
    apply_operation,
    file_cap_exec,
    lower_bounding,
    plain_exec,
    raise_ambient,
)
from .graph import Edge, Graph, analyze

__all__ = [
    "ALL_MASK",
    "CAP_NAMES",
    "NAME_TO_BIT",
    "to_mask",
    "to_names",
    "CapState",
    "TransitionError",
    "apply_operation",
    "file_cap_exec",
    "lower_bounding",
    "plain_exec",
    "raise_ambient",
    "Edge",
    "Graph",
    "analyze",
]
