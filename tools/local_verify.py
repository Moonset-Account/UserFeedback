#!/usr/bin/env python3
"""本地核验入口：运行 unittest 套件 + 本题样例/边界断言。

用法: /usr/local/bin/python3 tools/local_verify.py
退出码: 0 = 单测全部通过且实际执行未跳过 >= 1，且全部样例/边界断言通过；
        非 0 = 发现 0 项测试、全部跳过、异常、任何失败或任一样例断言失败。
"""

import json
import os
import sys
import unittest

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKSPACE)

import admission  # noqa: E402

NOW = "2026-11-14T00:00:00Z"


def load_example(name):
    with open(os.path.join(WORKSPACE, "examples", name), encoding="utf-8") as fh:
        return json.load(fh)


def run_unittest_suite():
    """discover -> 校验数量 -> 实际运行 -> 校验执行数与失败。"""
    loader = unittest.defaultTestLoader
    suite = loader.discover(os.path.join(WORKSPACE, "tests"),
                            top_level_dir=WORKSPACE)
    count = suite.countTestCases()
    print("[unittest] 发现测试用例数: %d" % count)
    if count < 1:
        print("[unittest] 失败: 未发现任何测试用例")
        return False

    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun
    skipped = len(result.skipped)
    actually_ran = executed - skipped
    print("[unittest] 执行=%d 跳过=%d 实际执行未跳过=%d 失败=%d 错误=%d"
          % (executed, skipped, actually_ran,
             len(result.failures), len(result.errors)))
    if actually_ran < 1:
        print("[unittest] 失败: 实际执行且未跳过的测试数为 0")
        return False
    if result.failures or result.errors:
        print("[unittest] 失败: 存在失败或错误")
        return False
    return True


