"""Login-name migration planning.

See :mod:`login_migration.migrator` for the rules and the public API.
"""

from .migrator import (
    Account,
    Alias,
    Conflict,
    Group,
    MigrationError,
    MigrationPlan,
    MigratedState,
    apply_plan,
    build_plan,
    plan_migration,
    COMPONENT_DEFERRED,
    DUPLICATE_TARGET_KEY,
    OWNED_BY_OTHER_ACCOUNT,
    PREEXISTING_COLLISION,
    RESERVED_BY_ALIAS,
)

__all__ = [
    "Account",
    "Alias",
    "Conflict",
    "Group",
    "MigrationError",
    "MigrationPlan",
    "MigratedState",
    "apply_plan",
    "build_plan",
    "plan_migration",
    "COMPONENT_DEFERRED",
    "DUPLICATE_TARGET_KEY",
    "OWNED_BY_OTHER_ACCOUNT",
    "PREEXISTING_COLLISION",
    "RESERVED_BY_ALIAS",
]
