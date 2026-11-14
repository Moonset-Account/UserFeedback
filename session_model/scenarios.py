"""确定性场景：正常登录、不同代次并发读取、写入中断后重试、注销事件迟到。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .model import Decision, Op, Primary, Replica, Verdict
from .scheduler import Scheduler


@dataclass
class Trace:
    scenario: str
    decisions: List[Decision]
    primary: Primary
    replicas: Dict[str, Replica]


def _begin(sched: Scheduler, scenario: str, primary: Primary, sid: str, identity: str) -> None:
    primary.begin_session(sid, identity)
    sched.record(
        scenario=scenario, action="BEGIN", node=primary.node_id, session_id=sid,
        op_id=None, generation=0, write_ack=None, replica_version=primary.seq,
        verdict="OK", note="匿名会话建立",
    )


def _commit(
    sched: Scheduler,
    scenario: str,
    primary: Primary,
    sid: str,
    op: Op,
    op_id: str,
    note: str = "",
    deliver_ack: bool = True,
    apply: bool = True,
):
    res = primary.commit(sid, op, op_id, deliver_ack=deliver_ack, apply=apply)
    if res.entry is not None:
        verdict = Verdict.COMMIT_IDEMPOTENT_REPLAY if res.replay else Verdict.COMMITTED
        generation: Optional[int] = res.entry.generation
        ack: Optional[bool] = res.ack
    elif res.error == "invalid_transition":
        verdict, generation, ack = Verdict.REJECT_INVALID_TRANSITION, None, False
    else:
        verdict, generation, ack = Verdict.COMMIT_INTERRUPTED, None, False
    sched.record(
        scenario=scenario, action="COMMIT:" + op.value, node=primary.node_id,
        session_id=sid, op_id=op_id, generation=generation, write_ack=ack,
        replica_version=primary.seq, verdict=verdict.value, note=note,
    )
    return res


def _replicate(
    sched: Scheduler,
    scenario: str,
    primary: Primary,
    replica: Replica,
    upto: Optional[int] = None,
    note: str = "",
) -> None:
    target = primary.seq if upto is None else upto
    replica.apply_up_to(primary, target)
    sched.record(
        scenario=scenario, action="REPLICATE", node=replica.replica_id, session_id="*",
        op_id=None, generation=None, write_ack=None, replica_version=replica.version,
        verdict="OK", note=note or "复制到 seq=%d" % replica.version,
    )


def _read(sched: Scheduler, scenario: str, node, node_name: str, sid: str, generation: int, note: str = ""):
    verdict = node.read(sid, generation)
    version = node.version if isinstance(node, Replica) else node.seq
    sched.record(
        scenario=scenario, action="READ", node=node_name, session_id=sid,
        op_id=None, generation=generation, write_ack=None, replica_version=version,
        verdict=verdict.value, note=note,
    )
    return verdict


def scenario_normal_login() -> Trace:
    """正常登录：匿名 -> 已登录，含非法转换与边界读取。"""
    name = "normal_login"
    p, r1, sched = Primary(), Replica("replica-1"), Scheduler()
    sid = "s-login"

    sched.schedule(1, lambda s: _begin(s, name, p, sid, "alice"))
    sched.schedule(2, lambda s: _commit(s, name, p, sid, Op.ELEVATE, "op-elevate-early",
                                        note="boundary:匿名状态提权应被拒绝"))
    sched.schedule(3, lambda s: _commit(s, name, p, sid, Op.LOGIN, "op-login-1",
                                        note="匿名会话转为已登录会话"))
    sched.schedule(4, lambda s: _replicate(s, name, p, r1))
    sched.schedule(5, lambda s: _read(s, name, r1, "replica-1", sid, 1, note="新代次应可访问"))
    sched.schedule(6, lambda s: _read(s, name, r1, "replica-1", sid, 0, note="boundary:匿名代次"))
    sched.schedule(7, lambda s: _read(s, name, r1, "replica-1", sid, 2, note="boundary:未来代次"))
    sched.schedule(8, lambda s: _read(s, name, r1, "replica-1", "ghost", 1, note="boundary:未知会话"))
    sched.run()
    return Trace(name, sched.decisions, p, {"replica-1": r1})


def scenario_concurrent_reads() -> Trace:
    """不同代次请求的并发读取：新副本与滞后副本同时受理。"""
    name = "concurrent_reads"
    p, ra, rb, sched = Primary(), Replica("replica-a"), Replica("replica-b"), Scheduler()
    sid = "s-conc"

    sched.schedule(1, lambda s: _begin(s, name, p, sid, "bob"))
    sched.schedule(2, lambda s: _commit(s, name, p, sid, Op.LOGIN, "op-c-login", note="登录 gen1"))
    sched.schedule(3, lambda s: _commit(s, name, p, sid, Op.ELEVATE, "op-c-elevate",
                                        note="同一身份提升权限 gen2"))
    sched.schedule(4, lambda s: _replicate(s, name, p, ra, note="replica-a 复制到最新"))
    sched.schedule(5, lambda s: _replicate(s, name, p, rb, upto=1, note="replica-b 滞后，仅到 seq1"))
    # 同一逻辑时刻的并发读取，用优先级固定次序保证确定性
    sched.schedule(6, lambda s: _read(s, name, ra, "replica-a", sid, 2, note="最新副本读新代次"), priority=0)
    sched.schedule(6, lambda s: _read(s, name, ra, "replica-a", sid, 1, note="最新副本读旧代次"), priority=1)
    sched.schedule(6, lambda s: _read(s, name, rb, "replica-b", sid, 2, note="滞后副本读新代次"), priority=2)
    sched.schedule(6, lambda s: _read(s, name, rb, "replica-b", sid, 1,
                                      note="滞后副本读旧代次（信息性：副本尚未收到新代次）"), priority=3)
    sched.run()
    return Trace(name, sched.decisions, p, {"replica-a": ra, "replica-b": rb})


def scenario_retry_after_interruption() -> Trace:
    """写入中断后重试：确认丢失与应用前中断两种情形。"""
    name = "retry_after_interruption"
    p, r1, sched = Primary(), Replica("replica-1"), Scheduler()
    sid = "s-retry"

    sched.schedule(1, lambda s: _begin(s, name, p, sid, "carol"))
    sched.schedule(2, lambda s: _commit(s, name, p, sid, Op.LOGIN, "op-r-login",
                                        deliver_ack=False, note="写入已应用但确认丢失"))
    sched.schedule(3, lambda s: _read(s, name, r1, "replica-1", sid, 1,
                                      note="确认缺失且未复制：副本应安全拒绝"))
    sched.schedule(4, lambda s: _commit(s, name, p, sid, Op.LOGIN, "op-r-login",
                                        note="同一 op_id 幂等重试，不得重复换发"))
    sched.schedule(5, lambda s: _commit(s, name, p, sid, Op.ELEVATE, "op-r-elevate",
                                        apply=False, note="写入在应用前中断（未提交）"))
    sched.schedule(6, lambda s: _commit(s, name, p, sid, Op.ELEVATE, "op-r-elevate",
                                        note="中断后重试，本次正常提交"))
    sched.schedule(7, lambda s: _replicate(s, name, p, r1))
    sched.schedule(8, lambda s: _read(s, name, r1, "replica-1", sid, 2, note="新代次应可访问"))
    sched.schedule(9, lambda s: _read(s, name, r1, "replica-1", sid, 1, note="旧代次应已失效"))
    sched.run()
    return Trace(name, sched.decisions, p, {"replica-1": r1})


def scenario_late_logout() -> Trace:
    """注销事件迟到：注销提交后，滞后副本在新事件复制前仍可能放行旧代次。"""
    name = "late_logout"
    p, ra, rb, sched = Primary(), Replica("replica-a"), Replica("replica-b"), Scheduler()
    sid = "s-late"

    sched.schedule(1, lambda s: _begin(s, name, p, sid, "dave"))
    sched.schedule(2, lambda s: _commit(s, name, p, sid, Op.LOGIN, "op-l-login", note="登录 gen1"))
    sched.schedule(3, lambda s: _replicate(s, name, p, ra, note="replica-a 复制到 seq1"))
    sched.schedule(4, lambda s: _replicate(s, name, p, rb, note="replica-b 复制到 seq1"))
    sched.schedule(5, lambda s: _read(s, name, rb, "replica-b", sid, 1, note="注销前基线读取"))
    sched.schedule(6, lambda s: _commit(s, name, p, sid, Op.LOGOUT, "op-l-logout",
                                        note="主动退出，主节点立即失效"))
    sched.schedule(7, lambda s: _read(s, name, p, "primary", sid, 1, note="主节点注销后立即拒绝"))
    sched.schedule(8, lambda s: _replicate(s, name, p, ra, note="注销事件复制到 replica-a"))
    sched.schedule(9, lambda s: _read(s, name, ra, "replica-a", sid, 1, note="已复制注销的副本拒绝"))
    sched.schedule(10, lambda s: _read(s, name, rb, "replica-b", sid, 1,
                                       note="注销事件迟到：replica-b 尚未复制（信息性放行）"))
    sched.schedule(11, lambda s: _replicate(s, name, p, rb, note="注销事件最终复制到 replica-b"))
    sched.schedule(12, lambda s: _read(s, name, rb, "replica-b", sid, 1, note="复制后旧代次拒绝"))
    sched.schedule(13, lambda s: _read(s, name, rb, "replica-b", sid, 2, note="注销代次本身也拒绝"))
    sched.run()
    return Trace(name, sched.decisions, p, {"replica-a": ra, "replica-b": rb})


SCENARIOS = {
    "normal_login": scenario_normal_login,
    "concurrent_reads": scenario_concurrent_reads,
    "retry_after_interruption": scenario_retry_after_interruption,
    "late_logout": scenario_late_logout,
}


def run_all() -> List[Trace]:
    return [fn() for fn in SCENARIOS.values()]
