"""应急复盘平台：升级事件归因规则的迁移核验模块。

公开 API 见 :mod:`attribution_migration.migration`，核心数据结构见
:mod:`attribution_migration.model`，声明式规则求值见
:mod:`attribution_migration.engine`。
"""

from .migration import (
    MigrationReport,
    MigratedConclusion,
    ReReviewItem,
    RetainedConclusion,
    verify_migration,
)
from .model import (
    LegacyConclusion,
    NewRule,
    Observation,
    OldRule,
    RuleValidationError,
    EVIDENCE_RANK,
)

__all__ = [
    "verify_migration",
    "MigrationReport",
    "MigratedConclusion",
    "ReReviewItem",
    "RetainedConclusion",
    "LegacyConclusion",
    "NewRule",
    "Observation",
    "OldRule",
    "RuleValidationError",
    "EVIDENCE_RANK",
]
