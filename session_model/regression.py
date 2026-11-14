"""回归规则校验：对照参考模型检查场景轨迹。

规则：
    R1 新代次提交并复制到某节点后，该节点不得再放行旧代次（及时失效）。
    R2 同一 (session_id, op_id) 的重试必须幂等，不得重复换发新代次。
    R3 副本不得放行自身尚未复制到的代次；缺失写入确认时应安全拒绝
       或返回待确认。
    R4 通过校验的新会话代次必须仍可正常访问（存在 ALLOW 证据且无反例）。
    R5 注销提交并复制到某节点后，该节点不得再放行该会话任何代次。

失败记录关联：规则号、场景、决策序号、提交确认（write_ack）、
读副本版本（replica_version）、会话代次（generation）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .model import Decision, Op, State, Verdict
from .scenarios import Trace

RULE_DESCRIPTIONS = {
    "R1": "新代次提交并复制后，旧代次必须及时失效",
    "R2": "同一操作重试必须幂等，不得重复换发新代次",
    "R3": "缺失写入确认/未复制时，副本必须安全拒绝或返回待确认",
    "R4": "通过校验的新会话代次必须仍可正常访问",
    "R5": "注销提交并复制后，任何代次都不得再被放行",
}

ALLOW = Verdict.ALLOW.value
COMMITTED = Verdict.COMMITTED.value


@dataclass
class Failure:
    rule_id: str
    scenario: str
    decision_order: int
    reason: str
    write_ack: Optional[bool]
    replica_version: Optional[int]
    generation: Optional[int]


@dataclass
class RuleResult:
    rule_id: str
    title: str
    status: str  # PASS / FAIL / INSUFFICIENT_EVIDENCE
    checked: int
    failures: List[Failure] = field(default_factory=list)


def _reads(traces: List[Trace]) -> List[Tuple[Trace, Decision]]:
    return [(t, d) for t in traces for d in t.decisions if d.action == "READ"]


def _commits(traces: List[Trace]) -> List[Tuple[Trace, Decision]]:
    return [(t, d) for t in traces for d in t.decisions if d.action.startswith("COMMIT")]


def _check_r1(traces: List[Trace]) -> RuleResult:
    failures, checked = [], 0
    for t, d in _reads(traces):
        if d.verdict != ALLOW:
            continue
        checked += 1
        for e in t.primary.log:
            if (
                e.session_id == d.session_id
                and e.generation > (d.generation or 0)
                and e.seq <= (d.replica_version or 0)
            ):
                failures.append(Failure(
                    "R1", t.scenario, d.order,
                    "旧代次 %s 在新代次 %s(seq=%s) 已复制到版本 %s 后仍被放行"
                    % (d.generation, e.generation, e.seq, d.replica_version),
                    e.ack_delivered, d.replica_version, d.generation,
                ))
                break
    return RuleResult("R1", RULE_DESCRIPTIONS["R1"],
                      "FAIL" if failures else "PASS", checked, failures)


def _check_r2(traces: List[Trace]) -> RuleResult:
    failures = []
    by_op = {}
    for t, d in _commits(traces):
        if d.op_id:
            by_op.setdefault((t.scenario, d.session_id, d.op_id), []).append((t, d))
    checked = len(by_op)
    for (scenario, sid, op_id), items in by_op.items():
        committed = [d for _, d in items if d.verdict == COMMITTED]
        if len(committed) > 1:
            d = committed[-1]
            failures.append(Failure(
                "R2", scenario, d.order,
                "op_id=%s 被重复换发（COMMITTED 出现 %d 次）" % (op_id, len(committed)),
                d.write_ack, d.replica_version, d.generation,
            ))
        gens = {d.generation for _, d in items if d.generation is not None}
        if len(gens) > 1:
            d = items[-1][1]
            failures.append(Failure(
                "R2", scenario, d.order,
                "op_id=%s 的重试返回了不同代次: %s" % (op_id, sorted(gens)),
                d.write_ack, d.replica_version, d.generation,
            ))
    # 防御性检查：主节点日志本身不得出现同 op_id 两条记录
    for t in traces:
        seen = set()
        for e in t.primary.log:
            key = (e.session_id, e.op_id)
            if key in seen:
                failures.append(Failure(
                    "R2", t.scenario, 0,
                    "主节点日志中 op_id=%s 出现重复提交记录" % e.op_id,
                    e.ack_delivered, e.seq, e.generation,
                ))
            seen.add(key)
    return RuleResult("R2", RULE_DESCRIPTIONS["R2"],
                      "FAIL" if failures else "PASS", checked, failures)


def _check_r3(traces: List[Trace]) -> RuleResult:
    failures, evidence = [], 0
    for t, d in _reads(traces):
        entry = next(
            (e for e in t.primary.log
             if e.session_id == d.session_id and e.generation == d.generation),
            None,
        )
        if entry is None:
            if d.verdict == ALLOW:
                failures.append(Failure(
                    "R3", t.scenario, d.order,
                    "副本放行了主节点不存在的代次 %s" % d.generation,
                    d.write_ack, d.replica_version, d.generation,
                ))
            continue
        if not entry.ack_delivered:
            evidence += 1  # 涉及缺失写入确认的读取证据
        if d.verdict == ALLOW and entry.seq > (d.replica_version or 0):
            failures.append(Failure(
                "R3", t.scenario, d.order,
                "副本版本 %s 未复制到 seq=%s 的代次 %s 却放行"
                % (d.replica_version, entry.seq, d.generation),
                entry.ack_delivered, d.replica_version, d.generation,
            ))
    if failures:
        status = "FAIL"
    elif evidence >= 1:
        status = "PASS"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    return RuleResult("R3", RULE_DESCRIPTIONS["R3"], status, evidence, failures)


def _check_r4(traces: List[Trace]) -> RuleResult:
    failures, checked = [], 0
    for t in traces:
        for sid, rec in t.primary.sessions.items():
            if rec.state is State.LOGGED_OUT or rec.generation < 1:
                continue
            entry = next(
                (e for e in reversed(t.primary.log)
                 if e.session_id == sid and e.generation == rec.generation),
                None,
            )
            if entry is None:
                continue
            evidence = [
                d for d in t.decisions
                if d.action == "READ" and d.session_id == sid
                and d.generation == rec.generation
                and (d.replica_version or 0) >= entry.seq
            ]
            for d in evidence:
                checked += 1
                if d.verdict != ALLOW:
                    failures.append(Failure(
                        "R4", t.scenario, d.order,
                        "会话 %s 当前代次 %s 在版本 %s 节点上被拒绝(%s)"
                        % (sid, rec.generation, d.replica_version, d.verdict),
                        entry.ack_delivered, d.replica_version, d.generation,
                    ))
            if not any(d.verdict == ALLOW for d in evidence):
                failures.append(Failure(
                    "R4", t.scenario, 0,
                    "会话 %s 新代次 %s 缺少可正常访问的证据" % (sid, rec.generation),
                    entry.ack_delivered, None, rec.generation,
                ))
    if failures:
        status = "FAIL"
    elif checked >= 1:
        status = "PASS"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    return RuleResult("R4", RULE_DESCRIPTIONS["R4"], status, checked, failures)


def _check_r5(traces: List[Trace]) -> RuleResult:
    failures, checked = [], 0
    for t, d in _reads(traces):
        if d.verdict != ALLOW:
            continue
        checked += 1
        for e in t.primary.log:
            if (
                e.session_id == d.session_id
                and e.op is Op.LOGOUT
                and e.seq <= (d.replica_version or 0)
            ):
                failures.append(Failure(
                    "R5", t.scenario, d.order,
                    "注销(seq=%s)已复制到版本 %s 后仍放行代次 %s"
                    % (e.seq, d.replica_version, d.generation),
                    e.ack_delivered, d.replica_version, d.generation,
                ))
                break
    return RuleResult("R5", RULE_DESCRIPTIONS["R5"],
                      "FAIL" if failures else "PASS", checked, failures)


def evaluate(traces: List[Trace]) -> List[RuleResult]:
    return [
        _check_r1(traces),
        _check_r2(traces),
        _check_r3(traces),
        _check_r4(traces),
        _check_r5(traces),
    ]
