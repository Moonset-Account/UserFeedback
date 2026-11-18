"""发布判定模块（release_gate）。

公共入口：

- :func:`release_gate.engine.evaluate_bundle`：整包发布判定
- :func:`release_gate.engine.real_open_segments`：单条豁免真实开放区间
- :class:`release_gate.review.DecisionRepository`：决定落盘与事后复核
- :func:`release_gate.loader.load_bundle` / :func:`load_bundle_file`：JSON 输入
"""

from .engine import (
    BundleEvaluation,
    Decision,
    REASON_OUT_OF_SCOPE,
    REASON_PROJECTION_EMPTY,
    REASON_QUOTA_EXCEEDED,
    REASON_QUOTA_MISSING,
    evaluate_application,
    evaluate_bundle,
    real_open_segments,
    used_open_intervals,
)
from .intervals import Interval, merge, union_duration
from .loader import load_bundle, load_bundle_file
from .models import (
    CLOSE,
    OPEN,
    ControlQuota,
    EvaluationBundle,
    Exemption,
    GateEvent,
    NewApplication,
)
from .review import DecisionRepository, ReviewReport, StoredDecision

__all__ = [
    "BundleEvaluation",
    "Decision",
    "DecisionRepository",
    "ReviewReport",
    "StoredDecision",
    "Interval",
    "merge",
    "union_duration",
    "load_bundle",
    "load_bundle_file",
    "evaluate_bundle",
    "evaluate_application",
    "real_open_segments",
    "used_open_intervals",
    "EvaluationBundle",
    "ControlQuota",
    "Exemption",
    "GateEvent",
    "NewApplication",
    "OPEN",
    "CLOSE",
    "REASON_OUT_OF_SCOPE",
    "REASON_QUOTA_MISSING",
    "REASON_QUOTA_EXCEEDED",
    "REASON_PROJECTION_EMPTY",
]
