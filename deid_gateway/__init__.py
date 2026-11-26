"""deid_gateway - project isolated DICOM UID de-identification gateway.

Public API
----------
High level::

    from deid_gateway import DeidGateway, process_tree, ProcessReport

Core mapping / UID primitives::

    from deid_gateway import (
        UidMapper, ShortCodeGenerator, ScriptedShortCodeGenerator,
        FieldDictionary, is_legal_uid, MappingConflictError,
    )

All modules are standard-library only.
"""

from .dictionary import (
    CONSTANT_ROLES,
    TRANSFER_SYNTAX_UIDS,
    FieldDictionary,
    FieldEntry,
    normalize_tag,
    normalize_vr,
)
from .gateway import (
    DeidGateway,
    DeidentificationGateway,
    Deidentifier,
    DicomDeidentifier,
    ExportResult,
    Gateway,
    deidentify,
)
from .processor import (
    ProcessReport,
    TreeProcessor,
    deidentify_dicom,
    deidentify_tree,
    looks_like_uid_name,
    process_attribute_tree,
    process_dicom,
    process_json_tree,
    process_tree,
)
from .uids import (
    DEFAULT_BASE_ROOT,
    MAPPING_FORMAT,
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

__version__ = "1.0.0"

__all__ = [
    # gateway
    "DeidGateway", "DeidentificationGateway", "DicomDeidentifier",
    "Deidentifier", "Gateway",
    "ExportResult", "deidentify",
    # processing
    "process_tree", "process_attribute_tree", "process_json_tree",
    "deidentify_tree", "process_dicom", "deidentify_dicom",
    "TreeProcessor", "ProcessReport",
    "looks_like_uid_name",
    # dictionary
    "FieldDictionary", "FieldEntry", "normalize_tag", "normalize_vr",
    "TRANSFER_SYNTAX_UIDS", "CONSTANT_ROLES",
    # uids / mapping
    "UidMapper", "UidMapping", "UidMappingStore", "UidRegistry",
    "ShortCodeGenerator", "ScriptedShortCodeGenerator",
    "UidGenerator", "ShortUidGenerator", "CodeGenerator",
    "is_legal_uid", "assert_legal_uid", "canonical_short_code",
    "MappingConflictError", "UidCollisionError", "IllegalUidError",
    "InvalidShortCodeError", "DEFAULT_BASE_ROOT", "MAPPING_FORMAT",
    "__version__",
]
