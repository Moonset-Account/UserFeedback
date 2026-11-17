#!/usr/bin/env python3
"""本地核验入口（C01-C04）。

用法：
    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case all

规则：
* 每个用例执行真实断言；任一断言失败、入口缺失、未实现、零断言、
  整组跳过都会导致非零退出码；
* 输出 JSON，记录 case、实际断言数、passed/failed/skipped，以及
  样本/种子标识与期望/观察摘要；
* 不输出原始样本内容与任何凭据，只输出哈希、计数与状态枚举。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from typing import Any, Callable

# 隔离模式 (-I) 下仍保证可导入工作区内的 reducer 包
_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from reducer import (  # noqa: E402
    Reducer,
    Sample,
    StructuralValidator,
    TrialCache,
    apply_transform,
    dependency_closure,
    generate_transforms,
    safe_run,
)
from reducer.engine import dedupe_transforms  # noqa: E402
from reducer.stubs import (  # noqa: E402
    AlwaysTargetTester,
    DependencyTester,
    FlakyMatchTester,
    OtherErrorTester,
    TARGET_FINGERPRINT,
    TimeoutTester,
    build_demo_sample,
)
from reducer.tester import (  # noqa: E402
    STATUS_ERROR,
    STATUS_MATCH,
    STATUS_OTHER_FP,
    STATUS_TIMEOUT,
    all_matches,
)


class AssertRecorder:
    """最小真实断言记录器：assert_true 失败即计入 failed，不跳过。"""

    def __init__(self, case: str) -> None:
        self.case = case
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []
        self.skipped = 0
        self.observations: list[str] = []

    def check(self, condition: bool, label: str, expected: Any, observed: Any) -> None:
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(label)
        self.observations.append(
            {
                "assertion": label,
                "expected": _safe(expected),
                "observed": _safe(observed),
                "ok": bool(condition),
            }
        )

    def note(self, label: str, value: Any) -> None:
        self.observations.append({"note": label, "value": _safe(value)})

    def summary(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        result = {
            "case": self.case,
            "assertions": self.passed + self.failed,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "failures": self.failures,
            "observations": self.observations,
        }
        if extra:
            result.update(extra)
        return result


def _safe(value: Any) -> Any:
    """只允许枚举/数字/字符串/短结构进入输出，杜绝原始样本泄露。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return repr(value)[:120]


