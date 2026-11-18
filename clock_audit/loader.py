"""把 JSON 描述的数据流转换为事件对象。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Union

from .audit import AuditError
from .model import (
    BaselineProof,
    ClockRead,
    Compare,
    DeadlineCompute,
    Event,
    Restore,
    Save,
)


def events_from_dicts(items: List[dict]) -> List[Event]:
    events: List[Event] = []
    for item in items:
        kind = item.get("type")
        if kind == "clock_read":
            events.append(
                ClockRead(
                    event_id=item["id"],
                    source=item.get("source"),
                    value=item["value"],
                    unit=item.get("unit"),
                    generation=item["generation"],
                )
            )
        elif kind == "deadline_compute":
            events.append(
                DeadlineCompute(
                    event_id=item["id"],
                    base=item["base"],
                    duration=item["duration"],
                    duration_unit=item["duration_unit"],
                )
            )
        elif kind == "save":
            events.append(
                Save(
                    event_id=item["id"],
                    target=item["target"],
                    generation=item["generation"],
                )
            )
        elif kind == "restore":
            events.append(
                Restore(
                    event_id=item["id"],
                    saved=item["saved"],
                    generation=item["generation"],
                )
            )
        elif kind == "baseline_proof":
            events.append(
                BaselineProof(
                    event_id=item["id"],
                    from_generation=item["from_generation"],
                    to_generation=item["to_generation"],
                    offset=item["offset"],
                    unit=item["unit"],
                )
            )
        elif kind == "compare":
            events.append(
                Compare(
                    event_id=item["id"],
                    deadline=item["deadline"],
                    now=item["now"],
                )
            )
        else:
            raise AuditError(f"未知事件类型: {kind!r}")
    return events


def load_events(path: Union[str, Path]) -> List[Event]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return events_from_dicts(data["events"])
