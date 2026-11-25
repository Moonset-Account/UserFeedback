"""可重放的服务网格端点选择器。

选择规则（题面）：

1. 从健康端点中按权重**无放回**抽取两个不同端点（两轮加权抽样，第二轮
   重新归一化剩余权重）；
2. 比较二者 ``在途数 / 权重``，选择负载较轻者；负载完全相等时按端点编号
   升序决定；
3. 健康池只剩一个端点时直接选择，**不消耗随机字节**；
4. 健康池为空时抛出 :class:`NoEndpointAvailableError`，同样不消耗随机字节。

随机整数到区间的映射采用拒绝采样，避免取模偏置：当区间长度不整除
2**64 时，落在最后一个不完整区间的随机数会被拒绝并重抽，保证每个端点
被抽中的概率严格正比于其权重。

端点离池（``remove_endpoint``）后，已发放的租约仍可正常归还；同名端点
重新加入时视为全新实例，在途数从 0 开始（内部以 generation 代际区分）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Hashable

from .errors import (
    DuplicateLeaseError,
    InvalidLeaseError,
    InvalidWeightError,
    NoEndpointAvailableError,
    RandomnessExhaustedError,
    UnknownEndpointError,
)
from .random_source import RandomByteStream

_UINT64_MOD = 1 << 64
_SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class Lease:
    """一次请求的租约，归还（``end_request``）时原样传回。

    - ``endpoint``：承接请求的端点编号；
    - ``generation``：端点实例代际，同名端点离池再入池后代际递增；
    - ``request_id``：选择器内部分配的租约序号。
    """

    endpoint: Hashable
    generation: int
    request_id: int

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "generation": self.generation,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, state: dict) -> "Lease":
        return cls(
            endpoint=state["endpoint"],
            generation=int(state["generation"]),
            request_id=int(state["request_id"]),
        )


class EndpointSelector:
    """接收权重/健康/请求事件与外部随机字节流的可重放选择器。"""

    def __init__(self, random_stream: RandomByteStream) -> None:
        if not isinstance(random_stream, RandomByteStream):
            raise TypeError(
                "random_stream 必须是 RandomByteStream 实例，"
                f"得到 {type(random_stream).__name__}"
            )
        self._stream = random_stream
        # 端点编号 -> {"weight": int, "healthy": bool, "generation": int,
        #              "in_flight": int}
        self._endpoints: dict[Hashable, dict] = {}
        # 尚未归还的租约 -> 发放时的代际
        self._active_leases: dict[Lease, int] = {}
        # 已归还或已离池代际的历史记录（key -> 每代的在途快照），用于内省
        self._retired: dict[Hashable, list[dict]] = {}
        self._next_request_id = 0
        self._next_generation = 1

    # ------------------------------------------------------------------
    # 权重与健康事件
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_weight(weight: Any) -> int:
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise InvalidWeightError(f"权重必须为正整数，得到 {weight!r}")
        if weight <= 0:
            raise InvalidWeightError(f"权重必须为正整数，得到 {weight}")
        return weight

    def upsert_endpoint(self, endpoint: Hashable, weight: int, *, healthy: bool = True) -> None:
        """加入新端点或更新在池端点的权重。

        新端点（含离池后同名再加入）在途数从 0 开始，代际递增。
        对仍在池中的端点更新权重不会重置在途数与代际。
        """
        weight = self._validate_weight(weight)
        record = self._endpoints.get(endpoint)
        if record is None:
            generation = self._next_generation
            self._next_generation += 1
            self._endpoints[endpoint] = {
                "weight": weight,
                "healthy": bool(healthy),
                "generation": generation,
                "in_flight": 0,
            }
            self._retired.setdefault(endpoint, [])
        else:
            record["weight"] = weight
            record["healthy"] = bool(healthy)

    def set_weight(self, endpoint: Hashable, weight: int) -> None:
        """更新在池端点的权重。"""
        weight = self._validate_weight(weight)
        record = self._endpoints.get(endpoint)
        if record is None:
            raise UnknownEndpointError(f"端点 {endpoint!r} 不在池中")
        record["weight"] = weight

    def set_health(self, endpoint: Hashable, healthy: bool) -> None:
        """更新在池端点的健康状态。"""
        record = self._endpoints.get(endpoint)
        if record is None:
            raise UnknownEndpointError(f"端点 {endpoint!r} 不在池中")
        record["healthy"] = bool(healthy)

    def remove_endpoint(self, endpoint: Hashable) -> None:
        """端点离池。

        离池后不再参与选择，但该实例已发放的租约仍允许正常归还；
        同名端点之后通过 :meth:`upsert_endpoint` 重新加入时按新实例处理，
        在途数从 0 开始。
        """
        record = self._endpoints.pop(endpoint, None)
        if record is None:
            raise UnknownEndpointError(f"端点 {endpoint!r} 不在池中")
        self._retired.setdefault(endpoint, []).append(
            {
                "generation": record["generation"],
                "weight": record["weight"],
                "in_flight_at_removal": record["in_flight"],
            }
        )

    # ------------------------------------------------------------------
    # 请求开始 / 结束事件
    # ------------------------------------------------------------------
    def start_request(self) -> Lease:
        """选择一个端点承接新请求并返回租约；租约在途数加一。"""
        candidates = [
            (name, record)
            for name, record in self._endpoints.items()
            if record["healthy"]
        ]
        if not candidates:
            raise NoEndpointAvailableError("没有健康端点可承接请求")

        if len(candidates) == 1:
            # 仅剩一端：直接选择，不触碰随机流。
            chosen_name, chosen_record = candidates[0]
        else:
            first = self._weighted_draw(candidates)
            remaining = [item for item in candidates if item[0] != first[0]]
            second = self._weighted_draw(remaining)
            chosen_name, chosen_record = self._lower_load(first, second)

        lease = Lease(
            endpoint=chosen_name,
            generation=chosen_record["generation"],
            request_id=self._next_request_id,
        )
        self._next_request_id += 1
        chosen_record["in_flight"] += 1
        self._active_leases[lease] = chosen_record["generation"]
        return lease

    # 语义别名：题面所述的“开始/结束请求事件”。
    acquire = start_request

    def end_request(self, lease: Lease) -> None:
        """归还请求。

        离池端点的旧租约仍可归还（在途数计入其历史代际）；未知租约或
        重复归还会抛出异常。
        """
        if not isinstance(lease, Lease):
            raise InvalidLeaseError(f"租约必须是 Lease 实例，得到 {type(lease).__name__}")
        generation = self._active_leases.pop(lease, None)
        if generation is None:
            raise DuplicateLeaseError(f"租约 {lease} 不存在或已归还")

        record = self._endpoints.get(lease.endpoint)
        if record is not None and record["generation"] == generation:
            record["in_flight"] -= 1
            return
        # 端点已离池（或同名新实例已入池）：在历史代际上归还。
        for entry in self._retired.get(lease.endpoint, ()):  # pragma: no branch
            if entry["generation"] == generation:
                entry["in_flight_at_removal"] -= 1
                return
        # 理论不可达：活动租约的代际必然来自在池或已离池记录。
        raise InvalidLeaseError(f"租约 {lease} 的代际 {generation} 无对应实例记录")

    release = end_request

    # ------------------------------------------------------------------
    # 抽样与比较
    # ------------------------------------------------------------------
    def _read_unbiased_index(self, n: int) -> int:
        """把 64 位无符号随机整数无偏地映射到 [0, n)。

        使用拒绝采样：若区间长度 n 不整除 2**64，则拒绝落在最后一个
        不完整区间内的随机数并重抽，杜绝取模偏置。
        """
        limit = _UINT64_MOD - (_UINT64_MOD % n)  # 可接受随机数的上界
        while True:
            value = self._stream.read_uint64()
            if value < limit:
                return value % n
            # value 落在尾部不完整区间：拒绝并重抽（再消耗一个随机数）。

    def _weighted_draw(self, candidates: list[tuple[Hashable, dict]]) -> tuple[Hashable, dict]:
        """在候选端点中按权重抽取一个（不放回由调用方保证）。

        每次调用都恰好消耗一个（拒绝采样时可能是多个）64 位随机数：
        把随机整数无偏映射到总权重区间 [0, total_weight)，再按累计权重
        落盘。即使只剩一个候选也照常消耗随机数，保证两轮无放回抽样的
        随机流消耗位置固定、可重放。候选顺序固定（字典插入序），端点
        编号只用于负载平局，不影响加权抽样的概率。
        """
        total_weight = sum(record["weight"] for _, record in candidates)
        threshold = self._read_unbiased_index(total_weight)
        cumulative = 0
        for name, record in candidates:
            cumulative += record["weight"]
            if threshold < cumulative:
                return name, record
        # 纯整数实现；threshold < total_weight 必然在循环内返回。
        return candidates[-1]

    @staticmethod
    def _lower_load(
        first: tuple[Hashable, dict], second: tuple[Hashable, dict]
    ) -> tuple[Hashable, dict]:
        """比较 在途数/权重，分数低者胜；平局按端点编号升序。"""
        name_a, rec_a = first
        name_b, rec_b = second
        # 交叉相乘避免浮点：in_flight_a / weight_a < in_flight_b / weight_b
        left = rec_a["in_flight"] * rec_b["weight"]
        right = rec_b["in_flight"] * rec_a["weight"]
        if left != right:
            return first if left < right else second
        return first if name_a <= name_b else second

    # ------------------------------------------------------------------
    # 内省
    # ------------------------------------------------------------------
    def in_flight_of(self, endpoint: Hashable) -> int:
        """返回在池端点的当前在途数。"""
        record = self._endpoints.get(endpoint)
        if record is None:
            raise UnknownEndpointError(f"端点 {endpoint!r} 不在池中")
        return record["in_flight"]

    @property
    def healthy_endpoints(self) -> list[Hashable]:
        """当前健康端点编号（按插入顺序）。"""
        return [name for name, record in self._endpoints.items() if record["healthy"]]

    @property
    def random_stream(self) -> RandomByteStream:
        return self._stream

    def describe(self, endpoint: Hashable) -> dict:
        """返回端点的当前状态快照（只读用途）。"""
        record = self._endpoints.get(endpoint)
        if record is None:
            raise UnknownEndpointError(f"端点 {endpoint!r} 不在池中")
        return dict(record)

    # ------------------------------------------------------------------
    # 可保存状态与重放
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        """返回可 JSON 序列化的完整状态快照。"""
        return {
            "version": _SNAPSHOT_VERSION,
            "endpoints": [
                (
                    name,
                    {
                        "weight": record["weight"],
                        "healthy": record["healthy"],
                        "generation": record["generation"],
                        "in_flight": record["in_flight"],
                    },
                )
                for name, record in self._endpoints.items()
            ],
            "retired": [
                (name, [dict(entry) for entry in entries])
                for name, entries in self._retired.items()
            ],
            "active_leases": [lease.to_dict() for lease in self._active_leases],
            "next_request_id": self._next_request_id,
            "next_generation": self._next_generation,
            "random_stream": self._stream.snapshot(),
        }

    @classmethod
    def restore(cls, state: dict, random_stream: RandomByteStream) -> "EndpointSelector":
        """从快照与同一随机字节流恢复选择器。

        随机流会被定位到快照记录的消耗偏移，因此后续事件重放时连随机
        字节的消耗位置都与原实例完全一致。
        """
        version = state.get("version")
        if version != _SNAPSHOT_VERSION:
            raise ValueError(
                f"不支持的快照版本 {version!r}，当前版本 {_SNAPSHOT_VERSION}"
            )
        selector = cls(random_stream)
        selector._endpoints = {
            name: {
                "weight": int(record["weight"]),
                "healthy": bool(record["healthy"]),
                "generation": int(record["generation"]),
                "in_flight": int(record["in_flight"]),
            }
            for name, record in state["endpoints"]
        }
        selector._retired = {
            name: [
                {
                    "generation": int(entry["generation"]),
                    "weight": int(entry["weight"]),
                    "in_flight_at_removal": int(entry["in_flight_at_removal"]),
                }
                for entry in entries
            ]
            for name, entries in state["retired"]
        }
        selector._active_leases = {
            Lease.from_dict(lease_state): int(lease_state["generation"])
            for lease_state in state["active_leases"]
        }
        selector._next_request_id = int(state["next_request_id"])
        selector._next_generation = int(state["next_generation"])
        selector._stream.restore(state["random_stream"])
        return selector
