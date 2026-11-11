"""任务编排层测试：去重-抽样衔接、预算边界声明、来源证据。"""
from __future__ import annotations

from collector.reservoir import ReservoirSampler
from collector.runner import run_sampling_task


def test_duplicates_do_not_increase_selection_chance(demo_url, datasets):
    """验收核心：同一 ID 的重复出现不得增加入样机会。

    独立对照实验（完全在测试侧构造，不读取采集器内部）：
      A. 真实目录（含跨页/页内重复）跑 T 次不同种子；
      B. 用真值中的唯一 ID 流（无任何重复）以同样种子跑 T 次；
    两组每个 ID 的入样频率必须一致——若重复会增加机会，A/B 必然出现系统差异。
    """
    golden = datasets["demo"]["golden"]
    unique_stream = golden["unique_ids_in_order"]
    trials = 300
    base_seed = 30000

    counts_real: dict[str, int] = {sid: 0 for sid in unique_stream}
    counts_clean: dict[str, int] = {sid: 0 for sid in unique_stream}

    for t in range(trials):
        seed = base_seed + t
        result = run_sampling_task(
            f"real-{t}", demo_url, seed=seed, sample_size=10, request_budget=100)
        assert result.status == "ok"
        for sid in result.sampling["sample"]:
            counts_real[sid] += 1

        sampler = ReservoirSampler(seed=seed, sample_size=10)
        for sid in unique_stream:
            sampler.offer(sid)
        for sid in sampler.finish().sample:
            counts_clean[sid] += 1

    # 逐 ID 频率完全相同（同种子、同去重后序列，随机数消耗序列一致）
    assert counts_real == counts_clean
    # 边际频率应接近理论 10/N（±0.08 宽松界，防止偶发抖动）
    n = len(unique_stream)
    for sid, c in counts_real.items():
        assert abs(c / trials - 10 / n) < 0.08, sid


def test_budget_exhausted_declares_observed_only(demo_url):
    result = run_sampling_task(
        "budget-1", demo_url, seed=20261111, sample_size=10, request_budget=4)
    assert result.status == "stopped"
    assert result.stop_reason == "budget_exhausted"
    assert "仅" in result.declaration and "已观察" in result.declaration
    assert "全站" in result.declaration  # 必须显式否认全站无偏
    assert result.scan["stop"]["next_url_not_fetched"] is not None
    # 样本只能来自已观察页面
    observed = {r["specimen_id"] for p in result.scan["pages"] for r in p["records"]}
    assert set(result.sampling["sample"]) <= observed
    # 样本量不超过已观察唯一数
    assert len(result.sampling["sample"]) <= result.observed_unique_count


def test_sample_size_greater_than_population(demo_url):
    result = run_sampling_task(
        "big-k", demo_url, seed=42, sample_size=500, request_budget=100)
    assert result.status == "ok"
    assert result.sampling["full_population_taken"] is True
    assert len(result.sampling["sample"]) == result.observed_unique_count
    assert all(e["action"] == "filled" for e in result.sampling["events"])
    assert result.observed_unique_count < 500


def test_sample_sources_point_to_real_pages(demo_url):
    result = run_sampling_task(
        "src-1", demo_url, seed=11, sample_size=7, request_budget=100)
    assert len(result.sample_sources) == len(result.sampling["sample"])
    for src in result.sample_sources:
        assert src["source_page"] is not None and src["source_page_url"].startswith("http")
        assert src["detail_url"] and src["detail_url"].startswith("http")
    # 来源页号与逐页证据自洽
    page_urls = {p["page_index"]: p["url"] for p in result.scan["pages"]}
    for src in result.sample_sources:
        assert page_urls[src["source_page"]] == src["source_page_url"]


def test_reproducible_end_to_end(demo_url):
    r1 = run_sampling_task("a", demo_url, seed=999, sample_size=12, request_budget=100)
    r2 = run_sampling_task("b", demo_url, seed=999, sample_size=12, request_budget=100)
    assert r1.sampling["sample"] == r2.sampling["sample"]
    assert r1.sampling["events"] == r2.sampling["events"]


def test_large_dataset_full_and_large_budget(large_url, datasets):
    golden = datasets["large"]["golden"]
    full = run_sampling_task("large-ok", large_url, seed=777, sample_size=25, request_budget=60)
    assert full.status == "ok"
    assert full.observed_unique_count == golden["unique_count"]
    assert full.duplicate_count == golden["raw_entry_count"] - golden["unique_count"]
    assert len(full.sampling["sample"]) == 25

    bounded = run_sampling_task("large-budget", large_url, seed=777, sample_size=25, request_budget=7)
    assert bounded.status == "stopped"
    assert len(bounded.scan["pages"]) == 7
    assert bounded.observed_unique_count < full.observed_unique_count


def test_request_error_is_reported_not_faked(demo_url, monkeypatch):
    import requests

    def boom(self, url, *args, **kwargs):
        raise requests.ConnectionError("模拟断网")
    monkeypatch.setattr(requests.Session, "get", boom)
    result = run_sampling_task(
        "err-1", demo_url, seed=1, sample_size=5, request_budget=10, max_retries=0)
    assert result.status == "failed"
    assert result.stop_reason == "request_error"
    assert "模拟断网" in result.error
    assert result.sampling is None  # 失败时不伪造抽样结果
