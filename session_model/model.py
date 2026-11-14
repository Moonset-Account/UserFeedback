"""会话代次参考模型：状态机、提交规则、内存主节点与读副本。

状态转换（每次成功提交都会使会话代次 +1）：
    LOGIN   : ANONYMOUS     -> AUTHENTICATED   匿名会话转为已登录会话
    ELEVATE : AUTHENTICATED -> ELEVATED        同一身份提升权限
    LOGOUT  : AUTHENTICATED -> LOGGED_OUT      主动退出（终态）
    LOGOUT  : ELEVATED      -> LOGGED_OUT      主动退出（终态）

提交规则：
    1. 只有主节点应用（apply）成功的转换才分配逻辑序号 seq 与新代次。
    2. 写入确认（ack）丢失不代表未提交：以主节点日志为准。
    3. 同一 (session_id, op_id) 的重试必须幂等：返回首次提交结果，
       不得重复换发新代次。
    4. 读副本只能按 seq 顺序应用日志；副本对自身尚未复制到的代次
       必须安全拒绝或返回待确认（PENDING_REPLICATION），不得放行。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


class State(enum.Enum):
    ANONYMOUS = "ANONYMOUS"
    AUTHENTICATED = "AUTHENTICATED"
    ELEVATED = "ELEVATED"
    LOGGED_OUT = "LOGGED_OUT"


class Op(enum.Enum):
    LOGIN = "LOGIN"
    ELEVATE = "ELEVATE"
    LOGOUT = "LOGOUT"


TRANSITIONS: Dict[Tuple[Op, State], State] = {
    (Op.LOGIN, State.ANONYMOUS): State.AUTHENTICATED,
    (Op.ELEVATE, State.AUTHENTICATED): State.ELEVATED,
    (Op.LOGOUT, State.AUTHENTICATED): State.LOGGED_OUT,
    (Op.LOGOUT, State.ELEVATED): State.LOGGED_OUT,
}


class Verdict(enum.Enum):
    # 读路径判定
    ALLOW = "ALLOW"
    DENY_STALE_GENERATION = "DENY_STALE_GENERATION"
    DENY_LOGGED_OUT = "DENY_LOGGED_OUT"
    DENY_NOT_AUTHENTICATED = "DENY_NOT_AUTHENTICATED"
    DENY_UNKNOWN_SESSION = "DENY_UNKNOWN_SESSION"
    DENY_UNKNOWN_GENERATION = "DENY_UNKNOWN_GENERATION"
    PENDING_REPLICATION = "PENDING_REPLICATION"
    # 写路径判定
    COMMITTED = "COMMITTED"
    COMMIT_IDEMPOTENT_REPLAY = "COMMIT_IDEMPOTENT_REPLAY"
    COMMIT_INTERRUPTED = "COMMIT_INTERRUPTED"
    REJECT_INVALID_TRANSITION = "REJECT_INVALID_TRANSITION"


@dataclass
class LogEntry:
    """主节点提交日志：逻辑顺序 seq、会话代次、写入确认是否送达。"""

    seq: int
    session_id: str
    op_id: str
    op: Op
    from_state: State
    to_state: State
    generation: int
    ack_delivered: bool


@dataclass
class SessionRecord:
    session_id: str
    identity: Optional[str]
    state: State
    generation: int


@dataclass
class CommitResult:
    entry: Optional[LogEntry]
    ack: bool
    replay: bool
    error: Optional[str] = None


@dataclass
class Decision:
    """决策记录：逻辑顺序、会话代次、写入确认、读副本版本、访问判定。"""

    order: int
    scenario: str
    action: str
    node: str
    session_id: str
    op_id: Optional[str]
    generation: Optional[int]
    write_ack: Optional[bool]
    replica_version: Optional[int]
    verdict: str
    note: str = ""


class Primary:
    """内存主节点：唯一可提交转换的节点。"""

    def __init__(self, node_id: str = "primary"):
        self.node_id = node_id
        self.seq = 0
        self.sessions: Dict[str, SessionRecord] = {}
        self.log: List[LogEntry] = []
        self.op_index: Dict[Tuple[str, str], LogEntry] = {}

    def begin_session(self, session_id: str, identity: Optional[str] = None) -> SessionRecord:
        rec = SessionRecord(session_id, identity, State.ANONYMOUS, 0)
        self.sessions[session_id] = rec
        return rec

    def commit(
        self,
        session_id: str,
        op: Op,
        op_id: str,
        deliver_ack: bool = True,
        apply: bool = True,
    ) -> CommitResult:
        """提交一次状态转换。

        deliver_ack=False 模拟“写入已应用但确认丢失”；
        apply=False 模拟“写入在应用前中断”（未提交）。
        """
        key = (session_id, op_id)
        existing = self.op_index.get(key)
        if existing is not None:
            # 幂等重试：返回首次提交结果，不重复换发代次
            return CommitResult(entry=existing, ack=deliver_ack, replay=True)

        rec = self.sessions.get(session_id)
        if rec is None:
            return CommitResult(None, False, False, error="unknown_session")
        target = TRANSITIONS.get((op, rec.state))
        if target is None:
            return CommitResult(None, False, False, error="invalid_transition")
        if not apply:
            return CommitResult(None, False, False, error="interrupted_before_apply")

        self.seq += 1
        entry = LogEntry(
            seq=self.seq,
            session_id=session_id,
            op_id=op_id,
            op=op,
            from_state=rec.state,
            to_state=target,
            generation=rec.generation + 1,
            ack_delivered=deliver_ack,
        )
        rec.state = target
        rec.generation += 1
        self.log.append(entry)
        self.op_index[key] = entry
        return CommitResult(entry=entry, ack=deliver_ack, replay=False)

    def read(self, session_id: str, generation: int) -> Verdict:
        rec = self.sessions.get(session_id)
        if rec is None:
            return Verdict.DENY_UNKNOWN_SESSION
        if rec.state is State.LOGGED_OUT:
            return Verdict.DENY_LOGGED_OUT
        if rec.state is State.ANONYMOUS or generation < 1:
            return Verdict.DENY_NOT_AUTHENTICATED
        if generation < rec.generation:
            return Verdict.DENY_STALE_GENERATION
        if generation > rec.generation:
            return Verdict.DENY_UNKNOWN_GENERATION
        return Verdict.ALLOW


class Replica:
    """内存读副本：按 seq 顺序应用主节点日志，基于本地视图做访问判定。"""

    def __init__(self, replica_id: str):
        self.replica_id = replica_id
        self.version = 0
        self.view: Dict[str, Tuple[State, int]] = {}

    def apply_up_to(self, primary: Primary, target_seq: int) -> int:
        for entry in primary.log:
            if self.version < entry.seq <= target_seq:
                self.view[entry.session_id] = (entry.to_state, entry.generation)
                self.version = entry.seq
        return self.version

    def read(self, session_id: str, generation: int) -> Verdict:
        view = self.view.get(session_id)
        if view is None:
            # 安全拒绝：副本未见过该会话
            return Verdict.DENY_UNKNOWN_SESSION
        state, current_gen = view
        if state is State.LOGGED_OUT:
            return Verdict.DENY_LOGGED_OUT
        if state is State.ANONYMOUS or generation < 1:
            return Verdict.DENY_NOT_AUTHENTICATED
        if generation < current_gen:
            return Verdict.DENY_STALE_GENERATION
        if generation > current_gen:
            # 副本滞后：返回待确认，不放行
            return Verdict.PENDING_REPLICATION
        return Verdict.ALLOW