# ---------------------------------------------------------------------
# C01：依赖闭包删除与字段约束（格式校验器 + 确定性试验桩）
# ---------------------------------------------------------------------
def case_c01() -> dict[str, Any]:
    rec = AssertRecorder("C01")
    sample = build_demo_sample()
    validator = StructuralValidator()
    tester = DependencyTester()
    initial_hash = sample.content_hash()
    rec.note("sample_content_hash", initial_hash[:16])
    rec.note("record_ids", sample.ids())

    # 1) 依赖闭包：删除 r_target 必须连带 r_mid、r_leaf
    closure_target = dependency_closure(sample, "r_target")
    rec.check(
        closure_target == ["r_leaf", "r_mid", "r_target"],
        "closure_delete_target_includes_dependents",
        ["r_leaf", "r_mid", "r_target"],
        closure_target,
    )

    # 2) 无被依赖记录的闭包只含自身
    closure_extra = dependency_closure(sample, "r_extra")
    rec.check(
        closure_extra == ["r_extra"],
        "closure_standalone_record_is_self",
        ["r_extra"],
        closure_extra,
    )

    # 3) 应用闭包删除后结构仍然有效（无悬空引用）
    transforms = dedupe_transforms(generate_transforms(sample))
    del_target = next(t for t in transforms if t.kind == "delete_records"
                      and "r_target" in t.rids)
    candidate = apply_transform(sample, del_target)
    rec.check(
        validator.validate(candidate),
        "closure_deleted_sample_is_structurally_valid",
        True,
        validator.validate(candidate),
    )

    # 4) 该候选破坏原失败类别：试验返回另一指纹，拒绝
    result = safe_run(tester, candidate, TARGET_FINGERPRINT)
    rec.check(
        result.status == STATUS_OTHER_FP,
        "closure_delete_loses_failure_category",
        STATUS_OTHER_FP,
        result.status,
    )

    # 5) 手动制造悬空引用的样本必须被校验器拒绝
    broken = sample.copy()
    broken.records = [r for r in broken.records if r.rid != "r_target"]
    violations = validator.check(broken)
    rec.check(
        any(v.startswith("dangling_reference") for v in violations),
        "dangling_reference_detected",
        "dangling_reference:*",
        violations,
    )
    rec.check(
        validator.validate(broken) is False,
        "broken_sample_rejected_by_validator",
        False,
        validator.validate(broken),
    )

    # 6) 字段最小长度约束：截断到 min_length 以下被拒绝
    trunc_below = next(
        t for t in transforms
        if t.kind == "truncate_field" and t.rids == ("r_mid",)
    )
    # 手工构造越界截断（引擎枚举本身不会产生越界变换）
    bad = sample.copy()
    bad.get("r_mid").fields["name"] = "ab"  # min_length=3
    rec.check(
        "min_length" in " ".join(validator.check(bad)),
        "min_length_violation_detected",
        "min_length:*",
        validator.check(bad),
    )

    # 7) 合法截断仍满足约束且试验保持原指纹
    valid_trunc = apply_transform(sample, trunc_below)
    name_len = len(valid_trunc.get("r_mid").fields["name"])
    rec.check(
        name_len >= valid_trunc.get("r_mid").specs["name"].min_length,
        "legal_truncate_respects_min_length",
        ">=3",
        name_len,
    )
    rec.check(
        safe_run(tester, valid_trunc, TARGET_FINGERPRINT).status == STATUS_MATCH,
        "legal_truncate_keeps_failure_fingerprint",
        STATUS_MATCH,
        safe_run(tester, valid_trunc, TARGET_FINGERPRINT).status,
    )

    # 8) 引擎端到端：无关记录删除被接受、依赖闭包删除被拒绝
    reducer = Reducer(
        sample=sample,
        tester=tester,
        validator=validator,
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=200,
    )
    report = reducer.run()
    final_ids = reducer.current.ids()
    rec.check(
        report.stop_reason == "minimal",
        "engine_reaches_transform_minimal",
        "minimal",
        report.stop_reason,
    )
    rec.check(
        "r_target" in final_ids and "r_mid" in final_ids and "r_leaf" in final_ids,
        "dependency_chain_preserved",
        "r_target/r_mid/r_leaf present",
        final_ids,
    )
    rec.check(
        "r_extra" not in final_ids,
        "unrelated_record_removed",
        "r_extra absent",
        final_ids,
    )
    rejected_kinds = [a.transform.kind for a in report.rejected]
    rec.check(
        any(
            a.transform.kind == "delete_records" and "r_target" in a.transform.rids
            for a in report.rejected
        ),
        "closure_delete_present_in_rejected",
        "delete_records containing r_target",
        rejected_kinds,
    )
    rec.note("final_record_count", len(final_ids))
    rec.note("trials_used", report.trials_used)
    return rec.summary(
        {
            "seed": {"sample_hash": initial_hash[:16], "records": len(sample.records)},
            "tester_version": tester.version,
        }
    )


