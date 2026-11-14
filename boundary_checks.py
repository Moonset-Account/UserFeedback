"""本题样例/边界断言：逐条记录实际结果并与期望值对比。

case_id 稳定；每条断言给出 actual（实际运行结果）与 expected（参考模型期望值）。
"""

from __future__ import annotations

from typing import Dict, List

from session_model.regression import evaluate
from session_model.scenarios import run_all


def _find(trace, **cond):
    out = []
    for d in trace.decisions:
        if all(getattr(d, k) == v for k, v in cond.items()):
            out.append(d)
    return out


def run_boundary_checks() -> List[Dict]:
    traces = {t.scenario: t for t in run_all()}
    checks: List[Dict] = []

    def add(case_id: str, actual, expected) -> None:
        checks.append({
            "case_id": case_id,
            "actual": actual,
            "expected": expected,
            "ok": actual == expected,
        })

    nl = traces["normal_login"]
    add("BC-01 正常登录后新代次可访问",
        _find(nl, action="READ", session_id="s-login", generation=1)[0].verdict, "ALLOW")
    add("BC-02 匿名代次(gen0)拒绝",
        _find(nl, action="READ", session_id="s-login", generation=0)[0].verdict,
        "DENY_NOT_AUTHENTICATED")
    add("BC-03 未来代次返回待确认",
        _find(nl, action="READ", session_id="s-login", generation=2)[0].verdict,
        "PENDING_REPLICATION")
    add("BC-04 未知会话安全拒绝",
        _find(nl, action="READ", session_id="ghost")[0].verdict, "DENY_UNKNOWN_SESSION")
    add("BC-05 匿名状态提权被拒绝",
        _find(nl, action="COMMIT:ELEVATE")[0].verdict, "REJECT_INVALID_TRANSITION")

    cr = traces["concurrent_reads"]
    add("BC-06 新代次在最新副本可访问",
        _find(cr, action="READ", node="replica-a", generation=2)[0].verdict, "ALLOW")
    add("BC-07 旧代次在最新副本已失效",
        _find(cr, action="READ", node="replica-a", generation=1)[0].verdict,
        "DENY_STALE_GENERATION")
    add("BC-08 滞后副本对新代次返回待确认",
        _find(cr, action="READ", node="replica-b", generation=2)[0].verdict,
        "PENDING_REPLICATION")

    rt = traces["retry_after_interruption"]
    add("BC-09 确认丢失后重试不重复换发",
        sum(1 for e in rt.primary.log if e.op_id == "op-r-login"), 1)
    login_commits = _find(rt, action="COMMIT:LOGIN", op_id="op-r-login")
    add("BC-10 重试返回相同代次", login_commits[-1].generation, 1)
    add("BC-11 重试标记为幂等回放", login_commits[-1].verdict, "COMMIT_IDEMPOTENT_REPLAY")
    add("BC-12 确认缺失且未复制时副本安全拒绝",
        _find(rt, action="READ", generation=1)[0].verdict, "DENY_UNKNOWN_SESSION")
    elev_commits = _find(rt, action="COMMIT:ELEVATE", op_id="op-r-elevate")
    add("BC-13 应用前中断不产生提交", elev_commits[0].verdict, "COMMIT_INTERRUPTED")
    add("BC-14 中断后重试正常提交", elev_commits[-1].verdict, "COMMITTED")

    lo = traces["late_logout"]
    rb_reads = _find(lo, action="READ", node="replica-b", generation=1)
    add("BC-15 注销复制前滞后副本仍放行(信息性)", rb_reads[1].verdict, "ALLOW")
    add("BC-16 注销复制后旧代次拒绝", rb_reads[-1].verdict, "DENY_LOGGED_OUT")
    add("BC-17 主节点注销后立即拒绝",
        _find(lo, action="READ", node="primary")[0].verdict, "DENY_LOGGED_OUT")

    results = evaluate(list(traces.values()))
    add("BC-18 全部回归规则通过", [r.status for r in results], ["PASS"] * len(results))
    return checks
