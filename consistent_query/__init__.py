"""Consistent query answering over minimum-cost deletion repairs.

Public API::

    from consistent_query import (
        Denial, ForeignKey, Problem, Query, Row, Table,
        analyse, keep_row, delete_row, Limits,
    )

See ``README.md`` for the semantics and the verification workflow.
"""

from .engine import (
    STATUS_ABSENT,
    STATUS_CERTAIN,
    STATUS_CONTINGENT,
    AnalysisReport,
    CandidateVerdict,
    ComponentReport,
    Derivation,
    InfeasiblePins,
    Limits,
    Pin,
    RepairError,
    RepairLimitExceeded,
    Witness,
    analyse,
    delete_row,
    keep_row,
)
from .model import (
    Denial,
    ForeignKey,
    ModelError,
    Problem,
    Query,
    Row,
    RowKey,
    Table,
    coerce_row_key,
)
from .verify import OracleReport, is_valid_repair, oracle, repair_cost

__version__ = "1.0.0"

__all__ = [
    "STATUS_ABSENT",
    "STATUS_CERTAIN",
    "STATUS_CONTINGENT",
    "AnalysisReport",
    "CandidateVerdict",
    "ComponentReport",
    "Denial",
    "Derivation",
    "ForeignKey",
    "InfeasiblePins",
    "Limits",
    "ModelError",
    "OracleReport",
    "Pin",
    "Problem",
    "Query",
    "RepairError",
    "RepairLimitExceeded",
    "Row",
    "RowKey",
    "Table",
    "Witness",
    "__version__",
    "analyse",
    "coerce_row_key",
    "delete_row",
    "is_valid_repair",
    "keep_row",
    "oracle",
    "repair_cost",
]
