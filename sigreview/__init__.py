"""sigreview —— 证书用途限制的离线回归审查模块。

模块组成：

* :mod:`sigreview.verifier`：验签域模型、严格 / 兼容策略、验签引擎与合成助手；
* :mod:`sigreview.synthetic`：本地合成记录构造器与固定场景目录；
* :mod:`sigreview.review`：回归审查器、证据对与报告渲染。

本模块完全离线，仅使用 Python 标准库；合成记录中出现的“签名”只是
:mod:`hmac` 摘要，用于驱动判定逻辑，不构成真实密码学签名实现。
"""

from .verifier import (
    CheckResult,
    Decision,
    Purpose,
    RejectReason,
    StrategyName,
    Certificate,
    SignedRequest,
    Trace,
    Attempt,
    CompatibilityPolicy,
    StrictPolicy,
    VerificationEngine,
    compute_digest,
    sign_payload,
)
from .review import (
    NegativeClass,
    MissingObservation,
    EvidencePair,
    InterfaceReport,
    ReviewReport,
    RegressionReviewer,
)
from .synthetic import (
    RecordBuilder,
    build_fixture_traces,
    INTERFACE_SPECS,
)

__all__ = [
    "CheckResult",
    "Decision",
    "Purpose",
    "RejectReason",
    "StrategyName",
    "Certificate",
    "SignedRequest",
    "Trace",
    "Attempt",
    "CompatibilityPolicy",
    "StrictPolicy",
    "VerificationEngine",
    "compute_digest",
    "sign_payload",
    "NegativeClass",
    "MissingObservation",
    "EvidencePair",
    "InterfaceReport",
    "ReviewReport",
    "RegressionReviewer",
    "RecordBuilder",
    "build_fixture_traces",
    "INTERFACE_SPECS",
]
