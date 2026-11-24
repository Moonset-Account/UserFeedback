"""ABI-specific decode tables and source-sequence version selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .model import ABI_ANY, EvidenceGap, GapCode, ParamPath


@dataclass(frozen=True)
class ObjectSpec:
    """Declarative description of how to extract an action target."""

    kind: str = "object"
    identifier: ParamPath | None = None
    fields: Mapping[str, ParamPath] = field(default_factory=dict)
    optional_fields: Mapping[str, ParamPath] = field(default_factory=dict)


@dataclass(frozen=True)
class SubOperation:
    name: str
    target: ObjectSpec | None = None


@dataclass(frozen=True)
class MultiplexSpec:
    """One entry point carrying several permission actions."""

    selector: ParamPath
    operations: Mapping[Any, SubOperation]
    selector_label: str = "suboperation"


@dataclass(frozen=True)
class ActionMapping:
    """Mapping from one ABI entry/code to a permission action."""

    name: str
    target: ObjectSpec | None = None
    multiplex: MultiplexSpec | None = None


@dataclass(frozen=True)
class TableVersion:
    """A decode table edition.

    ``valid_from_seq`` is inclusive.  ``valid_to_seq`` is optional and also
    inclusive; ordinary rolling editions only need ``valid_from_seq`` because
    the newest applicable edition supersedes earlier open-ended editions.
    """

    version: str | None = None
    mappings: Mapping[str, Mapping[Any, ActionMapping]] = field(default_factory=dict)
    valid_from_seq: Any = None
    valid_to_seq: Any = None
    code_selector: ParamPath | Mapping[str, ParamPath] | None = None

    def code_selector_for(self, abi: str | None) -> ParamPath | None:
        return _selector_for_abi(self.code_selector, abi)

    def find_mapping(self, abi: str | None, action_code: Any) -> ActionMapping | None:
        if action_code is None:
            return None

        if abi is not None and abi != ABI_ANY:
            found = _mapping_get(self.mappings.get(abi, {}), action_code)
            if found is None:
                found = _mapping_get(
                    self.mappings.get(str(abi).strip().lower(), {}), action_code
                )
            if found is not None:
                return found

        # An explicit "*" ABI section is a declared ABI-independent mapping.
        return _mapping_get(self.mappings.get(ABI_ANY, {}), action_code)


# Descriptive alias for callers who prefer the "decode table" vocabulary.
DecodeTable = TableVersion


class TableSchedule:
    """Versioned decode tables selectable by source sequence."""

    def __init__(
        self,
        versions: Sequence[TableVersion | Mapping[str, Any]] | TableVersion | Mapping[str, Any] | None = None,
        *,
        default_code_selector: ParamPath | Mapping[str, ParamPath] | None = None,
    ) -> None:
        self.default_code_selector = default_code_selector
        self._versions: list[TableVersion] = []
        if versions is None:
            return
        if isinstance(versions, TableSchedule):
            self._versions = list(versions._versions)
            self.default_code_selector = (
                default_code_selector or versions.default_code_selector
            )
            return
        if isinstance(versions, (TableVersion, Mapping)):
            items = [versions]
        elif isinstance(versions, Sequence) and not isinstance(versions, (str, bytes)):
            items = list(versions)
        else:
            raise TypeError("tables must be a table, mapping, or sequence of table versions")
        for item in items:
            self.add_version(item)

    @property
    def versions(self) -> tuple[TableVersion, ...]:
        return tuple(self._versions)

    def add_version(self, table: TableVersion | Mapping[str, Any]) -> TableSchedule:
        self._versions.append(coerce_table_version(table))
        return self

    def default_code_selector_for(self, abi: str | None) -> ParamPath | None:
        return _selector_for_abi(self.default_code_selector, abi)

    def resolve(self, source_seq: Any) -> tuple[TableVersion | None, list[EvidenceGap]]:
        if source_seq is None:
            return None, [
                EvidenceGap(
                    GapCode.TABLE_VERSION_UNKNOWN,
                    "source_seq 缺失，无法选择解码表版本。",
                )
            ]

        if not self._versions:
            return None, [
                EvidenceGap(
                    GapCode.TABLE_VERSION_UNKNOWN,
                    "未提供任何解码表版本。",
                    {"source_seq": source_seq},
                )
            ]

        covering: list[TableVersion] = []
        for table in self._versions:
            try:
                if table.valid_from_seq is not None and source_seq < table.valid_from_seq:
                    continue
                if table.valid_to_seq is not None and source_seq > table.valid_to_seq:
                    continue
            except TypeError as exc:
                return None, [
                    EvidenceGap(
                        GapCode.TABLE_VERSION_CONFLICT,
                        "解码表的源序号有效区间边界不可比较。",
                        {
                            "source_seq": source_seq,
                            "table_version": table.version,
                            "error": str(exc),
                        },
                    )
                ]
            covering.append(table)

        if not covering:
            known = [t.version for t in self._versions]
            return None, [
                EvidenceGap(
                    GapCode.TABLE_VERSION_UNKNOWN,
                    "没有覆盖该动作源序号的解码表版本。",
                    {"source_seq": source_seq, "known_versions": known},
                )
            ]

        unique: list[TableVersion] = []
        for table in covering:
            if not any(table == existing for existing in unique):
                unique.append(table)
        covering = unique

        def start_key(table: TableVersion) -> Any:
            return table.valid_from_seq

        has_any_start = any(t.valid_from_seq is not None for t in covering)
        if has_any_start:
            max_start = max(
                t.valid_from_seq for t in covering if t.valid_from_seq is not None
            )
            latest = [
                t
                for t in covering
                if t.valid_from_seq is not None and t.valid_from_seq == max_start
            ]
            # Tables without any start bound are baseline tables and are
            # superseded by a concrete later start.
            if not latest:
                latest = covering
        else:
            latest = covering

        if len(latest) > 1:
            return None, [
                EvidenceGap(
                    GapCode.TABLE_VERSION_CONFLICT,
                    "多个不同解码表版本在同一起始位置生效，无法安全选择。",
                    {
                        "source_seq": source_seq,
                        "versions": [t.version for t in latest],
                        "valid_from_seq": [t.valid_from_seq for t in latest],
                    },
                )
            ]

        selected = latest[0]

        # An explicit finite range on another covering edition means the
        # evidence claims overlapping validity, which is not safe to choose.
        overlapping_finite = [
            t.version
            for t in covering
            if t is not selected and t.valid_to_seq is not None
        ]
        if overlapping_finite:
            return None, [
                EvidenceGap(
                    GapCode.TABLE_VERSION_CONFLICT,
                    "解码表有效区间在该动作位置重叠，无法确定唯一版本。",
                    {
                        "source_seq": source_seq,
                        "selected_version": selected.version,
                        "overlapping_versions": overlapping_finite,
                    },
                )
            ]

        gaps: list[EvidenceGap] = []
        if selected.version is None or str(selected.version).strip().lower() in {
            "",
            "unknown",
            "unversioned",
        }:
            gaps.append(
                EvidenceGap(
                    GapCode.TABLE_VERSION_UNKNOWN,
                    "映射内容可用，但该解码表的版本标识未知。",
                    {"source_seq": source_seq},
                )
            )

        return selected, gaps


def coerce_table_version(value: TableVersion | Mapping[str, Any]) -> TableVersion:
    if isinstance(value, TableVersion):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("table version must be a TableVersion or mapping")

    version = value.get("version", value.get("name", value.get("table_version")))
    valid_from = _first_present(
        value, "valid_from_seq", "from_seq", "since_seq", "start_seq", "at_seq"
    )
    valid_to = _first_present(value, "valid_to_seq", "to_seq", "until_seq", "end_seq")
    code_selector = value.get("code_selector", value.get("entry_selector"))

    raw_mappings = value.get(
        "mappings", value.get("abis", value.get("abi_mappings", {}))
    )
    if not isinstance(raw_mappings, Mapping):
        raise ValueError("table 'mappings' must be a mapping of ABI to action-code map")

    mappings: dict[str, dict[Any, ActionMapping]] = {}
    for abi, code_map in raw_mappings.items():
        if not isinstance(code_map, Mapping):
            raise ValueError(f"mapping for ABI {abi!r} must be a code-to-action mapping")
        abi_key = str(abi).strip().lower()
        mappings[abi_key] = {
            code: coerce_action_mapping(action) for code, action in code_map.items()
        }

    return TableVersion(
        version=None if version is None else str(version),
        mappings=mappings,
        valid_from_seq=valid_from,
        valid_to_seq=valid_to,
        code_selector=code_selector,
    )


def coerce_action_mapping(value: ActionMapping | Mapping[str, Any] | str) -> ActionMapping:
    if isinstance(value, ActionMapping):
        return value
    if isinstance(value, str):
        return ActionMapping(name=value)
    if not isinstance(value, Mapping):
        raise TypeError("action mapping must be an ActionMapping, mapping, or action name")

    name = value.get("name", value.get("action"))
    if not name:
        raise ValueError("action mapping requires a 'name'")

    target = _target_from_value(value.get("target"))
    if target is None and (
        "target_param" in value or "target_kind" in value or "target_id_param" in value
    ):
        target = ObjectSpec(
            kind=value.get("target_kind", "object"),
            identifier=value.get(
                "target_param", value.get("target_id_param", value.get("identifier_param"))
            ),
        )

    multiplex_data = value.get("multiplex")
    if multiplex_data is None and (
        "operations" in value or "selector" in value or "selector_param" in value
    ):
        multiplex_data = value

    multiplex: MultiplexSpec | None = None
    if multiplex_data is not None:
        if not isinstance(multiplex_data, Mapping):
            raise ValueError("multiplex mapping must be a mapping")
        selector = multiplex_data.get(
            "selector",
            multiplex_data.get("selector_param", multiplex_data.get("parameter")),
        )
        if selector is None:
            raise ValueError("multiplex mapping requires 'selector'/'selector_param'")
        raw_operations = multiplex_data.get("operations", multiplex_data.get("suboperations", {}))
        if not isinstance(raw_operations, Mapping):
            raise ValueError("multiplex 'operations' must be a mapping")
        operations = {
            key: coerce_sub_operation(op) for key, op in raw_operations.items()
        }
        multiplex = MultiplexSpec(
            selector=selector,
            operations=operations,
            selector_label=str(
                multiplex_data.get("selector_label", multiplex_data.get("parameter_name", "suboperation"))
            ),
        )

    return ActionMapping(name=str(name), target=target, multiplex=multiplex)


def coerce_sub_operation(value: SubOperation | Mapping[str, Any] | str | ActionMapping) -> SubOperation:
    if isinstance(value, SubOperation):
        return value
    if isinstance(value, ActionMapping):
        return SubOperation(name=value.name, target=value.target)
    if isinstance(value, str):
        return SubOperation(name=value)
    if isinstance(value, Mapping):
        name = value.get("name", value.get("action"))
        if not name:
            raise ValueError("suboperation requires a 'name'")
        target = _target_from_value(value.get("target"))
        if target is None and ("target_param" in value or "target_kind" in value):
            target = ObjectSpec(
                kind=value.get("target_kind", "object"),
                identifier=value.get("target_param", value.get("target_id_param")),
            )
        return SubOperation(name=str(name), target=target)
    raise TypeError("suboperation must be a SubOperation, mapping, or string")


def _target_from_value(value: Any) -> ObjectSpec | None:
    if value is None:
        return None
    if isinstance(value, ObjectSpec):
        return value
    if isinstance(value, int):
        return ObjectSpec(kind="object", identifier=value)
    if isinstance(value, str):
        # A string target in a table is treated as an object kind, not as a
        # parameter reference; parameter references are usually integers.
        return ObjectSpec(kind=value)
    if not isinstance(value, Mapping):
        raise TypeError("target spec must be an ObjectSpec, mapping, int, or kind string")

    identifier = value.get(
        "identifier",
        value.get("param", value.get("id_param", value.get("target_param"))),
    )
    fields = value.get("fields", {})
    optional_fields = value.get("optional_fields", {})
    if not isinstance(fields, Mapping) or not isinstance(optional_fields, Mapping):
        raise ValueError("target fields must be mappings of field name to parameter reference")
    return ObjectSpec(
        kind=str(value.get("kind", value.get("type", "object"))),
        identifier=identifier,
        fields=dict(fields),
        optional_fields=dict(optional_fields),
    )


def _selector_for_abi(
    selector: ParamPath | Mapping[str, ParamPath] | None,
    abi: str | None,
) -> ParamPath | None:
    if selector is None:
        return None
    if isinstance(selector, Mapping):
        if abi is not None and abi in selector:
            return selector[abi]
        return selector.get(ABI_ANY)
    return selector


def _mapping_get(code_map: Mapping[Any, ActionMapping], code: Any) -> ActionMapping | None:
    if code in code_map:
        return code_map[code]

    for candidate in code_key_candidates(code):
        if candidate in code_map:
            return code_map[candidate]
    return None


def code_key_candidates(code: Any):
    yield code
    if isinstance(code, bool):
        return
    if isinstance(code, int):
        yield str(code)
    elif isinstance(code, str):
        try:
            yield int(code, 10)
        except (TypeError, ValueError):
            pass


def _first_present(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None
