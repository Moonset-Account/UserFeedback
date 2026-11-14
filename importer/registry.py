"""Static registry of versions, task types, fields, and extensions.

The registry is a data table.  Nothing here resolves a task type to a module,
attribute, class, or callable by name, so a record can only ever be decoded by
one of the decoders written in this file.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import paths
from .errors import (
    INVALID_VALUE,
    MISSING_FIELD,
    TYPE_ERROR,
    UNKNOWN_EXTENSION,
    UNKNOWN_FIELD,
    UNREGISTERED_COMBINATION,
    UNREGISTERED_TASK_TYPE,
    UNREGISTERED_VERSION,
    ImportFailure,
)

TEXT_CLASSIFICATION = "text_classification"
DOCUMENT_ANALYSIS = "document_analysis"


@dataclass(frozen=True)
class ExtensionSpec:
    """One extension field permitted by a version table."""

    kind: str  # "string" | "string_list" | "object"
    description: str


@dataclass(frozen=True)
class FieldSpec:
    """Maps a version-specific field name to its canonical name."""

    declared: str
    canonical: str
    kind: str  # "string" | "string_list" | "document"


@dataclass(frozen=True)
class TaskSpec:
    version: str
    declared_task_type: str
    canonical_task_type: str
    fields: tuple[FieldSpec, ...]
    extensions: dict[str, ExtensionSpec] = field(default_factory=dict)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(spec.declared for spec in self.fields)


@dataclass(frozen=True)
class VersionTable:
    version: str
    tasks: dict[str, TaskSpec]
    extensions: dict[str, ExtensionSpec]


def _ext_string(description: str) -> ExtensionSpec:
    return ExtensionSpec("string", description)


def _ext_string_list(description: str) -> ExtensionSpec:
    return ExtensionSpec("string_list", description)


def _ext_object(description: str) -> ExtensionSpec:
    return ExtensionSpec("object", description)


VERSION_1_TASKS: dict[str, TaskSpec] = {
    "text_label": TaskSpec(
        version="1",
        declared_task_type="text_label",
        canonical_task_type=TEXT_CLASSIFICATION,
        fields=(
            FieldSpec("task_id", "record_id", "string"),
            FieldSpec("content", "text", "string"),
            FieldSpec("answer_labels", "labels", "string_list"),
        ),
    ),
    "document_scan": TaskSpec(
        version="1",
        declared_task_type="document_scan",
        canonical_task_type=DOCUMENT_ANALYSIS,
        fields=(
            FieldSpec("task_id", "record_id", "string"),
            FieldSpec("document_title", "title", "string"),
            FieldSpec("sections", "paragraphs", "string_list"),
        ),
    ),
}

VERSION_1_EXTENSIONS: dict[str, ExtensionSpec] = {
    "legacy_source": _ext_string("legacy subsystem that produced the record"),
    "legacy_tags": _ext_string_list("legacy free-form tags retained verbatim"),
}

VERSION_2_TASKS: dict[str, TaskSpec] = {
    TEXT_CLASSIFICATION: TaskSpec(
        version="2",
        declared_task_type=TEXT_CLASSIFICATION,
        canonical_task_type=TEXT_CLASSIFICATION,
        fields=(
            FieldSpec("record_id", "record_id", "string"),
            FieldSpec("text", "text", "string"),
            FieldSpec("labels", "labels", "string_list"),
        ),
    ),
    DOCUMENT_ANALYSIS: TaskSpec(
        version="2",
        declared_task_type=DOCUMENT_ANALYSIS,
        canonical_task_type=DOCUMENT_ANALYSIS,
        fields=(
            FieldSpec("record_id", "record_id", "string"),
            FieldSpec("document", "document", "document"),
        ),
    ),
}

VERSION_2_EXTENSIONS: dict[str, ExtensionSpec] = {
    "source_system": _ext_string("system that produced the record"),
    "import_batch": _ext_string("batch identifier retained verbatim"),
    "annotations": _ext_object("nested annotations retained verbatim"),
}

#: The complete registry.  Keys are exact strings; there is no pattern matching.
VERSION_TABLES: dict[str, VersionTable] = {
    "1": VersionTable(
        "1",
        {
            name: replace(task, extensions=dict(VERSION_1_EXTENSIONS))
            for name, task in VERSION_1_TASKS.items()
        },
        VERSION_1_EXTENSIONS,
    ),
    "2": VersionTable(
        "2",
        {
            name: replace(task, extensions=dict(VERSION_2_EXTENSIONS))
            for name, task in VERSION_2_TASKS.items()
        },
        VERSION_2_EXTENSIONS,
    ),
}

REGISTERED_VERSIONS: tuple[str, ...] = tuple(sorted(VERSION_TABLES))

#: Declared task types known to at least one version.  Used only to report a
#: more precise classification for a version/type pair that is not registered.
KNOWN_DECLARED_TASK_TYPES: tuple[str, ...] = tuple(
    sorted(
        {
            declared
            for table in VERSION_TABLES.values()
            for declared in table.tasks
        }
    )
)

DOCUMENT_KEYS = ("title", "paragraphs")


def resolve_task_spec(record: dict[str, Any]) -> TaskSpec:
    """Resolve ``(version, task_type)`` from the static table or fail.

    The returned spec is the only thing that can select a decoder.
    """

    for required in ("version", "task_type"):
        if required not in record:
            raise ImportFailure(
                MISSING_FIELD,
                paths.child("$", required),
                f"missing required field {required!r}",
            )

    version = record["version"]
    if not isinstance(version, str):
        raise ImportFailure(
            TYPE_ERROR,
            paths.child("$", "version"),
            f"'version' must be a string, got {type(version).__name__}",
        )
    table = VERSION_TABLES.get(version)
    if table is None:
        raise ImportFailure(
            UNREGISTERED_VERSION,
            paths.child("$", "version"),
            "version is not registered; registered versions: "
            + ", ".join(REGISTERED_VERSIONS),
        )

    declared = record["task_type"]
    if not isinstance(declared, str):
        raise ImportFailure(
            TYPE_ERROR,
            paths.child("$", "task_type"),
            f"'task_type' must be a string, got {type(declared).__name__}",
        )

    spec = table.tasks.get(declared)
    if spec is not None:
        return spec

    if declared in KNOWN_DECLARED_TASK_TYPES:
        raise ImportFailure(
            UNREGISTERED_COMBINATION,
            paths.child("$", "task_type"),
            f"task_type {declared!r} is registered for version "
            + ", ".join(
                sorted(
                    repr(v)
                    for v, t in VERSION_TABLES.items()
                    if declared in t.tasks
                )
            )
            + f" but not for version {version!r}",
        )

    raise ImportFailure(
        UNREGISTERED_TASK_TYPE,
        paths.child("$", "task_type"),
        f"task_type {declared!r} is not registered; registered task types "
        "for version "
        f"{version!r}: " + (", ".join(sorted(table.tasks)) or "(none)"),
    )


def _require_string(value: Any, path: str, label: str) -> str:
    if not isinstance(value, str):
        raise ImportFailure(
            TYPE_ERROR, path, f"{label} must be a string, got {type(value).__name__}"
        )
    if value == "":
        raise ImportFailure(INVALID_VALUE, path, f"{label} must not be empty")
    return value


def _require_string_list(value: Any, path: str, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ImportFailure(
            TYPE_ERROR, path, f"{label} must be a list, got {type(value).__name__}"
        )
    result: list[str] = []
    for position, item in enumerate(value):
        item_path = paths.index(path, position)
        if not isinstance(item, str):
            raise ImportFailure(
                TYPE_ERROR,
                item_path,
                f"{label} item must be a string, got {type(item).__name__}",
            )
        if item == "":
            raise ImportFailure(INVALID_VALUE, item_path, f"{label} item must not be empty")
        result.append(item)
    return result


def _require_document(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImportFailure(
            TYPE_ERROR, path, f"document must be an object, got {type(value).__name__}"
        )

    for key in sorted(value):
        if key not in DOCUMENT_KEYS:
            raise ImportFailure(
                UNKNOWN_FIELD,
                paths.child(path, key),
                f"unknown document field {key!r}; allowed: "
                + ", ".join(DOCUMENT_KEYS),
            )

    for key in DOCUMENT_KEYS:
        if key not in value:
            raise ImportFailure(
                MISSING_FIELD, paths.child(path, key), f"missing document field {key!r}"
            )

    return {
        "title": _require_string(
            value["title"], paths.child(path, "title"), "document.title"
        ),
        "paragraphs": _require_string_list(
            value["paragraphs"],
            paths.child(path, "paragraphs"),
            "document.paragraphs",
        ),
    }


def _validate_extension(spec: ExtensionSpec, value: Any, path: str) -> Any:
    if spec.kind == "string":
        return _require_string(value, path, f"extension {path.rsplit('.', 1)[-1]!r}")
    if spec.kind == "string_list":
        return _require_string_list(value, path, "extension list")
    if spec.kind == "object":
        if not isinstance(value, dict):
            raise ImportFailure(
                TYPE_ERROR,
                path,
                f"extension object must be an object, got {type(value).__name__}",
            )
        # Nested extension content is retained verbatim; resource bounds were
        # already enforced by the structural scan over the whole record.
        return copy.deepcopy(value)
    raise AssertionError(f"unhandled extension kind {spec.kind!r}")


def _decode_record(
    record: dict[str, Any], spec: TaskSpec
) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed_top_level = {"version", "task_type", "extensions", *spec.field_names}
    for key in sorted(record):
        if key not in allowed_top_level:
            raise ImportFailure(
                UNKNOWN_FIELD,
                paths.child("$", key),
                f"unknown field {key!r} for version {spec.version!r} task "
                f"{spec.declared_task_type!r}",
            )

    for field_spec in spec.fields:
        if field_spec.declared not in record:
            raise ImportFailure(
                MISSING_FIELD,
                paths.child("$", field_spec.declared),
                f"missing required field {field_spec.declared!r}",
            )

    canonical: dict[str, Any] = {}
    for field_spec in spec.fields:
        value = record[field_spec.declared]
        path = paths.child("$", field_spec.declared)
        if field_spec.kind == "string":
            canonical[field_spec.canonical] = _require_string(
                value, path, field_spec.declared
            )
        elif field_spec.kind == "string_list":
            canonical[field_spec.canonical] = _require_string_list(
                value, path, field_spec.declared
            )
        elif field_spec.kind == "document":
            canonical[field_spec.canonical] = _require_document(value, path)
        else:  # pragma: no cover - registry data is static
            raise AssertionError(f"unhandled field kind {field_spec.kind!r}")

    extensions: dict[str, Any] = {}
    extensions_value = record.get("extensions", {})
    extensions_path = paths.child("$", "extensions")
    if not isinstance(extensions_value, dict):
        raise ImportFailure(
            TYPE_ERROR,
            extensions_path,
            f"'extensions' must be an object, got {type(extensions_value).__name__}",
        )
    for key in sorted(extensions_value):
        extension_spec = spec.extensions.get(key)
        extension_path = paths.child(extensions_path, key)
        if extension_spec is None:
            raise ImportFailure(
                UNKNOWN_EXTENSION,
                extension_path,
                f"extension {key!r} is not allowed for version {spec.version!r}; "
                "allowed: "
                + (", ".join(sorted(spec.extensions)) or "(none)"),
            )
        extensions[key] = _validate_extension(
            extension_spec, extensions_value[key], extension_path
        )

    return canonical, extensions