# ---------------------------------------------------------------------
# C02：重复失败指纹 / 超时 / 其他异常 / 偶发匹配的接受与拒绝
# ---------------------------------------------------------------------
def case_c02() -> dict[str, Any]:
    rec = AssertRecorder("C02")
    sample = build_demo_sample()
    rec.note("sample_content_hash", sample.content_hash()[:16])

    # 1) 偶发同指纹：3 次中仅 1 次 match，不满足全部复现
    flaky = FlakyMatchTester()
    flaky_results = [safe_run(flaky, sample, TARGET_FINGERPRINT) for _ in range(3)]
    match_count = sum(r.status == STATUS_MATCH for r in flaky_results)
    rec.check(
        match_count == 1,
        "flaky_tester_matches_only_once_in_three",
        1,
        match_count,
    )
    rec.check(
        all_matches(flaky_results, 3) is False,
        "flaky_candidate_rejected",
        False,
        all_matches(flaky_results, 3),
    )

    # 2) 超时一律归类为 timeout，且不算成功
    timeout_results = [
        safe_run(TimeoutTester(), sample, TARGET_FINGERPRINT) for _ in range(3)
    ]
    rec.check(
        all(r.status == STATUS_TIMEOUT for r in timeout_results),
        "timeout_classified_not_success",
        [STATUS_TIMEOUT] * 3,
        [r.status for r in timeout_results],
    )
    rec.check(
        all_matches(timeout_results, 3) is False,
        "timeout_candidate_rejected",
        False,
        all_matches(timeout_results, 3),
    )

    # 3) 其他异常归类为 error，与超时区分，且不算成功
    error_results = [
        safe_run(OtherErrorTester(), sample, TARGET_FINGERPRINT) for _ in range(3)
    ]
    rec.check(
        all(r.status == STATUS_ERROR for r in error_results),
        "other_exception_classified_error",
        [STATUS_ERROR] * 3,
        [r.status for r in error_results],
    )
    rec.check(
        all(r.status != STATUS_TIMEOUT for r in error_results),
        "error_distinguished_from_timeout",
        "no timeout",
        [r.status for r in error_results],
    )
    rec.check(
        all_matches(error_results, 3) is False,
        "error_candidate_rejected",
        False,
        all_matches(error_results, 3),
    )

    # 4) 确定性匹配桩：required_repeats 次全部 match 才接受
    good_results = [
        safe_run(DependencyTester(), sample, TARGET_FINGERPRINT) for _ in range(3)
    ]
    rec.check(
        all(r.status == STATUS_MATCH for r in good_results),
        "deterministic_tester_reproduces_every_time",
        [STATUS_MATCH] * 3,
        [r.status for r in good_results],
    )
    rec.check(
        all_matches(good_results, 3) is True,
        "repeated_match_candidate_accepted",
        True,
        all_matches(good_results, 3),
    )

    # 5) 指纹不同归类 other_fp
    broken = sample.copy()
    broken.records = [r for r in broken.records if r.rid != "r_target"]
    other = safe_run(DependencyTester(), broken, TARGET_FINGERPRINT)
    rec.check(
        other.status == STATUS_OTHER_FP and other.fingerprint != TARGET_FINGERPRINT,
        "different_fingerprint_classified_other_fp",
        "other_fp with distinct fingerprint",
        {"status": other.status, "fingerprint": other.fingerprint},
    )

    # 6) 引擎层面：偶发匹配候选永远不会替换当前样本
    reducer = Reducer(
        sample=sample,
        tester=FlakyMatchTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=3,
        budget=40,
    )
    before = sample.content_hash()
    report = reducer.run()
    rec.check(
        reducer.current.content_hash() == before,
        "flaky_engine_never_replaces_sample",
        before[:16],
        reducer.current.content_hash()[:16],
    )
    rec.check(
        len(report.accepted) == 0,
        "flaky_engine_zero_accepted_transforms",
        0,
        len(report.accepted),
    )
    rec.note("flaky_trials_used", report.trials_used)
    return rec.summary(
        {"seed": {"sample_hash": sample.content_hash()[:16], "mode": "deterministic_stubs"}}
    )


