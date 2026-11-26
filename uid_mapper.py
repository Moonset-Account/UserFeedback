"""Compatibility shim: ``import uid_mapper`` / ``from uid_mapper import UidMapper``."""

from deid_gateway.uids import (  # noqa: F401
    DEFAULT_BASE_ROOT,
    CodeGenerator,
    IllegalUidError,
    InvalidShortCodeError,
    MappingConflictError,
    ShortCodeGenerator,
    ShortUidGenerator,
    ScriptedShortCodeGenerator,
    UidCollisionError,
    UidGenerator,
    UidMapper,
    UidMapping,
    UidMappingStore,
    UidRegistry,
    assert_legal_uid,
    canonical_short_code,
    is_legal_uid,
)

__all__ = [
    "UidMapper", "UidMapping", "UidMappingStore", "UidRegistry",
    "ShortCodeGenerator", "ScriptedShortCodeGenerator",
    "UidGenerator", "ShortUidGenerator", "CodeGenerator",
    "MappingConflictError", "UidCollisionError", "IllegalUidError",
    "InvalidShortCodeError", "is_legal_uid", "assert_legal_uid",
    "canonical_short_code", "DEFAULT_BASE_ROOT",
]
