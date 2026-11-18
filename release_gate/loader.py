"""把 JSON 输入加载为领域模型，并做引用一致性校验。"""

from __future__ import annotations

import json
from pathlib import Path

from .intervals import Interval, parse_dt, parse_duration
from .models import (
    ControlQuota,
    EvaluationBundle,
    Exemption,
    GateEvent,
    NewApplication,
)


def _require(mapping: dict, key: str, context: str):
    if key not in mapping:
        raise ValueError(f"{context} 缺少必填字段 {key!r}")
    return mapping[key]


def load_bundle(data: dict) -> EvaluationBundle:
    """把字典形式的输入构建为 :class:`EvaluationBundle`。"""
    window = Interval.from_dict(_require(data, "window", "输入根对象"))
    environments = list(_require(data, "environments", "输入根对象"))

    quotas: list[ControlQuota] = []
    for item in data.get("quotas", []):
        env_id = _require(item, "env_id", "quota")
        control_id = _require(item, "control_id", "quota")
        if env_id not in environments:
            raise ValueError(f"quota 引用了环境范围外的环境 {env_id!r}")
        quotas.append(
            ControlQuota(
                env_id=env_id,
                control_id=control_id,
                max_open_duration=parse_duration(
                    _require(item, "max_open_duration", f"quota {env_id}/{control_id}")
                ),
            )
        )

    exemptions: list[Exemption] = []
    exemption_ids: set[str] = set()
    for item in data.get("exemptions", []):
        ex_id = _require(item, "exemption_id", "exemption")
        if ex_id in exemption_ids:
            raise ValueError(f"exemption_id 重复: {ex_id!r}")
        exemption_ids.add(ex_id)
        env_id = _require(item, "env_id", f"exemption {ex_id}")
        control_id = _require(item, "control_id", f"exemption {ex_id}")
        exemptions.append(
            Exemption(
                exemption_id=ex_id,
                env_id=env_id,
                control_id=control_id,
                effective_start=parse_dt(_require(item, "effective_start", f"exemption {ex_id}")),
                effective_end=parse_dt(_require(item, "effective_end", f"exemption {ex_id}")),
            )
        )

    events: list[GateEvent] = []
    seen_event_ids: set[str] = set()
    for item in data.get("events", []):
        event_id = _require(item, "event_id", "event")
        if event_id in seen_event_ids:
            raise ValueError(f"event_id 重复: {event_id!r}")
        seen_event_ids.add(event_id)
        ex_id = _require(item, "exemption_id", f"event {event_id}")
        if ex_id not in exemption_ids:
            raise ValueError(
                f"event {event_id} 引用了不存在的 exemption_id {ex_id!r}"
            )
        target = next(e for e in exemptions if e.exemption_id == ex_id)
        env_id = _require(item, "env_id", f"event {event_id}")
        control_id = _require(item, "control_id", f"event {event_id}")
        if (env_id, control_id) != (target.env_id, target.control_id):
            raise ValueError(
                f"event {event_id} 的环境/控制项与豁免 {ex_id} 不一致"
            )
        events.append(
            GateEvent(
                event_id=event_id,
                exemption_id=ex_id,
                env_id=env_id,
                control_id=control_id,
                action=_require(item, "action", f"event {event_id}"),
                occurred_at=parse_dt(_require(item, "occurred_at", f"event {event_id}")),
            )
        )

    applications: list[NewApplication] = []
    for item in data.get("applications", []):
        app_id = _require(item, "application_id", "application")
        env_id = _require(item, "env_id", f"application {app_id}")
        control_id = _require(item, "control_id", f"application {app_id}")
        projected = tuple(
            Interval.from_dict(raw)
            for raw in _require(item, "projected_intervals", f"application {app_id}")
        )
        applications.append(
            NewApplication(
                application_id=app_id,
                env_id=env_id,
                control_id=control_id,
                projected_intervals=projected,
            )
        )

    as_of = parse_dt(data["as_of"]) if data.get("as_of") else None
    return EvaluationBundle(
        window=window,
        environments=environments,
        quotas=quotas,
        exemptions=exemptions,
        events=events,
        applications=applications,
        as_of=as_of,
    )


def load_bundle_file(path: str | Path) -> EvaluationBundle:
    """从 JSON 文件加载判定输入。"""
    with open(path, "r", encoding="utf-8") as handle:
        return load_bundle(json.load(handle))
