"""小总体多种子频率检验（蓄水池抽样均匀性的实证检查）。

方法：对 M 个 ID 的固定顺序流，用不同随机种子重复 T 次蓄水池抽样（样本量 k），
统计每个 ID 的入样频率，并与理论概率 p=k/M 做 Pearson 卡方检验。

重要边界（README 与网页均须展示）：
* 该检验只能证明"抽样器在已知有限流上的边际入样概率近似均匀"；
* 它**不构成全站覆盖证明**——未被扫描到的条目根本不在流中，频率检验无法发现；
* 预算耗尽场景下，均匀性结论只对"已观察集合"成立。
"""
from __future__ import annotations

from collector.reservoir import ReservoirSampler

# df = M-1=24 时卡方分布的近似临界值（标准库无统计函数，内置常用值）
_CHI2_CRITICAL = {0.05: 36.415, 0.01: 42.980, 0.001: 52.618}


def run_frequency_check(population_size: int = 25, sample_size: int = 5,
                        trials: int = 2000, base_seed: int = 20261111) -> dict:
    if not (1 <= sample_size < population_size <= 200):
        raise ValueError("需满足 1 <= sample_size < population_size <= 200")
    if not (100 <= trials <= 20000):
        raise ValueError("trials 需在 100~20000 之间")

    population = [f"SPM-{i:04d}" for i in range(1, population_size + 1)]
    counts = {sid: 0 for sid in population}
    # 同时记录若干固定种子的最终样本，供"同种子可复现"人工核对
    pinned: dict[int, list[str]] = {}
    pin_seeds = [base_seed, base_seed + 1, base_seed + 2]

    for t in range(trials):
        seed = base_seed + t
        sampler = ReservoirSampler(seed=seed, sample_size=sample_size)
        for sid in population:
            sampler.offer(sid)
        result = sampler.finish()
        for chosen in result.sample:
            counts[chosen] += 1
        if seed in pin_seeds:
            pinned[seed] = list(result.sample)

    p = sample_size / population_size
    expected = trials * p
    chi2 = sum((c - expected) ** 2 / expected for c in counts.values())
    df = population_size - 1
    critical_05 = _CHI2_CRITICAL[0.05] if df == 24 else None
    passed = chi2 < (_CHI2_CRITICAL[0.05] if df == 24 else df * 1.6)  # 自由度不同时的宽松兜底
    freqs = [
        {"specimen_id": sid, "count": counts[sid],
         "frequency": counts[sid] / trials, "expected_probability": p}
        for sid in population
    ]
    return {
        "params": {"population_size": population_size, "sample_size": sample_size,
                   "trials": trials, "base_seed": base_seed, "df": df},
        "theoretical_probability": p,
        "expected_count_each": expected,
        "chi_square": chi2,
        "chi2_critical_0_05_df24": critical_05,
        "passed_at_0_05": passed,
        "frequencies": freqs,
        "pinned_runs": [{"seed": s, "sample": pinned[s]} for s in pin_seeds],
        "disclaimer": (
            "频率检验仅验证抽样器对已知有限 ID 流的边际入样概率近似均匀，"
            "不构成全站覆盖证明；未扫描到的条目不进入 ID 流，检验无法发现其缺失。"
            "预算耗尽时，任何均匀性结论仅适用于已观察集合。"
        ),
    }
