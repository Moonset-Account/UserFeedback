"""裁决输入的 JSON 加载器。

载荷结构（所有标识均为字符串，数值为非负数）::

    {
      "default_go_live_limit": 30.0,          // 可选
      "assets": [
        {"asset_id": "A1", "low": 40, "high": 100,
         "required": true, "go_live_limit": 30.0}   // 后两项可选
      ],
      "segments": [
        {"segment_id": "S1", "asset_id": "A1", "low": 40, "high": 100}
      ],
      "controls": [
        {"control_id": "C1",
         "coverage": {"S1": 20.0},
         "common_cause_group": "G1"}          // 可选，缺省为独立控制
      ],
      "receipts": [
        {"receipt_id": "R1", "control_id": "C1",
         "status": "valid", "detail": "演练通过"}  // detail 可选
      ]
    }
"""

from __future__ import annotations

from typing import Any, Optional

from governance.errors import GovernanceInputError
from governance.model import Asset, Control, Segment, ValidationReceipt


def _require_mapping(payload: Any, label: str) -> dict:
    if not isinstance(payload, dict):
        raise GovernanceInputError(f"{label} 必须是 JSON 对象")
    return payload


def _number(value: Any, label: str, *, allow_none: bool = False) -> Optional[float]:
    if value is None and allow_none:
        return None
    # bool 是 int 的子类，显式排除，避免 true 被当作 1。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GovernanceInputError(f"{label} 必须是数值，实际为 {value!r}")
    return float(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GovernanceInputError(f"{label} 必须是非空字符串")
    return value


def load_application_payload(payload: Any) -> tuple[
    list[Asset],
    list[Segment],
    list[Control],
    list[ValidationReceipt],
    Optional[float],
]:
    """把已解析的 JSON 结构转换为裁决模型对象。

    结构层面的完整性在此检查；实体之间的交叉约束由
    :func:`governance.engine.evaluate_application` 统一校验。
    """
    payload = _require_mapping(payload, "载荷根对象")

    default_limit = _number(
        payload.get("default_go_live_limit"),
        "default_go_live_limit",
        allow_none=True,
    )

    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise GovernanceInputError("assets 必须是数组")
    assets: list[Asset] = []
    for index, raw in enumerate(raw_assets):
        raw = _require_mapping(raw, f"assets[{index}]")
        assets.append(
            Asset(
                asset_id=_string(raw.get("asset_id"), f"assets[{index}].asset_id"),
                low=_number(raw.get("low"), f"assets[{index}].low"),  # type: ignore[arg-type]
                high=_number(raw.get("high"), f"assets[{index}].high"),  # type: ignore[arg-type]
                required=bool(raw.get("required", True)),
                go_live_limit=_number(
                    raw.get("go_live_limit"),
                    f"assets[{index}].go_live_limit",
                    allow_none=True,
                ),
            )
        )

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise GovernanceInputError("segments 必须是数组")
    segments: list[Segment] = []
    for index, raw in enumerate(raw_segments):
        raw = _require_mapping(raw, f"segments[{index}]")
        segments.append(
            Segment(
                segment_id=_string(
                    raw.get("segment_id"), f"segments[{index}].segment_id"
                ),
                asset_id=_string(
                    raw.get("asset_id"), f"segments[{index}].asset_id"
                ),
                low=_number(raw.get("low"), f"segments[{index}].low"),  # type: ignore[arg-type]
                high=_number(raw.get("high"), f"segments[{index}].high"),  # type: ignore[arg-type]
            )
        )

    raw_controls = payload.get("controls")
    if not isinstance(raw_controls, list):
        raise GovernanceInputError("controls 必须是数组")
    controls: list[Control] = []
    for index, raw in enumerate(raw_controls):
        raw = _require_mapping(raw, f"controls[{index}]")
        raw_coverage = raw.get("coverage")
        if not isinstance(raw_coverage, dict) or not raw_coverage:
            raise GovernanceInputError(
                f"controls[{index}].coverage 必须是非空对象"
            )
        coverage: dict[str, float] = {}
        for segment_id, amount in raw_coverage.items():
            coverage[_string(segment_id, "coverage 的风险段标识")] = _number(
                amount, f"controls[{index}].coverage[{segment_id!r}]"
            )  # type: ignore[assignment]
        group = raw.get("common_cause_group")
        controls.append(
            Control(
                control_id=_string(
                    raw.get("control_id"), f"controls[{index}].control_id"
                ),
                coverage=coverage,
                common_cause_group=(
                    _string(group, "common_cause_group")
                    if group is not None
                    else None
                ),
            )
        )

    raw_receipts = payload.get("receipts", [])
    if not isinstance(raw_receipts, list):
        raise GovernanceInputError("receipts 必须是数组")
    receipts: list[ValidationReceipt] = []
    for index, raw in enumerate(raw_receipts):
        raw = _require_mapping(raw, f"receipts[{index}]")
        receipts.append(
            ValidationReceipt(
                receipt_id=_string(
                    raw.get("receipt_id"), f"receipts[{index}].receipt_id"
                ),
                control_id=_string(
                    raw.get("control_id"), f"receipts[{index}].control_id"
                ),
                status=_string(raw.get("status"), f"receipts[{index}].status"),
                detail=str(raw.get("detail", "")),
            )
        )

    return assets, segments, controls, receipts, default_limit
