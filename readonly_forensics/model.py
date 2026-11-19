"""取证输入模型与输入校验。

所有数据类均为 ``frozen``，模块只读消费这些结构，绝不写回证据。
时间戳为可比较的标量（整数/浮点等），数值越大越晚。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

# ---- 区段状态 / 拒绝原因码 -------------------------------------------------

RECOVERED = "recovered"          # 有连续归属依据且采集成功，输出原字节
REALLOCATED = "reallocated"      # 窗内曾分配给其他对象，归属断裂
RECORD_GAP = "record_gap"        # 窗内存在记录缺口，归属不可追溯
OVERWRITTEN = "overwritten"      # 窗内被改写，现存字节不是原字节
ACQUISITION_MISSING = "acquisition_missing"  # 无对应采集分块
ACQUISITION_ERROR = "acquisition_error"      # 采集分块标记为错误
ACQUISITION_SHORT = "acquisition_short"      # 短读，可用字节不足
MAPPING_MISSING = "mapping_missing"          # 逻辑偏移无物理簇映射

_CHUNK_STATUSES = frozenset({"ok", "missing", "error"})


def _nonneg_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数，收到: {value!r}")
    return value


def _check_range(start: int, end: int, limit: int, what: str) -> None:
    if not (0 <= start < end <= limit):
        raise ValueError(
            f"{what} 区间非法: [{start}, {end})，须满足 0 <= start < end <= {limit}"
        )


@dataclass(frozen=True)
class Allocation:
    """物理簇在 [start_time, end_time) 内被分配给其他对象的记录。

    ``end_time=None`` 表示到采集时仍处于被占用状态。
    """

    physical_cluster: int
    start_time: int
    end_time: int | None
    object_id: str
    event_id: str

    def __post_init__(self) -> None:
        _nonneg_int(self.physical_cluster, "physical_cluster")
        if isinstance(self.start_time, bool) or not isinstance(
            self.start_time, (int, float)
        ):
            raise ValueError("start_time 必须是数值时间戳")
        if self.end_time is not None:
            if isinstance(self.end_time, bool) or not isinstance(
                self.end_time, (int, float)
            ):
                raise ValueError("end_time 必须是数值时间戳或 None")
            if self.end_time <= self.start_time:
                raise ValueError("Allocation 的 end_time 必须晚于 start_time")
        if not self.object_id:
            raise ValueError("object_id 不能为空")
        if not self.event_id:
            raise ValueError("event_id 不能为空")


@dataclass(frozen=True)
class Overwrite:
    """物理簇内 [phy_start, phy_end) 字节在某时刻被改写的记录。"""

    physical_cluster: int
    phy_start: int
    phy_end: int
    time: int
    event_id: str

    def __post_init__(self) -> None:
        _nonneg_int(self.physical_cluster, "physical_cluster")
        _nonneg_int(self.phy_start, "phy_start")
        _nonneg_int(self.phy_end, "phy_end")
        if self.phy_end <= self.phy_start:
            raise ValueError("Overwrite 区间必须非空且 start < end")
        if isinstance(self.time, bool) or not isinstance(self.time, (int, float)):
            raise ValueError("time 必须是数值时间戳")
        if not self.event_id:
            raise ValueError("event_id 不能为空")


@dataclass(frozen=True)
class RecordGapEvent:
    """物理簇在 [start_time, end_time) 内分配/释放记录缺失。

    缺口窗内无法证明字节连续归属原文件，相关字节必须拒绝输出。
    ``end_time=None`` 表示缺口一直延续到采集时刻。
    """

    physical_cluster: int
    start_time: int
    end_time: int | None
    event_id: str

    def __post_init__(self) -> None:
        _nonneg_int(self.physical_cluster, "physical_cluster")
        if isinstance(self.start_time, bool) or not isinstance(
            self.start_time, (int, float)
        ):
            raise ValueError("start_time 必须是数值时间戳")
        if self.end_time is not None:
            if isinstance(self.end_time, bool) or not isinstance(
                self.end_time, (int, float)
            ):
                raise ValueError("end_time 必须是数值时间戳或 None")
            if self.end_time <= self.start_time:
                raise ValueError("RecordGapEvent 的 end_time 必须晚于 start_time")
        if not self.event_id:
            raise ValueError("event_id 不能为空")


@dataclass(frozen=True)
class Chunk:
    """采集时某物理簇的分块结果。

    status:
      - ``"ok"``：data 为实际采集到的字节；
      - ``"missing"``：无该分块；
      - ``"error"``：采集报错，error 为错误说明。
    """

    physical_cluster: int
    status: str
    data: bytes | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _nonneg_int(self.physical_cluster, "physical_cluster")
        if self.status not in _CHUNK_STATUSES:
            raise ValueError(
                f"chunk status 非法: {self.status!r}，允许 {sorted(_CHUNK_STATUSES)}"
            )
        if self.status == "ok":
            if not isinstance(self.data, (bytes, bytearray)):
                raise ValueError("status=ok 的分块必须提供 bytes 类型 data")
            if self.error is not None:
                raise ValueError("status=ok 的分块不能携带 error")
        else:
            if self.data is not None:
                raise ValueError(f"status={self.status} 的分块不能携带 data")
        if self.status == "error" and not self.error:
            raise ValueError("status=error 的分块必须提供 error 说明")


@dataclass(frozen=True)
class RecoveryCase:
    """一次删除文件恢复判定的完整输入。"""

    file_size: int
    cluster_size: int
    deletion_time: int
    acquisition_time: int
    cluster_chain: tuple[int, ...]
    chunks: Mapping[int, Chunk]
    allocations: tuple[Allocation, ...] = ()
    overwrites: tuple[Overwrite, ...] = ()
    record_gaps: tuple[RecordGapEvent, ...] = ()
    file_name: str | None = None

    def __post_init__(self) -> None:
        _nonneg_int(self.file_size, "file_size")
        _nonneg_int(self.cluster_size, "cluster_size")
        if self.cluster_size == 0:
            raise ValueError("cluster_size 必须为正整数")
        if isinstance(self.deletion_time, bool) or not isinstance(
            self.deletion_time, (int, float)
        ):
            raise ValueError("deletion_time 必须是数值时间戳")
        if isinstance(self.acquisition_time, bool) or not isinstance(
            self.acquisition_time, (int, float)
        ):
            raise ValueError("acquisition_time 必须是数值时间戳")
        if self.acquisition_time <= self.deletion_time:
            raise ValueError("acquisition_time 必须晚于 deletion_time")
        if not isinstance(self.cluster_chain, tuple):
            raise ValueError("cluster_chain 必须是元组")
        # 0 字节文件不占簇，允许空簇链；其余情况簇链必须非空。
        expected = (self.file_size + self.cluster_size - 1) // self.cluster_size
        if expected == 0 and self.cluster_chain:
            raise ValueError("file_size 为 0 时 cluster_chain 必须为空")
        if expected > 0 and not self.cluster_chain:
            raise ValueError("非空文件的 cluster_chain 不能为空元组")
        for pc in self.cluster_chain:
            _nonneg_int(pc, "cluster_chain 元素")
        if len(set(self.cluster_chain)) != len(self.cluster_chain):
            raise ValueError("cluster_chain 中的物理簇不能重复")
        # 簇链可以短于需求：链尾之外的逻辑簇按 mapping_missing 输出；
        # 但不允许长出文件实际占用的簇数。
        if len(self.cluster_chain) > expected:
            raise ValueError(
                f"cluster_chain 长度 {len(self.cluster_chain)} 超过"
                f" file_size/cluster_size 推算的 {expected} 个簇"
            )
        if not isinstance(self.chunks, Mapping):
            raise ValueError("chunks 必须是物理簇到 Chunk 的映射")
        for pc, chunk in self.chunks.items():
            _nonneg_int(pc, "chunks 键")
            if not isinstance(chunk, Chunk):
                raise ValueError("chunks 的值必须是 Chunk 实例")
            if chunk.physical_cluster != pc:
                raise ValueError(
                    f"chunks 键 {pc} 与 Chunk.physical_cluster "
                    f"{chunk.physical_cluster} 不一致"
                )
        for name, events in (
            ("allocations", self.allocations),
            ("overwrites", self.overwrites),
            ("record_gaps", self.record_gaps),
        ):
            if not isinstance(events, tuple):
                raise ValueError(f"{name} 必须是元组")

        # 冻结对外可见的映射（dataclass frozen 只冻结重绑定，不冻结内部对象）。
        object.__setattr__(self, "chunks", MappingProxyType(dict(self.chunks)))

        known = set(self.cluster_chain)
        for ev in self.allocations:
            if ev.physical_cluster not in known:
                raise ValueError(
                    f"Allocation {ev.event_id} 引用的物理簇 "
                    f"{ev.physical_cluster} 不在簇链中"
                )
        for ev in self.overwrites:
            if ev.physical_cluster not in known:
                raise ValueError(
                    f"Overwrite {ev.event_id} 引用的物理簇 "
                    f"{ev.physical_cluster} 不在簇链中"
                )
            _check_range(
                ev.phy_start, ev.phy_end, self.cluster_size, f"Overwrite {ev.event_id}"
            )
        for ev in self.record_gaps:
            if ev.physical_cluster not in known:
                raise ValueError(
                    f"RecordGapEvent {ev.event_id} 引用的物理簇 "
                    f"{ev.physical_cluster} 不在簇链中"
                )
        chunk_ids = [c.physical_cluster for c in self.chunks.values()]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("chunks 中存在重复物理簇条目")

    # ------------------------------------------------------------------ #
    # 字典 / JSON 友好的构造入口
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: Mapping) -> "RecoveryCase":
        if not isinstance(data, Mapping):
            raise ValueError("case 数据必须是映射对象")

        def need(key):
            if key not in data:
                raise ValueError(f"缺少必填字段: {key}")
            return data[key]

        raw_chain = need("cluster_chain")
        if isinstance(raw_chain, Mapping):
            try:
                chain = tuple(int(raw_chain[k]) for k in sorted(raw_chain, key=int))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"cluster_chain 映射无法解析: {exc}") from exc
        elif isinstance(raw_chain, (list, tuple)):
            chain = tuple(int(x) for x in raw_chain)
        else:
            raise ValueError("cluster_chain 必须是列表/元组或映射")

        chunks = {}
        for item in need("chunks"):
            if not isinstance(item, Mapping):
                raise ValueError("chunks 条目必须是映射对象")
            status = item["status"]
            raw_bytes = item.get("data")
            if status == "ok":
                if isinstance(raw_bytes, str):
                    payload = base64.b64decode(raw_bytes, validate=True)
                elif isinstance(raw_bytes, (bytes, bytearray)):
                    payload = bytes(raw_bytes)
                else:
                    raise ValueError("status=ok 的分块 data 必须是 base64 字符串或字节")
                chunk = Chunk(
                    physical_cluster=int(item["physical_cluster"]),
                    status="ok",
                    data=payload,
                )
            else:
                chunk = Chunk(
                    physical_cluster=int(item["physical_cluster"]),
                    status=status,
                    error=item.get("error"),
                )
            chunks[chunk.physical_cluster] = chunk

        allocations = tuple(
            Allocation(
                physical_cluster=int(item["physical_cluster"]),
                start_time=item["start_time"],
                end_time=item.get("end_time"),
                object_id=str(item["object_id"]),
                event_id=str(item["event_id"]),
            )
            for item in data.get("allocations", ())
        )
        overwrites = tuple(
            Overwrite(
                physical_cluster=int(item["physical_cluster"]),
                phy_start=int(item["phy_start"]),
                phy_end=int(item["phy_end"]),
                time=item["time"],
                event_id=str(item["event_id"]),
            )
            for item in data.get("overwrites", ())
        )
        gaps = tuple(
            RecordGapEvent(
                physical_cluster=int(item["physical_cluster"]),
                start_time=item["start_time"],
                end_time=item.get("end_time"),
                event_id=str(item["event_id"]),
            )
            for item in data.get("record_gaps", ())
        )

        return cls(
            file_size=int(need("file_size")),
            cluster_size=int(need("cluster_size")),
            deletion_time=need("deletion_time"),
            acquisition_time=need("acquisition_time"),
            cluster_chain=chain,
            chunks=chunks,
            allocations=allocations,
            overwrites=overwrites,
            record_gaps=gaps,
            file_name=data.get("file_name"),
        )
