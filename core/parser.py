"""协议解析器：rpa-order/v1（JSON）。

声明支持的协议子集（详见 README）：
- 顶层字段：protocol(必须等于 "rpa-order/v1")、scenario_id、title、steps、
  constraints、source、interference；未知顶层字段直接报错，不静默忽略。
- steps：1..64 个步骤，元素含 id（^[A-Za-z0-9_-]{1,32}$）、name、可选 group。
- constraints.fixed：步骤 id -> 固定下标，下标范围 [0, 步骤数-1]。
- constraints.depends_on：步骤 id -> 前置步骤 id 列表（仅支持直接前置边，
  传递关系由求解器推导；不支持外部步骤引用）。
- 同组约束：相同 group 的步骤在 steps 中的原始相对顺序必须保持，
  由解析器展开为 depends_on 边；协议本身不含 group_order 等其他表达。
- 不支持：软约束、权重、互斥、跨文件引用、YAML/TOML 等其他编码。
结构校验使用成熟开源库 jsonschema，业务规则在 planner 中检查。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from .models import Spec, Step

PROTOCOL_ID = "rpa-order/v1"

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["protocol", "scenario_id", "title", "steps"],
    "properties": {
        "protocol": {"type": "string", "const": PROTOCOL_ID},
        "scenario_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "title": {"type": "string", "minLength": 1, "maxLength": 128},
        "source": {"type": "string", "maxLength": 256},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name"],
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9_-]{1,32}$",
                    },
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "group": {"type": ["string", "null"], "maxLength": 32},
                },
            },
        },
        "constraints": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "fixed": {
                    "type": "object",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
                "depends_on": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    },
                },
            },
        },
        "interference": {
            "type": "object",
            "additionalProperties": False,
            "required": ["after_moves", "swap_a", "swap_b"],
            "properties": {
                "after_moves": {"type": "integer", "minimum": 0},
                "swap_a": {"type": "string"},
                "swap_b": {"type": "string"},
            },
        },
    },
}


class ProtocolError(ValueError):
    """协议子集之外或结构非法的输入。"""


def parse_obj(obj: Any) -> Spec:
    """把已解码的 JSON 对象解析为 Spec；非法输入抛 ProtocolError。"""
    try:
        jsonschema.validate(instance=obj, schema=_SCHEMA)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise ProtocolError(f"协议校验失败（{path}）：{exc.message}") from exc

    raw_steps = obj["steps"]
    ids = [s["id"] for s in raw_steps]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ProtocolError(f"步骤 id 必须唯一，重复 id：{', '.join(dupes)}")
    id_set = set(ids)

    constraints = obj.get("constraints", {}) or {}
    fixed_raw = constraints.get("fixed", {}) or {}
    deps_raw = constraints.get("depends_on", {}) or {}

    unknown_fixed = sorted(set(fixed_raw) - id_set)
    if unknown_fixed:
        raise ProtocolError(f"fixed 引用了不存在的步骤：{', '.join(unknown_fixed)}")
    unknown_dep = sorted(
        {k for k in deps_raw if k not in id_set}
        | {v for vals in deps_raw.values() for v in vals if v not in id_set}
    )
    if unknown_dep:
        raise ProtocolError(
            f"depends_on 引用了不存在的步骤：{', '.join(unknown_dep)}"
        )

    n = len(raw_steps)
    out_of_range = sorted(
        f"{sid}->槽位{pos}" for sid, pos in fixed_raw.items() if pos >= n
    )
    if out_of_range:
        raise ProtocolError(
            f"固定下标超出范围（共 {n} 个槽位，合法区间 0..{n - 1}）："
            f"{', '.join(out_of_range)}"
        )

    # 同组约束展开：组内按 steps 中的原始先后加边，保持原相对顺序（去重）。
    depends_on: dict[str, list[str]] = {sid: [] for sid in ids}
    for sid, prereqs in deps_raw.items():
        for p in prereqs:
            if p not in depends_on[sid]:
                depends_on[sid].append(p)
    group_last: dict[str, str] = {}
    for raw in raw_steps:
        sid = raw["id"]
        grp = raw.get("group")
        if grp:
            if grp in group_last and group_last[grp] not in depends_on[sid]:
                depends_on[sid].append(group_last[grp])
            group_last[grp] = sid

    steps = [
        Step(id=s["id"], name=s["name"], group=s.get("group")) for s in raw_steps
    ]
    return Spec(
        scenario_id=obj["scenario_id"],
        title=obj["title"],
        steps=steps,
        fixed=dict(fixed_raw),
        depends_on=depends_on,
        interference=obj.get("interference"),
        source=obj.get("source", ""),
    )


def parse_text(text: str) -> Spec:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
    return parse_obj(obj)


def parse_file(path: str | Path) -> Spec:
    p = Path(path)
    return parse_text(p.read_text(encoding="utf-8"))