def sample_cases():
    """本题样例/边界断言：每条返回 (case_id, 断言内容, 输入, 实际输出, 期望值, 是否通过)。"""
    baseline = load_example("baseline.json")
    exceptions = load_example("exceptions.json")
    deployed = load_example("deployed.json")
    blocked = load_example("candidate_blocked.json")
    allowed = load_example("candidate_allowed.json")

    cases = []

    def record(case_id, assertion, inputs, actual, expected, ok):
        cases.append({
            "case_id": case_id,
            "assertion": assertion,
            "input": inputs,
            "actual": actual,
            "expected": expected,
            "pass": bool(ok),
        })

    # S1: 违规候选被拦截，且逐项给出位置/生效值/例外状态
    r1 = admission.check_release(blocked, baseline=baseline, exceptions=exceptions,
                                 deployed=deployed, now=NOW)
    record("S1-decision", "违规候选整体拦截", "candidate_blocked.json",
           r1["decision"], "block", r1["decision"] == "block")

    types = {}
    for v in r1["candidate"]["violations"]:
        types.setdefault(v["type"], []).append(v)
    record("S2-privileged-location-value",
           "普通容器特权违规: 位置/生效值/例外状态",
           "containers[0](app) privileged=true",
           {k: types["privileged"][0][k] for k in ("location", "effectiveValue", "exception")},
           {"location": "containers[0](app)", "effectiveValue": True,
            "exception": {"status": "none", "exceptionId": None}},
           types["privileged"][0]["location"] == "containers[0](app)"
           and types["privileged"][0]["effectiveValue"] is True
           and types["privileged"][0]["exception"]["status"] == "none")

    record("S3-initcontainer-root",
           "初始化容器 root 违规: 生效值 0 且例外已到期",
           "initContainers[0](migrate) runAsUser=0, EX-2026-003 已过期",
           {k: types["run-as-root"][0][k] for k in ("location", "effectiveValue", "exception")},
           {"location": "initContainers[0](migrate)", "effectiveValue": 0,
            "exception": {"status": "expired", "exceptionId": "EX-2026-003"}},
           types["run-as-root"][0]["location"] == "initContainers[0](migrate)"
           and types["run-as-root"][0]["effectiveValue"] == 0
           and types["run-as-root"][0]["exception"]["status"] == "expired")

    record("S4-hostpath-exception-applied",
           "挂载类例外五要素+宿主机路径全匹配且未到期 → 豁免",
           "EX-2026-002 hostPath=/var/lib/prod-data",
           types["host-path"][0]["exception"],
           {"status": "applied", "exceptionId": "EX-2026-002"},
           types["host-path"][0]["exception"]["status"] == "applied"
           and types["host-path"][0]["suppressed"] is True)

    record("S5-digest-change-invalidates-exception",
           "镜像摘要变化后旧例外不再匹配（EX-2026-001 绑定 sha256:aaa111，候选为 sha256:ccc333）",
           "candidate image digest sha256:ccc333 vs exception sha256:aaa111",
           types["privileged"][0]["exception"]["status"],
           "none",
           types["privileged"][0]["exception"]["status"] == "none"
           and types["privileged"][0]["imageDigest"] == "sha256:ccc333")

    record("S6-new-violations-vs-deployed",
           "与已上线版本同基线比对，标出候选新增违规",
           "deployed.json vs candidate_blocked.json",
           sorted(v["type"] for v in r1.get("newViolations", [])),
           ["privileged", "run-as-root"],
           sorted(v["type"] for v in r1.get("newViolations", []))
           == ["privileged", "run-as-root"])

    # S7: 合规候选放行（hostPath 命中基线放行目录）
    r2 = admission.check_release(allowed, baseline=baseline, now=NOW)
    record("S7-allow-clean-candidate", "合规候选放行",
           "candidate_allowed.json + baseline allowedPaths",
           r2["decision"], "allow", r2["decision"] == "allow")

    # B1: 边界 —— 容器显式 UID 覆盖工作负载默认值
    override = {
        "namespace": "payments", "name": "w",
        "defaults": {"runAsUser": 1000},
        "containers": [{"name": "c", "image": "r/i@sha256:x",
                        "securityContext": {"runAsUser": 0}}],
    }
    r3 = admission.check_release(override, now=NOW)
    root_hits = [v for v in r3["candidate"]["violations"] if v["type"] == "run-as-root"]
    record("B1-container-uid-overrides-default",
           "容器 runAsUser=0 覆盖工作负载默认 1000 → 按 root 拦截",
           "container runAsUser=0, workload default 1000",
           {"decision": r3["decision"],
            "effectiveValue": root_hits[0]["effectiveValue"] if root_hits else None},
           {"decision": "block", "effectiveValue": 0},
           r3["decision"] == "block" and root_hits
           and root_hits[0]["effectiveValue"] == 0)

    # B2: 边界 —— 两处都没有 runAsUser → 运行身份无法确认拦截
    unknown = {"namespace": "payments", "name": "w",
               "containers": [{"name": "c", "image": "r/i@sha256:x"}]}
    r4 = admission.check_release(unknown, now=NOW)
    unconfirmed = [v for v in r4["candidate"]["violations"]
                   if v["type"] == "run-identity-unconfirmed"]
    record("B2-unconfirmed-identity-blocks",
           "容器与工作负载默认值均无 runAsUser → 运行身份无法确认拦截",
           "no runAsUser anywhere",
           {"decision": r4["decision"], "unconfirmed": bool(unconfirmed)},
           {"decision": "block", "unconfirmed": True},
           r4["decision"] == "block" and bool(unconfirmed))

    # B3: 边界 —— 例外到期日恰好等于评估时刻 → 视为已到期
    exp_edge = dict(unknown)
    exp_edge["containers"] = [dict(unknown["containers"][0])]
    exc = [{"id": "EX-EDGE", "namespace": "payments", "workload": "w",
            "container": "c", "imageDigest": "sha256:x",
            "violation": "run-identity-unconfirmed", "expiresAt": NOW}]
    r5 = admission.check_release(exp_edge, exceptions=exc, now=NOW)
    edge = [v for v in r5["candidate"]["violations"]
            if v["type"] == "run-identity-unconfirmed"][0]
    record("B3-exception-expired-at-now",
           "例外到期时刻等于评估时刻 → 不豁免",
           "expiresAt == now",
           edge["exception"]["status"], "expired",
           edge["exception"]["status"] == "expired" and r5["decision"] == "block")

    return cases


def main():
    ok = True

    print("=" * 60)
    print("阶段 1: unittest 套件")
    print("=" * 60)
    if not run_unittest_suite():
        ok = False

    print("=" * 60)
    print("阶段 2: 样例/边界断言")
    print("=" * 60)
    try:
        cases = sample_cases()
    except Exception as exc:  # 样例执行异常同样判失败
        print("[samples] 执行异常: %r" % (exc,))
        return 1
    for case in cases:
        status = "PASS" if case["pass"] else "FAIL"
        print("[%s] %s" % (status, case["case_id"]))
        print("  断言: %s" % case["assertion"])
        print("  输入: %s" % case["input"])
        print("  实际: %s" % json.dumps(case["actual"], ensure_ascii=False))
        print("  期望: %s" % json.dumps(case["expected"], ensure_ascii=False))
        if not case["pass"]:
            ok = False
    if not cases:
        print("[samples] 失败: 没有任何样例断言")
        ok = False

    print("=" * 60)
    print("总体结果: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
