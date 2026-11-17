"""可信采集端事件的数据模型与反序列化。

事件由可信采集端给出，分析器不信任路径作为文件身份，身份以 ``file_id``
（稳定的文件标识，如 inode/卷内唯一编号）为准；``path`` 只用于展示与
独立目录计数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


class EventType(str, Enum):
    """采集端支持的事件类型。"""

    OPEN = "open"          # 打开文档
    WRITE = "write"        # 写入，携带写前/写后内容类别与熵区间
    CLOSE = "close"        # 关闭，携带关闭时确认的内容类别与熵区间
    RENAME = "rename"      # 重命名，携带新路径
    HARDLINK = "hardlink"  # 新建硬链接，携带新路径
    UNLINK = "unlink"      # 删除路径（可选，便于还原身份轨迹）
    SPAWN = "spawn"        # 进程派生，携带父/子进程标识
    WATERMARK = "watermark"  # 显式水位线：该时间及之前的事件均已送达


@dataclass(frozen=True)
class EntropyRange:
    """熵区间（闭区间），None 端点表示无下界/上界。"""

    low: Optional[float] = None
    high: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high}


def _entropy(value: Any) -> Optional[EntropyRange]:
    if value is None:
        return None
    if isinstance(value, EntropyRange):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"非法熵区间: {value!r}")
    low = value.get("low")
    high = value.get("high")
    if low is not None and not isinstance(low, (int, float)):
        raise ValueError(f"非法熵区间下界: {low!r}")
    if high is not None and not isinstance(high, (int, float)):
        raise ValueError(f"非法熵区间上界: {high!r}")
    return EntropyRange(
        low=None if low is None else float(low),
        high=None if high is None else float(high),
    )


@dataclass(frozen=True)
class Event:
    """单条采集事件。

    :param seq: 采集端单调序号，用于稳定排序。
    :param ts: 事件时间（秒，数值即可）。
    :param etype: 事件类型。
    :param file_id: 稳定文件标识；进程/水位线事件可为 None。
    :param path: 事件发生时该路径（rename/hardlink 为旧路径）。
    :param new_path: rename/hardlink 的目标路径。
    :param pid: 进程标识。
    :param ppid: spawn 事件的父进程标识。
    :param gen: 进程世代（采集端给出的世代号），用于核验派生关系。
    :param pre_category / post_category: 写前/写后（或关闭时）内容类别。
    :param entropy: 写后/关闭时熵区间。
    :param pre_entropy: 写前熵区间（可选，便于审计核对）。
    :param new_file: 写入目标是否为本进程新建文件（新建归档判定用）。
    :param archived_source: 归档事件保留的源文档 file_id（可选）。
    """

    seq: int
    ts: float
    etype: EventType
    file_id: Optional[str] = None
    path: Optional[str] = None
    new_path: Optional[str] = None
    pid: Optional[str] = None
    ppid: Optional[str] = None
    gen: Optional[int] = None
    pre_category: Optional[str] = None
    post_category: Optional[str] = None
    entropy: Optional[EntropyRange] = None
    pre_entropy: Optional[EntropyRange] = None
    new_file: bool = False
    archived_source: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典（样本回放/审计用）。"""
        data: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "etype": self.etype.value,
        }
        if self.file_id is not None:
            data["file_id"] = self.file_id
        if self.path is not None:
            data["path"] = self.path
        if self.new_path is not None:
            data["new_path"] = self.new_path
        if self.pid is not None:
            data["pid"] = self.pid
        if self.ppid is not None:
            data["ppid"] = self.ppid
        if self.gen is not None:
            data["gen"] = self.gen
        if self.pre_category is not None:
            data["pre_category"] = self.pre_category
        if self.post_category is not None:
            data["post_category"] = self.post_category
        if self.entropy is not None:
            data["entropy"] = self.entropy.to_dict()
        if self.pre_entropy is not None:
            data["pre_entropy"] = self.pre_entropy.to_dict()
        if self.new_file:
            data["new_file"] = True
        if self.archived_source is not None:
            data["archived_source"] = self.archived_source
        return data


_REQUIRED = ("seq", "ts", "etype")
_STR_FIELDS = (
    "file_id",
    "path",
    "new_path",
    "pid",
    "ppid",
    "pre_category",
    "post_category",
    "archived_source",
)


def event_from_dict(raw: Mapping[str, Any]) -> Event:
    """把一条字典记录转换为 :class:`Event`，缺字段/非法类型直接报错。"""
    if not isinstance(raw, Mapping):
        raise ValueError(f"事件必须是对象: {raw!r}")
    for key in _REQUIRED:
        if key not in raw:
            raise ValueError(f"事件缺少必填字段 {key!r}: {raw!r}")
    etype = raw["etype"]
    if not isinstance(etype, str):
        raise ValueError(f"非法事件类型: {etype!r}")
    try:
        etype_enum = EventType(etype)
    except ValueError as exc:
        raise ValueError(f"未知事件类型: {etype!r}") from exc

    kwargs: dict[str, Any] = {"etype": etype_enum}
    kwargs["seq"] = int(raw["seq"])
    kwargs["ts"] = float(raw["ts"])

    for key in _STR_FIELDS:
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"字段 {key!r} 必须是字符串: {value!r}")
        kwargs[key] = value

    gen = raw.get("gen")
    if gen is not None:
        kwargs["gen"] = int(gen)
    kwargs["new_file"] = bool(raw.get("new_file", False))
    kwargs["entropy"] = _entropy(raw.get("entropy"))
    kwargs["pre_entropy"] = _entropy(raw.get("pre_entropy"))
    return Event(**kwargs)


def events_from_dicts(items: Iterable[Mapping[str, Any]]) -> list[Event]:
    return [event_from_dict(item) for item in items]


def load_scenario(path: str) -> tuple[dict[str, Any], list[Event]]:
    """加载 JSON 场景文件，返回 ``(policy 字典, 事件列表)``。

    场景格式：``{"policy": {...}, "events": [...], "description": "..."}``。
    """
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
    if not isinstance(doc, Mapping) or "events" not in doc:
        raise ValueError(f"场景文件缺少 events: {path}")
    policy = dict(doc.get("policy") or {})
    events = events_from_dicts(doc["events"])
    return policy, events
