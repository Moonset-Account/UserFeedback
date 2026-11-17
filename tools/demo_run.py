#!/usr/bin/env python3
"""端到端可重放演示（合成数据，确定性桩，无外部服务）。

运行：
    .venv/bin/python -I tools/demo_run.py

三个场景：
1. 有依赖的删除：闭包删除破坏失败类别 -> 拒绝；无关记录删除 -> 接受；
2. 偶发同指纹：FlakyMatchTester 三次中仅一次匹配 -> 候选永不被接受；
3. 暂停恢复：小预算中断 -> 保存状态 -> 追加预算恢复，结果与连续运行一致。

输出只包含哈希前缀、计数和状态枚举，不含原始样本内容。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from reducer import Reducer, StructuralValidator, safe_run  # noqa: E402
from reducer.engine import apply_transform, dedupe_transforms, generate_transforms  # noqa: E402
from reducer.stubs import (  # noqa: E402
    DependencyTester,
    FlakyMatchTester,
    TARGET_FINGERPRINT,
    build_demo_sample,
)
from reducer.tester import STATUS_MATCH, STATUS_OTHER_FP  # noqa: E402


def scenario_dependency_delete() -> dict:
    sample = build_demo_sample()
    tester = DependencyTester()
    validator = StructuralValidator()
    transforms = dedupe_transforms(generate_transforms(sample))
    closure = next(
        t for t in transforms
        if t.kind == "delete_records" and "r_target" in t.rids
    )
    candidate = apply_transform(sample, closure)
    verdict = safe_run(tester, candidate, TARGET_FINGERPRINT)

    reducer = Reducer(
        sample=sample,
        tester=tester,
        validator=validator,
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=300,
    )
    report = reducer.run()
    return {
        "scenario": "dependency_aware_deletion",
        "sample_hash": sample.content_hash()[:16],
        "closure_delete_rids": sorted(closure.rids),
        "closure_candidate_format_valid": validator.validate(candidate),
        "closure_candidate_trial_status": verdict.status,
        "closure_candidate_accepted": verdict.status == STATUS_MATCH,
        "engine_stop_reason": report.stop_reason,
        "engine_accepted_transforms": len(report.accepted),
        "engine_rejected_transforms": len(report.rejected),
        "engine_invalid_transforms": len(report.invalid),
        "final_record_count": len(reducer.current.records),
        "final_sample_hash": reducer.current.content_hash()[:16],
        "final_confirm_3x": [
            safe_run(tester, reducer.current, TARGET_FINGERPRINT).status
            for _ in range(3)
        ],
    }


def scenario_flaky() -> dict:
    sample = build_demo_sample()
    flaky = FlakyMatchTester()
    direct = [safe_run(flaky, sample, TARGET_FINGERPRINT) for _ in range(3)]
    reducer = Reducer(
        sample=sample,
        tester=FlakyMatchTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=3,
        budget=300,
    )
    report = reducer.run()
    return {
        "scenario": "intermittent_same_fingerprint",
        "sample_hash": sample.content_hash()[:16],
        "three_trial_statuses": [r.status for r in direct],
        "intermittent_matches": sum(r.status == STATUS_MATCH for r in direct),
        "engine_accepted_transforms": len(report.accepted),
        "engine_stop_reason": report.stop_reason,
        "engine_trials_used": report.trials_used,
        "sample_replaced": reducer.current.content_hash() != sample.content_hash(),
        "claim": "not_minimal: flaky result can never replace current sample",
    }


def scenario_pause_resume() -> dict:
    tester = DependencyTester()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "reducer_state.json")
        paused = Reducer(
            sample=build_demo_sample(),
            tester=tester,
            target_fingerprint=TARGET_FINGERPRINT,
            required_repeats=2,
            budget=5,
        )
        paused.run()
        assert paused.report.stop_reason == "budget_exhausted"
        paused.save_state(state_path)

        resumed = Reducer.load_state(state_path, tester=tester, new_budget=300)
        resumed_report = resumed.run()

    continuous = Reducer(
        sample=build_demo_sample(),
        tester=DependencyTester(),
        target_fingerprint=TARGET_FINGERPRINT,
        required_repeats=2,
        budget=300,
    )
    continuous.run()
    return {
        "scenario": "pause_and_resume",
        "paused_budget": 5,
        "paused_trials_used": paused.trials_used,
        "resumed_total_trials_used": resumed.trials_used,
        "resumed_stop_reason": resumed_report.stop_reason,
        "resumed_hash": resumed.current.content_hash()[:16],
        "continuous_hash": continuous.current.content_hash()[:16],
        "resume_matches_continuous": (
            resumed.current.content_hash() == continuous.current.content_hash()
        ),
        "claim": "resumed and continuous runs converge to the same reduced sample",
    }


def main() -> int:
    output = [
        scenario_dependency_delete(),
        scenario_flaky(),
        scenario_pause_resume(),
    ]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
