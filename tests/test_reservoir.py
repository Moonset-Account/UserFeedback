"""蓄水池抽样核心测试。

所有期望值均由本文件内的独立参考实现/手工推导给出，不引用被测代码内部常量。
"""
from __future__ import annotations

import random

import pytest

from collector.reservoir import ReservoirSampler


def reference_algorithm_r(stream, k, seed):
    """独立参考实现：按 Vitter Algorithm R 教科书定义直接书写。

    注意教科书形式在每个 i>k 的步骤上都先抽一个均匀随机数再比较，
    与被测实现逐次消耗 RNG 的方式一致。
    """
    rng = random.Random(seed)
    reservoir = []
    for i, item in enumerate(stream, start=1):
        if len(reservoir) < k:
            rng.random()  # 填充步骤同样消耗一次随机数，保持序列对齐
            reservoir.append(item)
        elif rng.random() < k / i:
            reservoir[rng.randrange(k)] = item
    return reservoir


def test_reproducible_same_seed_same_order():
    stream = [f"ID-{i:03d}" for i in range(1, 51)]
    r1 = ReservoirSampler(seed=12345, sample_size=8)
    r2 = ReservoirSampler(seed=12345, sample_size=8)
    for sid in stream:
        r1.offer(sid)
    for sid in stream:
        r2.offer(sid)
    a, b = r1.finish(), r2.finish()
    assert a.sample == b.sample
    assert [(e.step, e.specimen_id, e.action, e.draw, e.slot, e.ejected_id) for e in a.events] == \
           [(e.step, e.specimen_id, e.action, e.draw, e.slot, e.ejected_id) for e in b.events]
    # 与独立参考实现逐元素一致（相同 RNG 序列下必须得到同一蓄水池）
    assert a.sample == reference_algorithm_r(stream, 8, 12345)


def test_different_seed_or_order_changes_result_reasonably():
    stream = [f"ID-{i:03d}" for i in range(1, 81)]
    out = set()
    for seed in (1, 2, 3, 4, 5):
        s = ReservoirSampler(seed=seed, sample_size=10)
        for sid in stream:
            s.offer(sid)
        out.add(tuple(s.finish().sample))
    assert len(out) >= 4  # 不同种子几乎必然产生不同样本（5 组至少 4 组不同）

    # 相同种子、改变输入顺序 => 结果允许且应当反映顺序差异
    s_rev = ReservoirSampler(seed=1, sample_size=10)
    for sid in reversed(stream):
        s_rev.offer(sid)
    s_fwd = ReservoirSampler(seed=1, sample_size=10)
    for sid in stream:
        s_fwd.offer(sid)
    # 流内容相同但顺序相反：样本集合可能偶合相同，事件序列必然不同
    assert [(e.step, e.specimen_id) for e in s_rev.finish().events] != \
           [(e.step, e.specimen_id) for e in s_fwd.finish().events]


def test_sample_size_exceeds_population_takes_all_without_replacement():
    stream = [f"ID-{i}" for i in range(5)]
    s = ReservoirSampler(seed=7, sample_size=100)
    for sid in stream:
        s.offer(sid)
    result = s.finish()
    assert result.sample == stream
    assert result.observed_unique == 5
    assert result.full_population_taken is True
    assert all(e.action == "filled" for e in result.events)
    assert not any(e.action == "replaced" for e in result.events)
    assert len(result.sample) == 5  # 不能伪造出不存在的样本


def test_zero_sample_size_and_empty_stream():
    s = ReservoirSampler(seed=1, sample_size=0)
    for sid in ("A", "B"):
        assert s.offer(sid) is None
    result = s.finish()
    assert result.sample == [] and result.observed_unique == 2
    assert result.full_population_taken is False

    empty = ReservoirSampler(seed=1, sample_size=5).finish()
    assert empty.sample == [] and empty.full_population_taken is False


def test_replacement_probability_matches_k_over_i():
    # 构造固定状态：k=3，已观察 10 个 ID；第 11 个 ID 的替换概率应为 3/11。
    # 用独立 RNG 大量重复该决策，频率应接近 3/11（±3 个标准误，约 0.024@10000）。
    trials = 10000
    replaced = 0
    for t in range(trials):
        s = ReservoirSampler(seed=900 + t, sample_size=3)
        for i in range(1, 11):
            s.offer(f"ID-{i:02d}")
        event = s.offer("ID-NEW")
        if event.action == "replaced":
            replaced += 1
    expected = 3 / 11
    se = (expected * (1 - expected) / trials) ** 0.5
    assert abs(replaced / trials - expected) < 3 * se


def test_events_are_complete_and_reservoir_invariants_hold():
    stream = [f"ID-{i:03d}" for i in range(1, 41)]
    s = ReservoirSampler(seed=55, sample_size=6)
    for sid in stream:
        s.offer(sid)
    result = s.finish()
    # 每个唯一 ID 恰好一个事件；蓄水池始终不超过 k 且元素唯一
    assert len(result.events) == len(stream)
    for e in result.events:
        assert len(e.reservoir_after) <= 6
        assert len(set(e.reservoir_after)) == len(e.reservoir_after)
        if e.action == "replaced":
            assert e.ejected_id in e.reservoir_before
            assert e.ejected_id not in e.reservoir_after
            assert e.specimen_id in e.reservoir_after
    assert set(result.sample).issubset(set(stream))


def test_invalid_arguments():
    with pytest.raises(ValueError):
        ReservoirSampler(seed=1, sample_size=-1)
    with pytest.raises(TypeError):
        ReservoirSampler(seed=1.5, sample_size=3)  # type: ignore[arg-type]
