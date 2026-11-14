"""合成测试场景库（离线夹具，无外部依赖）。

每个场景包含：
- case_id: 稳定编号
- description: 业务语义
- input: 完整的程序输入 JSON 对象
- expected: 以 build_id 为键的期望结论（status、逐修复 status、缺失记录 code 列表）
- extra_checks: 对完整结果的附加断言描述与函数

这些场景同时被 tests/ 下的 unittest 和 tools/local_verify.py 使用，
保证样例断言与单测来自同一份输入与期望值。
"""
from __future__ import annotations


def linear_input() -> dict:
    return {
        "commits": [
            {"id": "c0", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "c1", "parents": ["c0"], "fix_ids": ["F1"], "reverts": []},
            {"id": "c2", "parents": ["c1"], "fix_ids": [], "reverts": ["F1"]},
            {"id": "c3", "parents": ["c2"], "fix_ids": ["F1"], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["c0"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-base", "line": "LTS", "source_commit": "c0"},
            {"build_id": "B-fixed", "line": "LTS", "source_commit": "c1"},
            {"build_id": "B-reverted", "line": "LTS", "source_commit": "c2"},
            {"build_id": "B-restored", "line": "LTS", "source_commit": "c3"},
        ],
        "merge_resolutions": [],
    }


def merge_conflict_input() -> dict:
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "a1", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
            {"id": "b1", "parents": ["r"], "fix_ids": [], "reverts": []},
            {"id": "m1", "parents": ["a1", "b1"], "fix_ids": [], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-active-side", "line": "LTS", "source_commit": "a1"},
            {"build_id": "B-merge-unknown", "line": "LTS", "source_commit": "m1"},
        ],
        "merge_resolutions": [],
    }


def merge_resolved_input() -> dict:
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "a1", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
            {"id": "b1", "parents": ["r"], "fix_ids": [], "reverts": []},
            {"id": "m1", "parents": ["a1", "b1"], "fix_ids": [], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-resolved-fixed", "line": "LTS", "source_commit": "m1"}
        ],
        "merge_resolutions": [
            {"commit": "m1", "fix_id": "F1", "resolved": "fixed", "line": "LTS"}
        ],
    }


def two_line_scope_input() -> dict:
    # 同一修复标识只被 LTS 认可，MAIN 不认可
    return {
        "commits": [
            {"id": "g0", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "g1", "parents": ["g0"], "fix_ids": ["F1"], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["g0"], "recognized_fix_ids": ["F1"]},
            {"name": "MAIN", "roots": ["g0"], "recognized_fix_ids": ["F2"]},
        ],
        "builds": [
            {"build_id": "B-lts", "line": "LTS", "source_commit": "g1"},
            {"build_id": "B-main", "line": "MAIN", "source_commit": "g1"},
        ],
        "merge_resolutions": [],
    }


def bare_revert_input() -> dict:
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "f1", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
            {"id": "rv", "parents": ["f1"], "fix_ids": [], "reverts": [{"fix_id": None}]},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-bare-reverted", "line": "LTS", "source_commit": "rv"}
        ],
        "merge_resolutions": [],
    }


def unreachable_source_input() -> dict:
    return {
        "commits": [
            {"id": "r1", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "r2", "parents": [], "fix_ids": ["F1"], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r1"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-orphan", "line": "LTS", "source_commit": "r2"}
        ],
        "merge_resolutions": [],
    }


def three_parent_merge_input() -> dict:
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "p1", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
            {"id": "p2", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
            {"id": "p3", "parents": ["r"], "fix_ids": [], "reverts": []},
            {"id": "m3", "parents": ["p1", "p2", "p3"], "fix_ids": [], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-three-way", "line": "LTS", "source_commit": "m3"}
        ],
        "merge_resolutions": [],
    }


def multi_fix_input() -> dict:
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "f1f2", "parents": ["r"], "fix_ids": ["F1", "F2"], "reverts": []},
            {"id": "rv1", "parents": ["f1f2"], "fix_ids": [], "reverts": ["F1"]},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1", "F2"]}
        ],
        "builds": [
            {"build_id": "B-mixed", "line": "LTS", "source_commit": "rv1"}
        ],
        "merge_resolutions": [],
    }


def missing_record_input() -> dict:
    # 显式撤回不存在的修复、且修复标识不被维护线认可
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "x", "parents": ["r"], "fix_ids": ["FX"], "reverts": ["F9"]},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [
            {"build_id": "B-anomaly", "line": "LTS", "source_commit": "x"}
        ],
        "merge_resolutions": [],
    }


def line_scoped_resolution_input() -> dict:
    # 同一合并提交：LTS 有行级解决记录 fixed；MAIN 无记录，应保持 unknown
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "a1", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
            {"id": "b1", "parents": ["r"], "fix_ids": [], "reverts": []},
            {"id": "m1", "parents": ["a1", "b1"], "fix_ids": [], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]},
            {"name": "MAIN", "roots": ["r"], "recognized_fix_ids": ["F1"]},
        ],
        "builds": [
            {"build_id": "B-lts-resolved", "line": "LTS", "source_commit": "m1"},
            {"build_id": "B-main-unknown", "line": "MAIN", "source_commit": "m1"},
        ],
        "merge_resolutions": [
            {"commit": "m1", "fix_id": "F1", "resolved": "fixed", "line": "LTS"}
        ],
    }


