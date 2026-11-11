"""可复现的蓄水池抽样（Reservoir Sampling, Algorithm R）。

适用场景：公开标本目录的**已完整扫描的唯一 ID 流**——总体规模预先未知，
条目可能跨页重复（去重由调用方负责，本模块只接收唯一 ID 流）。

性质（见 tests/test_reservoir.py 的独立断言）：
1. 每个被观察到的唯一 ID 入样概率相等（k/N，k=样本量，N=观察到的唯一 ID 数）；
2. 相同随机种子 + 相同输入顺序 => 逐事件完全可复现；
3. 同一 ID 若被去重后只出现一次，则重复条目不增加其入样机会（去重在 runner 完成）；
4. 当 N <= k 时全部入样，不产生替换。

随机源严格使用 random.Random(seed) 实例，不使用任何全局随机状态。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class ReplacementEvent:
    """一次入样/替换事件（供网页展示完整过程）。"""

    step: int                 # 该 ID 在唯一流中的序号（从 1 开始）
    specimen_id: str          # 新到达的唯一 ID
    draw: float               # 本次实际使用的随机数（独立 Random 实例，便于复现核对）
    action: str               # "filled"（填充蓄水池） | "replaced"（替换） | "skipped"（未入样）
    reservoir_before: list[str]
    reservoir_after: list[str]
    ejected_id: str | None = None  # 被挤出的 ID（仅 replaced）
    slot: int | None = None        # 被替换的蓄水池槽位（从 0 开始）


@dataclass
class SamplingResult:
    """一次抽样的最终结果与全部证据。"""

    seed: int
    requested_sample_size: int
    observed_unique: int
    sample: list[str]
    events: list[ReplacementEvent] = field(default_factory=list)
    full_population_taken: bool = False  # N <= k，总体被全量收录
    rng_name: str = "random.Random (MT19937, Algorithm R)"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["events"] = [asdict(e) for e in self.events]
        return d


class ReservoirSampler:
    """Algorithm R 蓄水池抽样器。

    用法::

        s = ReservoirSampler(seed=20261111, sample_size=10)
        for unique_id in stream_of_unique_ids:
            s.offer(unique_id)
        result = s.finish()
    """

    def __init__(self, seed: int, sample_size: int) -> None:
        if not isinstance(seed, int):
            raise TypeError("seed 必须为整数")
        if not isinstance(sample_size, int) or sample_size < 0:
            raise ValueError("sample_size 必须为非负整数")
        self._rng = random.Random(seed)
        self._seed = seed
        self._k = sample_size
        self._reservoir: list[str] = []
        self._step = 0
        self._events: list[ReplacementEvent] = []

    def offer(self, specimen_id: str) -> ReplacementEvent | None:
        """向抽样器提供一个**已去重**的 ID；返回本次事件（k=0 时返回 None）。"""
        self._step += 1
        step = self._step

        if self._k == 0:
            return None

        before = list(self._reservoir)
        draw = self._rng.random()

        if len(self._reservoir) < self._k:
            # 前 k 个唯一 ID 直接填满蓄水池
            self._reservoir.append(specimen_id)
            event = ReplacementEvent(
                step=step,
                specimen_id=specimen_id,
                draw=draw,
                action="filled",
                reservoir_before=before,
                reservoir_after=list(self._reservoir),
            )
        elif draw < self._k / step:
            # 以 k/step 的概率替换某个均匀随机槽位
            slot = self._rng.randrange(self._k)
            ejected = self._reservoir[slot]
            self._reservoir[slot] = specimen_id
            event = ReplacementEvent(
                step=step,
                specimen_id=specimen_id,
                draw=draw,
                action="replaced",
                reservoir_before=before,
                reservoir_after=list(self._reservoir),
                ejected_id=ejected,
                slot=slot,
            )
        else:
            event = ReplacementEvent(
                step=step,
                specimen_id=specimen_id,
                draw=draw,
                action="skipped",
                reservoir_before=before,
                reservoir_after=list(self._reservoir),
            )

        self._events.append(event)
        return event

    def finish(self) -> SamplingResult:
        """结束抽样并固化结果。可重复调用，结果不变。"""
        # 全量收录：观察到了至少一个唯一 ID，且其数量未超过样本量
        full = self._step > 0 and self._step <= self._k
        return SamplingResult(
            seed=self._seed,
            requested_sample_size=self._k,
            observed_unique=self._step,
            sample=list(self._reservoir),
            events=list(self._events),
            full_population_taken=full,
            rng_name="random.Random (MT19937, Algorithm R)",
        )

    @property
    def observed_unique(self) -> int:
        return self._step

    @property
    def reservoir(self) -> list[str]:
        return list(self._reservoir)
