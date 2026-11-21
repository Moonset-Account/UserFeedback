"""按轮推进的推理调度器。

核心资源模型
============

* 每个调度轮（round）拥有 ``slot_budget``（B）个 Token 算力槽。
* KV 缓存总容量为 ``cache_capacity``（Token 数），与算力槽相互独立。
* 会话在接纳时按 ``提示词长度 + 最大输出长度`` 一次性预留缓存，
  会话结束（停止符 / 输出上限 / 取消）的当轮轮末才释放预留。

每轮的固定处理顺序
==================

1. 接纳（admission）：按入队序扫描等待队列，缓存与并发会话数都满足才接纳；
   队首暂时放不下则跳过（保留入队次序，后续请求继续尝试）；
   单条需求超过总缓存容量则直接拒绝。
2. 解码（decode）：先给每个已完成提示词处理的会话各排一个解码步，每步一个槽，
   Token 由调用方通过 token_provider 注入。
3. 提示词处理（prefill）：剩余算力槽按入队序分给尚未处理完提示词的会话，
   每槽处理一个提示词 Token；本轮刚处理完提示词的会话转为解码态，但下一轮才出词。
4. 轮末收尾（finalize）：停止符 / 到输出上限 / 被取消的会话统一在轮末释放缓存预留，
   腾出的空间在下一轮接纳时可用。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, List, Mapping, Optional, Sequence, Union

__all__ = [
    "SessionState",
    "FinishReason",
    "RejectReason",
    "SessionInfo",
    "DecodeWork",
    "PromptWork",
    "FinishedRequest",
    "RejectedRequest",
    "RoundResult",
    "TokenProvider",
    "RoundScheduler",
]

TokenProvider = Callable[[str, int], str]
"""Token 注入回调：参数为 (request_id, round_index)，返回本解码步生成的 Token。"""


class SessionState(str, Enum):
    """会话生命周期状态。"""

    WAITING = "waiting"      # 已入队，等待接纳
    PREFILL = "prefill"      # 已接纳，提示词尚未处理完
    DECODING = "decoding"    # 提示词已处理完，正在逐 Token 解码
    FINISHED = "finished"    # 终止（停止符 / 上限 / 取消 / 拒绝）


class FinishReason(str, Enum):
    """活跃会话的终止原因。"""

    STOP = "stop"            # 命中停止符
    LENGTH = "length"        # 达到最大输出长度
    CANCELLED = "cancelled"  # 被调用方取消


class RejectReason(str, Enum):
    """接纳阶段的拒绝原因。"""

    EXCEEDS_CAPACITY = "exceeds_capacity"  # 单条预留需求超过缓存总容量


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True)
class SessionInfo:
    """会话的只读快照。"""

    request_id: str
    state: SessionState
    prompt_len: int
    prompt_processed: int
    max_new_tokens: int
    output_len: int
    output_tokens: tuple
    reserved_tokens: int
    finish_reason: Optional[FinishReason]
    enqueue_seq: int


@dataclass(frozen=True)
class DecodeWork:
    """一轮中某个会话实际出词的一个解码步。"""

    request_id: str
    token: str
    output_index: int                  # 该 Token 在输出序列中的下标（0 起）
    finish_reason: Optional[FinishReason]  # 本步是否终止会话及其原因


@dataclass(frozen=True)
class PromptWork:
    """一轮中某个会话被处理的一段提示词。"""

    request_id: str
    prompt_start: int                  # 段起始偏移（含）
    prompt_end: int                    # 段结束偏移（不含）
    tokens_processed: int
    completed: bool                    # 本段处理完后提示词是否全部处理完


@dataclass(frozen=True)
class FinishedRequest:
    """轮末收尾中终止并释放预留的会话。"""

    request_id: str
    reason: FinishReason
    prompt_len: int
    output_len: int
    output_tokens: tuple


@dataclass(frozen=True)
class RejectedRequest:
    """接纳阶段被拒绝的入队请求。"""

    request_id: str
    reason: RejectReason
    required_tokens: int
    cache_capacity: int


@dataclass(frozen=True)
class RoundResult:
    """一个调度轮的完整可见结果。"""

    round_index: int                   # 轮次下标，0 起
    round_number: int                  # 轮次序号，1 起，便于调用方展示
    decode_work: List[DecodeWork] = field(default_factory=list)
    prompt_work: List[PromptWork] = field(default_factory=list)
    finished: List[FinishedRequest] = field(default_factory=list)
    rejected: List[RejectedRequest] = field(default_factory=list)
    active_session_ids: List[str] = field(default_factory=list)
    waiting_session_ids: List[str] = field(default_factory=list)
    total_slots: int = 0
    decode_slots_used: int = 0
    prefill_slots_used: int = 0
    unused_slots: int = 0
    cache_capacity: int = 0
    cache_reserved_start: int = 0      # 本轮开始时已预留缓存
    cache_reserved_end: int = 0        # 轮末释放后仍占用的缓存


class _Session:
    """会话内部可变状态。"""

    __slots__ = (
        "request_id",
        "prompt_len",
        "max_new_tokens",
        "reserved_tokens",
        "state",
        "prompt_processed",
        "output_tokens",
        "cancel_requested",
        "finish_reason",
        "enqueue_seq",
    )

    def __init__(self, request_id: str, prompt_len: int,
                 max_new_tokens: int, enqueue_seq: int) -> None:
        self.request_id = request_id
        self.prompt_len = prompt_len
        self.max_new_tokens = max_new_tokens
        self.reserved_tokens = prompt_len + max_new_tokens
        self.state: SessionState = SessionState.WAITING
        self.prompt_processed = 0
        self.output_tokens: List[str] = []
        self.cancel_requested = False
        self.finish_reason: Optional[FinishReason] = None
        self.enqueue_seq = enqueue_seq

    def snapshot(self) -> SessionInfo:
        return SessionInfo(
            request_id=self.request_id,
            state=self.state,
            prompt_len=self.prompt_len,
            prompt_processed=self.prompt_processed,
            max_new_tokens=self.max_new_tokens,
            output_len=len(self.output_tokens),
            output_tokens=tuple(self.output_tokens),
            reserved_tokens=self.reserved_tokens,
            finish_reason=self.finish_reason,
            enqueue_seq=self.enqueue_seq,
        )


class RoundScheduler:
    """按轮推进的推理调度器。

    参数
    ----
    slot_budget:
        每轮的 Token 算力槽数 B，同时也是并发会话数上限（活跃会话不会超过 B）。
    cache_capacity:
        KV 缓存总容量（Token 数），会话按 ``prompt_len + max_new_tokens`` 预留。
    stop_tokens:
        停止符集合；解码注入的 Token 命中其中任意一个即终止，原因记为 STOP。
    """

    def __init__(
        self,
        slot_budget: int,
        cache_capacity: int,
        stop_tokens: Optional[Sequence[str]] = None,
    ) -> None:
        if not _is_positive_int(slot_budget):
            raise ValueError("slot_budget 必须是正整数")
        if not _is_positive_int(cache_capacity):
            raise ValueError("cache_capacity 必须是正整数")
        self._slot_budget = slot_budget
        self._cache_capacity = cache_capacity
        self._stop_tokens = frozenset(stop_tokens) if stop_tokens else frozenset()

        self._lock = threading.RLock()
        self._waiting: Deque[str] = deque()
        self._waiting_sessions: Dict[str, _Session] = {}
        self._active: Dict[str, _Session] = {}
        self._terminated: Dict[str, _Session] = {}
        self._cache_reserved = 0
        self._next_seq = 0
        self._round = 0

    # ------------------------------------------------------------------ #
    # 基本属性
    # ------------------------------------------------------------------ #

    @property
    def slot_budget(self) -> int:
        return self._slot_budget

    @property
    def cache_capacity(self) -> int:
        return self._cache_capacity

    @property
    def round_index(self) -> int:
        """已执行完成的轮数（下一轮的 0 基下标）。"""
        return self._round

    @property
    def cache_reserved(self) -> int:
        """当前已预留的缓存 Token 数。"""
        return self._cache_reserved

    @property
    def active_session_ids(self) -> List[str]:
        with self._lock:
            return list(self._active.keys())

    @property
    def waiting_session_ids(self) -> List[str]:
        with self._lock:
            return list(self._waiting)

    def has_pending_work(self) -> bool:
        """是否还有等待或活跃的会话。"""
        with self._lock:
            return bool(self._waiting) or bool(self._active)

    def get_session(self, request_id: str) -> SessionInfo:
        """获取任意会话（含已终止）的只读快照；不存在则抛 KeyError。"""
        with self._lock:
            session = self._find_session(request_id)
            return session.snapshot()

    # ------------------------------------------------------------------ #
    # 入队 / 取消
    # ------------------------------------------------------------------ #

    def add_request(self, request_id: str, prompt_len: int,
                    max_new_tokens: int) -> SessionInfo:
        """提交一个推理请求并入队，等待后续轮次接纳。

        超出缓存总容量的请求不会在这里报错，而是在下一轮接纳扫描到时被拒绝，
        拒绝信息通过 :class:`RoundResult` 的 ``rejected`` 字段返回。
        """
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id 必须是非空字符串")
        if not _is_positive_int(prompt_len):
            raise ValueError("prompt_len 必须是正整数")
        if not _is_positive_int(max_new_tokens):
            raise ValueError("max_new_tokens 必须是正整数")
        with self._lock:
            if request_id in self._waiting or request_id in self._active \
                    or request_id in self._terminated:
                raise ValueError(f"request_id 已存在: {request_id!r}")
            session = _Session(
                request_id=request_id,
                prompt_len=prompt_len,
                max_new_tokens=max_new_tokens,
                enqueue_seq=self._next_seq,
            )
            self._next_seq += 1
            self._waiting.append(request_id)
            self._waiting_sessions[request_id] = session
            return session.snapshot()

    def cancel_request(self, request_id: str) -> bool:
        """请求取消会话。

        * 等待中的会话：下一轮接纳扫描到时移除并以 CANCELLED 上报，不占缓存。
        * 活跃会话：标记取消，下一轮不会被排解码 / 提示词处理，
          并在该轮轮末释放预留、以 CANCELLED 上报。
        * 已终止或未知的 request_id：返回 False。
        """
        with self._lock:
            try:
                session = self._find_session(request_id)
            except KeyError:
                return False
            if session.state == SessionState.FINISHED:
                return False
            session.cancel_requested = True
            return True

    # ------------------------------------------------------------------ #
    # 调度轮
    # ------------------------------------------------------------------ #

    def step(self, token_provider: Union[TokenProvider, Mapping[str, str], None] = None
             ) -> RoundResult:
        """推进一个调度轮并返回本轮的完整可见结果。

        ``token_provider`` 可以是 ``(request_id, round_index) -> token`` 回调，
        也可以是 ``request_id -> token`` 的映射；仅当本轮存在解码会话时才会被调用。
        """
        with self._lock:
            provider = self._normalize_provider(token_provider)
            self._round += 1
            round_index = self._round - 1
            cache_start = self._cache_reserved

            decode_work: List[DecodeWork] = []
            prompt_work: List[PromptWork] = []
            finished: List[FinishedRequest] = []
            rejected: List[RejectedRequest] = []

            # 活跃会话的取消标记在本轮开始时快照，轮内统一按快照处理。
            cancelled_active = {
                sid for sid, s in self._active.items() if s.cancel_requested
            }

            # 1) 接纳阶段 ------------------------------------------------
            self._admit(rejected, finished)

            # 2) 解码阶段：每个解码态会话一个槽 ---------------------------
            slots_left = self._slot_budget
            decode_finished: List[str] = []
            for session in list(self._active.values()):
                if session.state != SessionState.DECODING:
                    continue
                if session.request_id in cancelled_active:
                    continue
                if provider is None:
                    raise ValueError(
                        "本轮存在解码会话但未提供 token_provider"
                    )
                slots_left -= 1
                token = provider(session.request_id, round_index)
                session.output_tokens.append(token)
                output_index = len(session.output_tokens) - 1

                reason: Optional[FinishReason] = None
                if token in self._stop_tokens:
                    reason = FinishReason.STOP
                elif len(session.output_tokens) >= session.max_new_tokens:
                    reason = FinishReason.LENGTH
                decode_work.append(
                    DecodeWork(
                        request_id=session.request_id,
                        token=token,
                        output_index=output_index,
                        finish_reason=reason,
                    )
                )
                if reason is not None:
                    session.finish_reason = reason
                    decode_finished.append(session.request_id)

            decode_slots_used = self._slot_budget - slots_left

            # 3) 提示词处理阶段：剩余槽按入队序分配 ------------------------
            for session in list(self._active.values()):
                if slots_left == 0:
                    break
                if session.state != SessionState.PREFILL:
                    continue
                if session.request_id in cancelled_active:
                    continue
                remaining_prompt = session.prompt_len - session.prompt_processed
                if remaining_prompt <= 0:
                    continue
                take = min(remaining_prompt, slots_left)
                start = session.prompt_processed
                end = start + take
                session.prompt_processed = end
                slots_left -= take
                completed = session.prompt_processed >= session.prompt_len
                prompt_work.append(
                    PromptWork(
                        request_id=session.request_id,
                        prompt_start=start,
                        prompt_end=end,
                        tokens_processed=take,
                        completed=completed,
                    )
                )
                # 本轮刚处理完提示词：转入解码态，但下一轮才会被排解码步。
                if completed:
                    session.state = SessionState.DECODING

            prefill_slots_used = self._slot_budget - decode_slots_used - slots_left

            # 4) 轮末收尾：统一释放预留 -----------------------------------
            for request_id in decode_finished:
                session = self._active.get(request_id)
                if session is None:
                    continue
                self._release(session)
                finished.append(self._make_finished(session))

            for request_id in list(self._active.keys()):
                session = self._active[request_id]
                if request_id in cancelled_active:
                    session.finish_reason = FinishReason.CANCELLED
                    self._release(session)
                    finished.append(self._make_finished(session))

            finished.sort(key=lambda item: self._terminated[item.request_id].enqueue_seq)

            return RoundResult(
                round_index=round_index,
                round_number=round_index + 1,
                decode_work=decode_work,
                prompt_work=prompt_work,
                finished=finished,
                rejected=rejected,
                active_session_ids=list(self._active.keys()),
                waiting_session_ids=list(self._waiting),
                total_slots=self._slot_budget,
                decode_slots_used=decode_slots_used,
                prefill_slots_used=prefill_slots_used,
                unused_slots=slots_left,
                cache_capacity=self._cache_capacity,
                cache_reserved_start=cache_start,
                cache_reserved_end=self._cache_reserved,
            )

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _find_session(self, request_id: str) -> _Session:
        if request_id in self._waiting_sessions:
            return self._waiting_sessions[request_id]
        if request_id in self._active:
            return self._active[request_id]
        if request_id in self._terminated:
            return self._terminated[request_id]
        raise KeyError(request_id)

    def _admit(self, rejected: List[RejectedRequest],
               finished: List[FinishedRequest]) -> None:
        """按入队序扫描等待队列并尝试接纳。"""
        waiting_map = self._waiting_sessions
        queued_ids = list(self._waiting)
        self._waiting.clear()

        for request_id in queued_ids:
            session = waiting_map.pop(request_id)

            # 等待期间被取消：不占缓存，直接终止并上报。
            if session.cancel_requested:
                session.state = SessionState.FINISHED
                session.finish_reason = FinishReason.CANCELLED
                self._terminated[request_id] = session
                finished.append(self._make_finished(session))
                continue

            # 单条需求超过总容量：永久拒绝。
            if session.reserved_tokens > self._cache_capacity:
                session.state = SessionState.FINISHED
                session.finish_reason = None
                self._terminated[request_id] = session
                rejected.append(
                    RejectedRequest(
                        request_id=request_id,
                        reason=RejectReason.EXCEEDS_CAPACITY,
                        required_tokens=session.reserved_tokens,
                        cache_capacity=self._cache_capacity,
                    )
                )
                continue

            # 并发会话数上限（不超过每轮槽数 B）。
            if len(self._active) >= self._slot_budget:
                self._waiting.append(request_id)
                waiting_map[request_id] = session
                continue

            # 队首暂时放不下：跳过并保留入队次序，后续请求继续尝试。
            if self._cache_reserved + session.reserved_tokens > self._cache_capacity:
                self._waiting.append(request_id)
                waiting_map[request_id] = session
                continue

            # 接纳：一次性预留整条会话的缓存。
            session.state = SessionState.PREFILL
            self._cache_reserved += session.reserved_tokens
            self._active[request_id] = session

    def _release(self, session: _Session) -> None:
        """轮末释放会话预留并移出活跃集合。"""
        self._active.pop(session.request_id, None)
        self._cache_reserved -= session.reserved_tokens
        if self._cache_reserved < 0:  # 防御性检查，正常流程不应触发
            self._cache_reserved = 0
        session.state = SessionState.FINISHED
        self._terminated[session.request_id] = session

    @staticmethod
    def _make_finished(session: _Session) -> FinishedRequest:
        if session.finish_reason is None:
            raise RuntimeError(
                f"会话 {session.request_id!r} 进入终止上报但缺少终止原因"
            )
        return FinishedRequest(
            request_id=session.request_id,
            reason=session.finish_reason,
            prompt_len=session.prompt_len,
            output_len=len(session.output_tokens),
            output_tokens=tuple(session.output_tokens),
        )

    @staticmethod
    def _normalize_provider(
        token_provider: Union[TokenProvider, Mapping[str, str], None],
    ) -> Optional[TokenProvider]:
        if token_provider is None:
            return None
        if callable(token_provider):
            return token_provider
        if isinstance(token_provider, Mapping):
            mapping = token_provider

            def _lookup(request_id: str, _round_index: int) -> str:
                return mapping[request_id]

            return _lookup
        raise TypeError("token_provider 必须是回调、映射或 None")
