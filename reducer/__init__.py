"""结构化样本缩减库。

公共入口：

* :class:`Sample` / :class:`Record` / :class:`FieldSpec`：数据模型；
* :class:`StructuralValidator` / :class:`CallableValidator`：格式校验；
* :class:`Reducer`：缩减引擎（闭包删除、字段缩减、预算、暂停恢复）；
* :class:`TrialCache`：内容/试验器版本/重复次数三要素绑定缓存；
* :mod:`reducer.stubs`：确定性试验桩与演示样本；
* :mod:`reducer.tester`：试验器协议与结果分类。
"""

from .cache import CacheKey, TrialCache
from .engine import (
    Reducer,
    ReductionReport,
    Transform,
    TransformAttempt,
    apply_transform,
    dependency_closure,
    generate_transforms,
)
from .model import FieldSpec, Record, Sample
from .stubs import TARGET_FINGERPRINT
from .tester import (
    STATUS_ERROR,
    STATUS_FP_ERROR,
    STATUS_MATCH,
    STATUS_OTHER_FP,
    STATUS_TIMEOUT,
    TrialResult,
    safe_run,
)
from .validator import CallableValidator, StructuralValidator

__all__ = [
    "CacheKey",
    "TrialCache",
    "Reducer",
    "ReductionReport",
    "Transform",
    "TransformAttempt",
    "apply_transform",
    "dependency_closure",
    "generate_transforms",
    "FieldSpec",
    "Record",
    "Sample",
    "TARGET_FINGERPRINT",
    "STATUS_ERROR",
    "STATUS_FP_ERROR",
    "STATUS_MATCH",
    "STATUS_OTHER_FP",
    "STATUS_TIMEOUT",
    "TrialResult",
    "safe_run",
    "CallableValidator",
    "StructuralValidator",
]
