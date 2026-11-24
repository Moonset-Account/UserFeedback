"""Core receipt normalization logic."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .coercion import coerce_action_record
from .model import (
    ActionRecord,
    EvidenceGap,
    GapCode,
    NormalizedReceipt,
    Target,
    lookup_path,
)
from .tables import (
    ActionMapping,
    ObjectSpec,
    TableSchedule,
    TableVersion,
    code_key_candidates,
)
from .timeline import ImageTimeline


class ReceiptNormalizer:
    """Normalize raw action records using versioned process/table evidence."""

    def __init__(
        self,
        tables: TableSchedule
        | Sequence[TableVersion | Mapping[str, Any]]
        | TableVersion
        | Mapping[str, Any]
        | None = None,
        image_switches: ImageTimeline
        | Sequence[Any]
        | Any
        | None = None,
        *,
        default_code_selector: Any = None,
    ) -> None:
        if isinstance(tables, TableSchedule):
            self.tables = tables
        else:
            self.tables = TableSchedule(
                tables, default_code_selector=default_code_selector
            )
        self.images = (
            image_switches
            if isinstance(image_switches, ImageTimeline)
            else ImageTimeline(image_switches)
        )

    def add_table_version(self, table: TableVersion | Mapping[str, Any]) -> ReceiptNormalizer:
        self.tables.add_version(table)
        return self

    def add_image_switch(self, switch: Any) -> ReceiptNormalizer:
        self.images.add(switch)
        return self

    def normalize(self, record: ActionRecord | Mapping[str, Any] | Sequence[Any]) -> NormalizedReceipt:
        action = coerce_action_record(record)
        gaps: list[EvidenceGap] = []

        process, process_gaps = self.images.resolve(
            action.process_instance, action.source_seq
        )
        gaps.extend(process_gaps)

        table, table_gaps = self.tables.resolve(action.source_seq)
        gaps.extend(table_gaps)

        action_code = action.action_code
        if action_code is None:
            code_selector = None
            if table is not None:
                code_selector = table.code_selector_for(process.abi)
            if code_selector is None:
                code_selector = self.tables.default_code_selector_for(process.abi)
            if code_selector is not None:
                found, selected_code = lookup_path(action.params, code_selector)
                if found:
                    action_code = selected_code
                else:
                    gaps.append(
                        EvidenceGap(
                            GapCode.ACTION_CODE_MISSING,
                            "无法按配置的参数引用读取动作码。",
                            {"parameter": _safe_detail(code_selector)},
                        )
                    )

        if action_code is None and not any(
            gap.code == GapCode.ACTION_CODE_MISSING for gap in gaps
        ):
            gaps.append(
                EvidenceGap(
                    GapCode.ACTION_CODE_MISSING,
                    "原始记录未提供动作码，也没有配置 code_selector。",
                )
            )

        mapping: ActionMapping | None = None
        if table is not None and action_code is not None:
            mapping = table.find_mapping(process.abi, action_code)
            if mapping is None and process.abi is not None:
                gaps.append(
                    EvidenceGap(
                        GapCode.ACTION_UNMAPPED,
                        "所选解码表中没有该 ABI 与动作码对应的映射。",
                        {
                            "abi": process.abi,
                            "action_code": _safe_detail(action_code),
                            "table_version": table.version,
                        },
                    )
                )

        action_name, entry_name, selector_value, target, decode_gaps = _decode_mapping(
            mapping, action.params
        )
        gaps.extend(decode_gaps)

        return NormalizedReceipt(
            source_seq=action.source_seq,
            process=process,
            action_code=action_code,
            action_name=action_name,
            entry_name=entry_name,
            target=target,
            table_version=None if table is None else table.version,
            selector_value=selector_value,
            params=action.params,
            gaps=tuple(gaps),
        )

    def normalize_all(
        self, records: Sequence[ActionRecord | Mapping[str, Any] | Sequence[Any]]
    ) -> list[NormalizedReceipt]:
        return [self.normalize(record) for record in records]


def normalize_receipt(
    record: ActionRecord | Mapping[str, Any] | Sequence[Any],
    *,
    tables: Any = None,
    image_switches: Any = None,
    **kwargs: Any,
) -> NormalizedReceipt:
    """One-record convenience API."""

    return ReceiptNormalizer(
        tables=tables, image_switches=image_switches, **kwargs
    ).normalize(record)


def normalize_receipts(
    records: Sequence[Any],
    *,
    tables: Any = None,
    image_switches: Any = None,
    **kwargs: Any,
) -> list[NormalizedReceipt]:
    """Normalize many records using one shared evidence set."""

    return ReceiptNormalizer(
        tables=tables, image_switches=image_switches, **kwargs
    ).normalize_all(records)


def _decode_mapping(
    mapping: ActionMapping | None,
    params: Any,
) -> tuple[str | None, str | None, Any, Target | None, list[EvidenceGap]]:
    if mapping is None:
        return None, None, None, None, []

    gaps: list[EvidenceGap] = []
    entry_name = mapping.name
    action_name: str | None = mapping.name
    selector_value: Any = None
    target_spec: ObjectSpec | None = mapping.target

    if mapping.multiplex is not None:
        mux = mapping.multiplex
        action_name = None
        found, selector_value = lookup_path(params, mux.selector)
        if not found:
            gaps.append(
                EvidenceGap(
                    GapCode.SELECTOR_MISSING,
                    "复合入口缺少映射表指定的子操作选择参数。",
                    {
                        "parameter": _safe_detail(mux.selector),
                        "selector_label": mux.selector_label,
                        "entry_name": entry_name,
                    },
                )
            )
        else:
            operation = mux.operations.get(selector_value)
            if operation is None:
                for candidate in code_key_candidates(selector_value):
                    operation = mux.operations.get(candidate)
                    if operation is not None:
                        break
            if operation is None:
                gaps.append(
                    EvidenceGap(
                        GapCode.SUBOPERATION_UNKNOWN,
                        "子操作选择参数的值未登记在映射表中。",
                        {
                            "selector_label": mux.selector_label,
                            "selector_value": _safe_detail(selector_value),
                            "known_values": [_safe_detail(k) for k in mux.operations],
                            "entry_name": entry_name,
                        },
                    )
                )
            else:
                action_name = operation.name
                if operation.target is not None:
                    target_spec = operation.target

    target, target_gaps = _build_target(target_spec, params)
    gaps.extend(target_gaps)
    return action_name, entry_name, selector_value, target, gaps


def _build_target(
    spec: ObjectSpec | None, params: Any
) -> tuple[Target | None, list[EvidenceGap]]:
    if spec is None:
        return None, []

    gaps: list[EvidenceGap] = []
    identifier: Any = None
    if spec.identifier is not None:
        found, value = lookup_path(params, spec.identifier)
        if found:
            identifier = value
        else:
            gaps.append(
                EvidenceGap(
                    GapCode.TARGET_PARAMETER_MISSING,
                    "映射要求的动作对象标识参数缺失。",
                    {
                        "target_kind": spec.kind,
                        "parameter": _safe_detail(spec.identifier),
                        "field": "identifier",
                    },
                )
            )

    attributes: dict[str, Any] = {}
    for field_name, reference in spec.fields.items():
        found, value = lookup_path(params, reference)
        if found:
            attributes[field_name] = value
        else:
            gaps.append(
                EvidenceGap(
                    GapCode.TARGET_PARAMETER_MISSING,
                    "映射要求的对象属性参数缺失。",
                    {
                        "target_kind": spec.kind,
                        "field": field_name,
                        "parameter": _safe_detail(reference),
                    },
                )
            )

    for field_name, reference in spec.optional_fields.items():
        found, value = lookup_path(params, reference)
        if found:
            attributes[field_name] = value

    target = Target(
        kind=spec.kind,
        identifier=identifier,
        parameter=spec.identifier,
        attributes=attributes,
    )
    return target, gaps


def _safe_detail(value: Any) -> Any:
    """Convert parameter paths and other values into evidence-detail values."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _safe_detail(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_safe_detail(v) for v in value]
    return str(value)