SCENARIOS = [
    {
        "case_id": "SC-01-linear-lifecycle",
        "description": "线性历史：未修复→已修复→撤回后重新受影响→再次修复",
        "input": linear_input(),
        "expected": {
            "B-base": {"status": "affected", "fix": {"F1": "affected"}, "missing_codes": []},
            "B-fixed": {"status": "fixed", "fix": {"F1": "fixed"}, "missing_codes": []},
            "B-reverted": {"status": "reaffected", "fix": {"F1": "reaffected"}, "missing_codes": []},
            "B-restored": {"status": "fixed", "fix": {"F1": "fixed"}, "missing_codes": []},
        },
    },
    {
        "case_id": "SC-02-merge-conflict-unknown",
        "description": "合并父线状态矛盾且无显式解决记录：合并节点保留未知",
        "input": merge_conflict_input(),
        "expected": {
            "B-active-side": {"status": "fixed", "fix": {"F1": "fixed"}, "missing_codes": []},
            "B-merge-unknown": {
                "status": "unknown",
                "fix": {"F1": "unknown"},
                "missing_codes": ["merge_conflict", "missing_merge_resolution"],
            },
        },
    },
    {
        "case_id": "SC-03-merge-explicit-resolution",
        "description": "合并矛盾被显式解决记录裁定为 fixed",
        "input": merge_resolved_input(),
        "expected": {
            "B-resolved-fixed": {
                "status": "fixed",
                "fix": {"F1": "fixed"},
                "missing_codes": [],
            }
        },
    },
    {
        "case_id": "SC-04-line-scoped-recognition",
        "description": "构建只在其维护线上判断：同一提交在 LTS 已修复，在 MAIN 仍受影响",
        "input": two_line_scope_input(),
        "expected": {
            "B-lts": {"status": "fixed", "fix": {"F1": "fixed"}, "missing_codes": []},
            "B-main": {"status": "affected", "fix": {"F2": "affected"},
                       "missing_codes": ["unrecognized_fix_record"]},
        },
    },
    {
        "case_id": "SC-05-bare-revert",
        "description": "裸撤回（不指定修复标识）撤回当前所有生效修复",
        "input": bare_revert_input(),
        "expected": {
            "B-bare-reverted": {
                "status": "reaffected",
                "fix": {"F1": "reaffected"},
                "missing_codes": [],
            }
        },
    },
    {
        "case_id": "SC-06-source-unreachable",
        "description": "来源提交不在维护线可达范围内：无法判定并给出缺失记录",
        "input": unreachable_source_input(),
        "expected": {
            "B-orphan": {
                "status": "unknown",
                "fix": {"F1": "unknown"},
                "missing_codes": ["source_not_reachable_from_line"],
            }
        },
    },
    {
        "case_id": "SC-07-three-parent-merge",
        "description": "三父合并：两条父线生效、一条未生效，无解决记录仍为未知",
        "input": three_parent_merge_input(),
        "expected": {
            "B-three-way": {
                "status": "unknown",
                "fix": {"F1": "unknown"},
                "missing_codes": ["merge_conflict", "missing_merge_resolution"],
            }
        },
    },
    {
        "case_id": "SC-08-multi-fix-overall",
        "description": "多修复标识：一个仍生效、一个被撤回，部署总体为重新受影响",
        "input": multi_fix_input(),
        "expected": {
            "B-mixed": {
                "status": "reaffected",
                "fix": {"F1": "reaffected", "F2": "fixed"},
                "missing_codes": [],
            }
        },
    },
    {
        "case_id": "SC-09-missing-and-ignored-records",
        "description": "未被认可的修复与撤回记录进入缺失/异常清单，不改变结论",
        "input": missing_record_input(),
        "expected": {
            "B-anomaly": {
                "status": "affected",
                "fix": {"F1": "affected"},
                "missing_codes": [
                    "unrecognized_fix_record",
                    "unrecognized_revert_record",
                ],
            }
        },
    },
    {
        "case_id": "SC-10-line-scoped-resolution",
        "description": "行级解决记录只对该维护线生效，另一维护线合并仍未知",
        "input": line_scoped_resolution_input(),
        "expected": {
            "B-lts-resolved": {
                "status": "fixed",
                "fix": {"F1": "fixed"},
                "missing_codes": [],
            },
            "B-main-unknown": {
                "status": "unknown",
                "fix": {"F1": "unknown"},
                "missing_codes": ["merge_conflict", "missing_merge_resolution"],
            },
        },
    },
]


# 输入错误场景：期望解析阶段抛出 InputError
ERROR_SCENARIOS = [
    {
        "case_id": "SC-11-cycle-detected",
        "description": "提交图存在有向环：判定程序拒绝并报错",
        "input": {
            "commits": [
                {"id": "a", "parents": ["b"], "fix_ids": [], "reverts": []},
                {"id": "b", "parents": ["a"], "fix_ids": [], "reverts": []},
            ],
            "maintenance_lines": [
                {"name": "LTS", "roots": ["a"], "recognized_fix_ids": ["F1"]}
            ],
            "builds": [{"build_id": "B", "line": "LTS", "source_commit": "a"}],
            "merge_resolutions": [],
        },
        "error_contains": "有向环",
    },
    {
        "case_id": "SC-12-dangling-parent",
        "description": "父提交不存在：输入校验失败",
        "input": {
            "commits": [
                {"id": "a", "parents": ["ghost"], "fix_ids": [], "reverts": []}
            ],
            "maintenance_lines": [
                {"name": "LTS", "roots": ["a"], "recognized_fix_ids": ["F1"]}
            ],
            "builds": [{"build_id": "B", "line": "LTS", "source_commit": "a"}],
            "merge_resolutions": [],
        },
        "error_contains": "不存在的父提交",
    },
]