# ---------------------------------------------------------------------
# C03：总预算、暂停恢复、缓存三要素绑定
# ---------------------------------------------------------------------
def case_c03() -> dict[str, Any]:
    rec = AssertRecorder("C03")
    sample = build_demo_sample()
    tester = DependencyTester()
    initial_hash = sample.content_hash()
    rec.note("sample_content_hash", initial_hash[:16])

    # 1) 总预算硬约束：预算 4、每次接受需 2 次试验
    reducer = Reducer(
        sample=sample,
        tester=tester,
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=4,
    )
    report = reducer.run()
    rec.check(
        report.trials_used <= 4,
        "trials_never_exceed_budget",
        "<=4",
        report.trials_used,
    )
    rec.check(
        report.stop_reason == "budget_exhausted",
        "stops_on_budget_exhaustion",
        "budget_exhausted",
        report.stop_reason,
    )
    rec.check(
        report.trials_used == 4,
        "budget_fully_consumed",
        4,
        report.trials_used,
    )

    # 2) 暂停恢复：保存状态 -> 新引擎恢复 -> 追加预算继续，结果可复现
    paused = Reducer(
        sample=build_demo_sample(),
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=5,
    )
    paused_report = paused.run()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        paused.save_state(state_path)
        saved = json.load(open(state_path, encoding="utf-8"))
        rec.check(
            saved["trials_used"] == paused.trials_used
            and saved["budget"] == 5
            and saved["target_fingerprint"] == TARGET_FINGERPRINT,
            "state_file_captures_budget_and_progress",
            {"trials_used": paused.trials_used, "budget": 5},
            {"trials_used": saved["trials_used"], "budget": saved["budget"]},
        )

        resumed = Reducer.load_state(
            state_path, tester=DependencyTester(), new_budget=300
        )
        rec.check(
            resumed.trials_used == paused.trials_used,
            "resume_restores_trial_counter",
            paused.trials_used,
            resumed.trials_used,
        )
        rec.check(
            resumed.current.content_hash() == paused.current.content_hash(),
            "resume_restores_current_candidate",
            paused.current.content_hash()[:16],
            resumed.current.content_hash()[:16],
        )
        resumed_report = resumed.run()

    # 对照：一次性给足预算的连续运行
    continuous = Reducer(
        sample=build_demo_sample(),
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=300,
    )
    continuous_report = continuous.run()
    rec.check(
        resumed.current.content_hash() == continuous.current.content_hash(),
        "resumed_run_matches_continuous_run_result",
        continuous.current.content_hash()[:16],
        resumed.current.content_hash()[:16],
    )
    rec.check(
        resumed_report.stop_reason == "minimal",
        "resumed_run_reaches_minimal",
        "minimal",
        resumed_report.stop_reason,
    )

    # 3) 恢复时试验器版本不一致必须拒绝
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        paused.save_state(state_path)
        version_rejected = False
        try:
            Reducer.load_state(state_path, tester=AlwaysTargetTester())
        except ValueError:
            version_rejected = True
        rec.check(
            version_rejected,
            "resume_rejects_tester_version_mismatch",
            True,
            version_rejected,
        )

    # 4) 缓存绑定：内容哈希 + 试验器版本 + 重复次数
    cache = TrialCache()
    h1 = sample.content_hash()
    entry = cache.get(h1, tester.version, 2)
    rec.check(entry is None, "cache_miss_on_fresh_cache", None, entry)
    r1 = safe_run(tester, sample, TARGET_FINGERPRINT)
    cache.append(h1, tester.version, 2, r1)
    rec.check(
        cache.get(h1, tester.version, 2) is not None,
        "cache_hit_with_same_triple",
        "entry",
        "entry" if cache.get(h1, tester.version, 2) else None,
    )
    rec.check(
        cache.get(h1, "dep-tester-9.9", 2) is None,
        "cache_miss_on_tester_version_change",
        None,
        cache.get(h1, "dep-tester-9.9", 2),
    )
    rec.check(
        cache.get(h1, tester.version, 3) is None,
        "cache_miss_on_repeat_count_change",
        None,
        cache.get(h1, tester.version, 3),
    )
    other_sample = sample.copy()
    other_sample.records = other_sample.records[:-1]
    rec.check(
        cache.get(other_sample.content_hash(), tester.version, 2) is None,
        "cache_miss_on_content_change",
        None,
        cache.get(other_sample.content_hash(), tester.version, 2),
    )

    # 5) 部分试验记录可跨暂停补齐：预算中断后缓存保留 1 条，恢复只补 1 次
    partial = Reducer(
        sample=build_demo_sample(),
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=3,
    )
    partial.run()
    partial_entries = partial.cache.to_dict()["entries"]
    partial_sizes = sorted(len(e["results"]) for e in partial_entries)
    rec.check(
        partial_sizes and partial_sizes[-1] >= 1,
        "partial_trial_records_persisted_in_cache",
        ">=1 result in an entry",
        partial_sizes,
    )
    return rec.summary(
        {
            "seed": {
                "sample_hash": initial_hash[:16],
                "records": len(sample.records),
                "required_repeats": 2,
            },
            "tester_version": tester.version,
            "trials": {
                "budgeted_run": paused_report.trials_used,
                "continuous_run": continuous_report.trials_used,
                "resumed_total": resumed.trials_used,
            },
        }
    )


# ---------------------------------------------------------------------
# C04：变换级最小性与预算停止报告；缩减结果保留原失败类别
# ---------------------------------------------------------------------
def case_c04() -> dict[str, Any]:
    rec = AssertRecorder("C04")
    sample = build_demo_sample()
    initial_hash = sample.content_hash()
    rec.note("sample_content_hash", initial_hash[:16])

    # 1) 充分预算下达到变换级最小：枚举的每个剩余变换均被拒绝或无效
    reducer = Reducer(
        sample=sample,
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=200,
    )
    report = reducer.run()
    rec.check(
        report.stop_reason == "minimal",
        "full_budget_run_reports_minimal",
        "minimal",
        report.stop_reason,
    )
    remaining = reducer.current
    remaining_transforms = dedupe_transforms(generate_transforms(remaining))
    rec.check(
        len(remaining_transforms) > 0,
        "minimal_sample_still_has_enumerable_transforms",
        ">0 transforms",
        len(remaining_transforms),
    )
    # 对最小样本再完整跑一轮：不存在任何可接受变换
    second = Reducer(
        sample=remaining.copy(),
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=200,
    )
    second_report = second.run()
    rec.check(
        len(second_report.accepted) == 0,
        "no_single_transform_can_reduce_further",
        0,
        len(second_report.accepted),
    )
    rec.check(
        second.current.content_hash() == remaining.content_hash(),
        "second_pass_sample_identical",
        remaining.content_hash()[:16],
        second.current.content_hash()[:16],
    )

    # 2) 缩减结果保留原失败类别：最终样本仍稳定复现目标指纹
    confirm = [
        safe_run(DependencyTester(), remaining, TARGET_FINGERPRINT) for _ in range(3)
    ]
    rec.check(
        all(r.status == STATUS_MATCH and r.fingerprint == TARGET_FINGERPRINT
            for r in confirm),
        "reduced_sample_retains_failure_category",
        "3x match with target fingerprint",
        [r.status for r in confirm],
    )
    # 关键记录与依赖链仍在
    rec.check(
        all(remaining.contains(rid) for rid in ("r_target", "r_mid", "r_leaf")),
        "failure_dependency_chain_intact",
        "r_target/r_mid/r_leaf present",
        remaining.ids(),
    )

    # 3) 预算停止报告：预算极小时报告 budget_exhausted 且不声称最小
    starved = Reducer(
        sample=build_demo_sample(),
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=2,
    )
    starved_report = starved.run()
    rec.check(
        starved_report.stop_reason == "budget_exhausted",
        "starved_run_reports_budget_not_minimal",
        "budget_exhausted",
        starved_report.stop_reason,
    )
    rec.check(
        starved_report.trials_used == 2
        and starved_report.budget - starved_report.trials_used == 0,
        "budget_report_counts_are_consistent",
        {"used": 2, "remaining": 0},
        {
            "used": starved_report.trials_used,
            "remaining": starved_report.budget - starved_report.trials_used,
        },
    )
    # 预算停止时不得把当前候选宣传为最小：仍存在未评估完的变换
    rec.check(
        len(dedupe_transforms(generate_transforms(starved.current))) > 0,
        "budget_stop_does_not_claim_minimality",
        ">0 untested transforms remain",
        len(dedupe_transforms(generate_transforms(starved.current))),
    )

    # 4) 被接受的变换都有完整重复试验记录；被拒绝候选不得替换样本
    rec.check(
        all(
            len(a.trial_results) == 2
            and all(r.status == STATUS_MATCH for r in a.trial_results)
            for a in report.accepted
        ),
        "every_accepted_transform_has_full_repeated_matches",
        "2x match per accepted transform",
        [
            [r.status for r in a.trial_results]
            for a in report.accepted
        ],
    )
    rec.check(
        report.initial_hash != report.final_hash,
        "sample_actually_shrank",
        "different hashes",
        {
            "initial": report.initial_hash[:16],
            "final": report.final_hash[:16],
        },
    )
    rec.note("record_count_change", {
        "initial": report.initial_record_count,
        "final": report.final_record_count,
    })
    return rec.summary(
        {
            "seed": {
                "sample_hash": initial_hash[:16],
                "records": len(sample.records),
                "required_repeats": 2,
            },
            "tester_version": DependencyTester().version,
            "stop": {
                "well_funded": report.stop_reason,
                "starved": starved_report.stop_reason,
            },
        }
    )


CASES: dict[str, Callable[[], dict[str, Any]]] = {
    "C01": case_c01,
    "C02": case_c02,
    "C03": case_c03,
    "C04": case_c04,
}


def main(argv: list[str]) -> int:
    if "--list" in argv:
        print(json.dumps({"cases": list(CASES.keys())}, ensure_ascii=False, indent=2))
        return 0

    if "--case" not in argv:
        print(json.dumps({"error": "missing --case; use --list"}), file=sys.stderr)
        return 2
    index = argv.index("--case")
    if index + 1 >= len(argv):
        print(json.dumps({"error": "--case requires a value"}), file=sys.stderr)
        return 2
    requested = argv[index + 1]
    selected = list(CASES.keys()) if requested == "all" else [requested]

    unknown = [c for c in selected if c not in CASES]
    if unknown:
        print(json.dumps({"error": f"unknown case: {unknown}"}), file=sys.stderr)
        return 2

    outputs: list[dict[str, Any]] = []
    exit_code = 0
    for case_name in selected:
        try:
            output = CASES[case_name]()
        except Exception:  # noqa: BLE001 - 未实现/崩溃也必须非零退出
            output = {
                "case": case_name,
                "assertions": 0,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "failures": ["case_raised_exception"],
                "observations": [
                    {"note": "traceback", "value": traceback.format_exc()[:500]}
                ],
            }
        outputs.append(output)
        # 零断言、任一失败、整组跳过均视为不通过
        if (
            output["assertions"] == 0
            or output["failed"] > 0
            or output["skipped"] == output["assertions"]
            and output["assertions"] > 0
        ):
            exit_code = 1

    print(json.dumps(outputs if len(outputs) > 1 else outputs[0],
                     ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
