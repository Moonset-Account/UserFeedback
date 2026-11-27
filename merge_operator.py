"""批计算执行器的多路归并算子。

将各分片内按键有序的行做 k 路归并；相等键按全局输入序号（seq）给出稳定次序。
分片可用空洞标记（HoleOpen/HoleClosed）声明一段键区间正在重算，空洞携带键的下界；
算子只在能证明顺序安全（所有分片的 frontier 均严格大于待输出键）时才输出，
因此一个分片重算期间，其余分片键小于空洞下界的行仍可继续输出。

内存不足时把已知的分片片段溢写（spill）到 work_dir 下的文件；checkpoint 之后
可用 MergeOperator.recover 在同一读位置重开。输出经 OutputCommitter 做批次确认，
半次提交（emit 后未 ack 即崩溃）的批次在恢复时作废，不能被重复确认。
"""
from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Optional, Sequence, Tuple


# ------------------------------------------------------------------ 哨兵键

class _KeyBound:
    """两侧无穷哨兵：MIN_KEY 表示无界下界，MAX_KEY 表示已耗尽分片的 frontier。"""

    __slots__ = ("_name", "_is_min")

    def __init__(self, name: str, is_min: bool) -> None:
        self._name = name
        self._is_min = is_min

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, _KeyBound):
            return self._is_min and not other._is_min
        return self._is_min

    def __le__(self, other: Any) -> bool:
        if isinstance(other, _KeyBound):
            return self._is_min or not other._is_min
        return self._is_min

    def __gt__(self, other: Any) -> bool:
        if isinstance(other, _KeyBound):
            return not self._is_min and other._is_min
        return not self._is_min

    def __ge__(self, other: Any) -> bool:
        if isinstance(other, _KeyBound):
            return not self._is_min or other._is_min
        return not self._is_min

    def __eq__(self, other: Any) -> bool:
        return self is other

    def __ne__(self, other: Any) -> bool:
        return self is not other

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return self._name


MIN_KEY = _KeyBound("MIN_KEY", True)
MAX_KEY = _KeyBound("MAX_KEY", False)


# ------------------------------------------------------------------ 事件

@dataclass(frozen=True)
class Row:
    """一行输入。key 为归并键（不可为 None），seq 为全局输入序号（假定全局唯一）。"""

    key: Any
    seq: int
    payload: Any = None


@dataclass(frozen=True)
class HoleOpen:
    """空洞开启：本分片键 >= lower_bound 的输入正在重算，暂缓投递。

    lower_bound 为 None（或 MIN_KEY）表示无界下界，即整个分片输入都在重算。
    承诺：键 < lower_bound 的行已全部投递且为最终结果；键 == lower_bound 的
    已投递行同样为最终结果（相同键可跨空洞边界，重算后还会再来一批）。
    """

    lower_bound: Any = MIN_KEY


@dataclass(frozen=True)
class HoleClosed:
    """空洞关闭：重算完成，随后投递的是重算后的行（键 >= 空洞下界）。"""


@dataclass(frozen=True)
class Eos:
    """分片输入结束（end of stream）。结束时不允许有未关闭的空洞。"""


# ------------------------------------------------------------------ 异常

class MergeError(Exception):
    """算子相关错误的基类。"""


class SourceOrderError(MergeError):
    """分片内行键未按非降序投递（含误过空洞导致的键序回退）。"""


class SourceProtocolError(MergeError):
    """分片违反空洞协议（空洞期间投递行、下界倒退、未关闭即结束等）。"""


class HoleRegressionError(MergeError):
    """空洞下界覆盖了已经输出的前缀，无法保证顺序安全。"""


class OutputOrderError(MergeError):
    """算子自检：待输出的键不大于已输出键，顺序被破坏。"""


class CommitError(MergeError):
    """输出确认相关错误的基类。"""


class DoubleAckError(CommitError):
    """同一批次被重复确认。"""


class StaleBatchError(CommitError):
    """确认的批次已在恢复时作废（半次提交的批次不能再次被确认）。"""


class UnknownBatchError(CommitError):
    """确认了一个不存在的批次。"""


# ------------------------------------------------------------------ 输入源

class ListSource:
    """基于事件列表的分片源，用于把分片事件脚本化回放。

    poll() 返回 None 表示"暂时没有事件"（例如正在重算），不等于结束；
    结束必须显式投递 Eos。extend() 可在运行中追加事件（模拟重算完成）。
    offset/seek 供检查点记录与恢复读位置。
    """

    def __init__(self, events: Sequence[Any] = ()) -> None:
        self._events: List[Any] = list(events)
        self._pos = 0

    def poll(self) -> Optional[Any]:
        if self._pos < len(self._events):
            ev = self._events[self._pos]
            self._pos += 1
            return ev
        return None

    def extend(self, events: Sequence[Any]) -> None:
        self._events.extend(events)

    @property
    def offset(self) -> int:
        return self._pos

    def seek(self, offset: int) -> None:
        if not 0 <= offset <= len(self._events):
            raise ValueError(f"非法读位置 {offset}（事件数 {len(self._events)}）")
        self._pos = offset

    def __len__(self) -> int:
        return len(self._events)


# ------------------------------------------------------------------ 序列化

def _row_to_json(row: Row) -> list:
    return [row.key, row.seq, row.payload]


def _row_from_json(rec: list) -> Row:
    return Row(key=rec[0], seq=rec[1], payload=rec[2])


def _read_spill_rows(path: str, start: int, count: int) -> List[Row]:
    rows: List[Row] = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i < start:
                continue
            if len(rows) >= count:
                break
            rows.append(_row_from_json(json.loads(line)))
    return rows


# ------------------------------------------------------------------ 输出确认

class OutputCommitter:
    """输出批次的恰好一次确认。

    emit() 先把整批行追加到 WAL（fsync）再返回批次号；ack() 追加确认记录。
    恢复时重放 WAL：撕裂的尾部记录（半次写入）被丢弃；emit 了但未 ack 且
    超出算子检查点视野的批次由 abort_after() 作废——作废批次之后任何 ack
    都会抛 StaleBatchError，已确认批次再 ack 抛 DoubleAckError，
    从而保证半次提交的输出不能被重复确认。
    """

    WAL_FILE = "output.wal"

    def __init__(self, work_dir: str) -> None:
        self._wal_path = os.path.join(work_dir, self.WAL_FILE)
        self._batches: dict[int, List[Row]] = {}
        self._acked: set[int] = set()
        self._aborted: set[int] = set()
        self._max_id = 0
        if os.path.exists(self._wal_path):
            self._replay()

    def _replay(self) -> None:
        with open(self._wal_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    break  # 撕裂的尾部记录：丢弃
                self._apply(rec)

    def _apply(self, rec: dict) -> None:
        op = rec.get("op")
        if op == "emit":
            self._batches[rec["id"]] = [_row_from_json(r) for r in rec["rows"]]
            self._max_id = max(self._max_id, rec["id"])
        elif op == "ack":
            self._acked.add(rec["id"])
        elif op == "abort":
            self._aborted.add(rec["id"])

    def _append(self, rec: dict) -> None:
        with open(self._wal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._apply(rec)

    def emit(self, rows: Sequence[Row]) -> int:
        batch_id = self._max_id + 1
        self._append({"op": "emit", "id": batch_id,
                      "rows": [_row_to_json(r) for r in rows]})
        return batch_id

    def ack(self, batch_id: int) -> None:
        if batch_id in self._aborted:
            raise StaleBatchError(f"批次 {batch_id} 已在恢复时作废，不能确认")
        if batch_id in self._acked:
            raise DoubleAckError(f"批次 {batch_id} 已确认过，不能重复确认")
        if batch_id not in self._batches:
            raise UnknownBatchError(f"未知批次 {batch_id}")
        self._append({"op": "ack", "id": batch_id})

    def abort_after(self, max_keep_id: int) -> None:
        """作废 id 大于 max_keep_id 且尚未确认的批次（恢复时调用）。"""
        for bid in sorted(self._batches):
            if bid > max_keep_id and bid not in self._acked and bid not in self._aborted:
                self._append({"op": "abort", "id": bid})

    def pending(self) -> List[Tuple[int, List[Row]]]:
        """已 emit 但未确认、未作废的批次（恢复后需重新投递给下游）。"""
        return [(bid, self._batches[bid]) for bid in sorted(self._batches)
                if bid not in self._acked and bid not in self._aborted]

    def is_acked(self, batch_id: int) -> bool:
        return batch_id in self._acked

    def is_aborted(self, batch_id: int) -> bool:
        return batch_id in self._aborted


# ------------------------------------------------------------------ 分片状态

class _ShardState:
    """单个分片的缓冲状态。

    未输出行的物理顺序为：head 队列 -> 溢写文件未读部分 -> buffer 队列。
    head 是从溢写文件读回的行（只消费、不再溢写）；buffer 是新到达的尾部，
    内存超限时只有 buffer 会被溢写追加到文件末尾，从而保证文件内行序不变。
    """

    __slots__ = ("index", "source", "head", "buffer", "holes", "exhausted",
                 "last_row_key", "key_floor", "pending_count",
                 "last_buffered_key", "spill_path", "spill_written", "spill_read")

    def __init__(self, source: Any, work_dir: str, index: int) -> None:
        self.index = index
        self.source = source
        self.head: Deque[Row] = deque()        # 从溢写文件读回的行（先消费）
        self.buffer: Deque[Row] = deque()      # 新到达的尾部（内存超限时可溢写）
        self.holes: List[Any] = []             # 活动空洞的下界（可嵌套）
        self.exhausted = False
        self.last_row_key: Any = MIN_KEY       # 最近投递行的键（源内有序性检查）
        self.key_floor: Any = MIN_KEY          # 已关闭空洞下界的最大值
        self.pending_count = 0                 # 未输出行数（head + 溢写 + buffer）
        self.last_buffered_key: Any = MIN_KEY  # 最近缓冲行的键
        self.spill_path = os.path.join(work_dir, f"shard_{index}.spill.jsonl")
        self.spill_written = 0                 # 溢写文件已写入行数
        self.spill_read = 0                    # 溢写文件已读回行数


# ------------------------------------------------------------------ 归并算子

class MergeOperator:
    """多路归并算子。

    使用方式：
        op = MergeOperator(sources, work_dir, memory_limit=...)
        rows = op.emit_ready()        # 返回一批可证明顺序安全的行（一个输出批次）
        op.ack(op.last_batch_id)      # 下游确认
        op.checkpoint()               # 持久化读位置 / 溢写状态 / 确认视野
        op = MergeOperator.recover(sources, work_dir)   # 崩溃后在同一读位置重开

    顺序安全条件：某键 k 的所有行可输出，当且仅当每个分片的 frontier 都严格
    大于 k。frontier 综合考虑已缓冲行的最大键、活动空洞下界与是否耗尽。
    """

    STATE_FILE = "state.json"

    def __init__(self, sources: Sequence[Any], work_dir: str,
                 memory_limit: int = 1024, spill_chunk: Optional[int] = None) -> None:
        state_path = os.path.join(work_dir, self.STATE_FILE)
        if os.path.exists(state_path):
            raise MergeError(f"{work_dir} 已存在检查点，请改用 MergeOperator.recover()")
        os.makedirs(work_dir, exist_ok=True)
        self._work_dir = work_dir
        self._memory_limit = max(1, int(memory_limit))
        self._spill_chunk = (max(1, int(spill_chunk)) if spill_chunk
                             else max(1, min(64, self._memory_limit)))
        self._shards = [_ShardState(src, work_dir, i) for i, src in enumerate(sources)]
        self._buffered = 0
        self._emitted_any = False
        self._last_emitted_key: Any = MIN_KEY
        self._last_batch_id = 0
        self._committer = OutputCommitter(work_dir)

    # ------------------------------------------------------------ 恢复

    @classmethod
    def recover(cls, sources: Sequence[Any], work_dir: str,
                memory_limit: int = 1024,
                spill_chunk: Optional[int] = None) -> "MergeOperator":
        state_path = os.path.join(work_dir, cls.STATE_FILE)
        if not os.path.exists(state_path):
            raise MergeError(f"{work_dir} 没有检查点，无法恢复")
        with open(state_path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if st.get("version") != 1:
            raise MergeError(f"未知检查点版本: {st.get('version')!r}")
        if len(sources) != len(st["shards"]):
            raise MergeError("分片数量与检查点不一致")

        self = cls.__new__(cls)
        self._work_dir = work_dir
        self._memory_limit = max(1, int(memory_limit))
        self._spill_chunk = (max(1, int(spill_chunk)) if spill_chunk
                             else max(1, min(64, self._memory_limit)))
        self._shards = []
        for i, (src, sst) in enumerate(zip(sources, st["shards"])):
            sh = _ShardState(src, work_dir, i)
            src.seek(sst["offset"])  # 重开要继续同一读位置
            sh.holes = [MIN_KEY if h is None else h for h in sst["holes"]]
            sh.exhausted = sst["exhausted"]
            sh.last_row_key = MIN_KEY if sst["last_row_key"] is None else sst["last_row_key"]
            sh.key_floor = MIN_KEY if sst["key_floor"] is None else sst["key_floor"]
            sh.pending_count = sst["pending_count"]
            sh.last_buffered_key = (MIN_KEY if sst["last_buffered_key"] is None
                                    else sst["last_buffered_key"])
            sh.spill_written = sst["spill_written"]
            sh.spill_read = sst["spill_read"]
            if sh.spill_written > 0 and not os.path.exists(sh.spill_path):
                raise MergeError(f"溢写文件丢失: {sh.spill_path}")
            sh.head = deque(_row_from_json(r) for r in sst["head"])
            sh.buffer = deque(_row_from_json(r) for r in sst["buffer"])
            self._shards.append(sh)
        self._buffered = sum(len(sh.buffer) for sh in self._shards)
        self._emitted_any = st["emitted_any"]
        self._last_emitted_key = (MIN_KEY if st["last_emitted_key"] is None
                                  else st["last_emitted_key"])
        self._last_batch_id = st["last_batch_id"]
        self._committer = OutputCommitter(work_dir)
        # 检查点之后 emit 但未 ack 的批次属于半次提交：作废，由算子重新发射。
        self._committer.abort_after(self._last_batch_id)
        return self

    # ------------------------------------------------------------ 事件处理

    def _apply_event(self, sh: _ShardState, ev: Any) -> None:
        if isinstance(ev, Row):
            if sh.holes:
                raise SourceProtocolError(
                    f"分片 {sh.index} 在空洞开启期间投递了行 {ev!r}")
            if ev.key < sh.last_row_key:
                raise SourceOrderError(
                    f"分片 {sh.index} 键序回退: {sh.last_row_key!r} -> {ev.key!r}")
            if ev.key < sh.key_floor:
                raise SourceProtocolError(
                    f"分片 {sh.index} 投递了低于已关闭空洞下界的行: "
                    f"{ev.key!r} < {sh.key_floor!r}")
            sh.last_row_key = ev.key
            self._buffer_row(sh, ev)
        elif isinstance(ev, HoleOpen):
            lb = MIN_KEY if ev.lower_bound is None else ev.lower_bound
            if self._emitted_any and not (self._last_emitted_key < lb):
                raise HoleRegressionError(
                    f"空洞下界 {lb!r} 覆盖了已输出的前缀（已输出至 "
                    f"{self._last_emitted_key!r}）")
            if lb < sh.last_row_key:
                raise SourceProtocolError(
                    f"空洞下界 {lb!r} 低于分片 {sh.index} 已投递的键 "
                    f"{sh.last_row_key!r}")
            sh.holes.append(lb)
        elif isinstance(ev, HoleClosed):
            if not sh.holes:
                raise SourceProtocolError(f"分片 {sh.index} 收到多余的 HoleClosed")
            bound = sh.holes.pop()
            if bound > sh.key_floor:
                sh.key_floor = bound
        elif isinstance(ev, Eos):
            if sh.holes:
                raise SourceProtocolError(f"分片 {sh.index} 在空洞未关闭时结束")
            sh.exhausted = True
        else:
            raise TypeError(f"未知事件类型: {ev!r}")

    # ------------------------------------------------------------ 缓冲与溢写

    def _buffer_row(self, sh: _ShardState, row: Row) -> None:
        sh.buffer.append(row)
        sh.last_buffered_key = row.key
        sh.pending_count += 1
        self._buffered += 1
        if self._buffered > self._memory_limit:
            self._spill_largest()

    def _spill_largest(self) -> None:
        victim = max(self._shards, key=lambda s: len(s.buffer))
        if not victim.buffer:
            return
        n = len(victim.buffer)
        mode = "a" if victim.spill_written > 0 else "w"
        with open(victim.spill_path, mode, encoding="utf-8") as fh:
            for r in victim.buffer:
                fh.write(json.dumps(_row_to_json(r), ensure_ascii=False) + "\n")
        victim.spill_written += n
        victim.buffer.clear()
        self._buffered -= n

    def _ensure_head(self, sh: _ShardState) -> None:
        """head 为空且溢写文件还有未读行时，读回一个块到 head。"""
        if sh.head:
            return
        if sh.spill_written > 0 and sh.spill_read >= sh.spill_written:
            sh.spill_written = 0  # 文件内容已全部读回并消费，逻辑上重置
            sh.spill_read = 0
            return
        if sh.spill_read >= sh.spill_written:
            return
        n = min(self._spill_chunk, sh.spill_written - sh.spill_read)
        rows = _read_spill_rows(sh.spill_path, sh.spill_read, n)
        sh.spill_read += len(rows)
        sh.head.extend(rows)

    @staticmethod
    def _head_row(sh: _ShardState) -> Optional[Row]:
        if sh.head:
            return sh.head[0]
        if sh.buffer:
            return sh.buffer[0]
        return None

    # ------------------------------------------------------------ frontier

    def _fill_shard(self, sh: _ShardState) -> None:
        """拉取当前可用的全部事件（源在重算等情况下会暂时返回 None）。"""
        while not sh.exhausted:
            ev = sh.source.poll()
            if ev is None:
                return
            self._apply_event(sh, ev)

    def _frontier(self, sh: _ShardState) -> Any:
        """键 < frontier 的行在该分片已全部投递且为最终结果。"""
        if sh.exhausted:
            return MAX_KEY
        f: Any = MIN_KEY
        if sh.holes:
            f = min(sh.holes)
        if sh.pending_count > 0 and sh.last_buffered_key > f:
            f = sh.last_buffered_key
        return f

    # ------------------------------------------------------------ 输出

    def emit_ready(self) -> List[Row]:
        """返回当前可证明顺序安全的全部行（一个输出批次）；没有则返回空表。"""
        out: List[Row] = []
        while True:
            for sh in self._shards:
                self._fill_shard(sh)
                self._ensure_head(sh)
            frontier = min(self._frontier(sh) for sh in self._shards)
            k: Any = MAX_KEY
            for sh in self._shards:
                row = self._head_row(sh)
                if row is not None and row.key < k:
                    k = row.key
            if k is MAX_KEY or not (k < frontier):
                break
            out.extend(self._pop_group(k))
        if out:
            self._last_batch_id = self._committer.emit(out)
        return out

    def _pop_group(self, k: Any) -> List[Row]:
        if self._emitted_any and not (self._last_emitted_key < k):
            raise OutputOrderError(
                f"输出键回退: {self._last_emitted_key!r} -> {k!r}")
        group: List[Row] = []
        for sh in self._shards:
            while True:
                self._ensure_head(sh)
                dq = sh.head if sh.head else sh.buffer
                if not dq or dq[0].key != k:
                    break
                row = dq.popleft()
                if dq is sh.buffer:
                    self._buffered -= 1
                sh.pending_count -= 1
                group.append(row)
        # 相等键按全局输入序号稳定排序
        group.sort(key=lambda r: r.seq)
        self._last_emitted_key = k
        self._emitted_any = True
        return group

    # ------------------------------------------------------------ 确认与检查点

    @property
    def last_batch_id(self) -> int:
        return self._last_batch_id

    def ack(self, batch_id: int) -> None:
        self._committer.ack(batch_id)

    def pending(self) -> List[Tuple[int, List[Row]]]:
        return self._committer.pending()

    @property
    def committer(self) -> OutputCommitter:
        return self._committer

    @property
    def exhausted(self) -> bool:
        return all(sh.exhausted and sh.pending_count == 0 for sh in self._shards)

    def checkpoint(self) -> None:
        st = {
            "version": 1,
            "emitted_any": self._emitted_any,
            "last_emitted_key": self._last_emitted_key if self._emitted_any else None,
            "last_batch_id": self._last_batch_id,
            "shards": [self._shard_to_json(sh) for sh in self._shards],
        }
        tmp = os.path.join(self._work_dir, self.STATE_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(self._work_dir, self.STATE_FILE))

    @staticmethod
    def _shard_to_json(sh: _ShardState) -> dict:
        return {
            "offset": sh.source.offset,
            "holes": [None if h is MIN_KEY else h for h in sh.holes],
            "exhausted": sh.exhausted,
            "last_row_key": None if sh.last_row_key is MIN_KEY else sh.last_row_key,
            "key_floor": None if sh.key_floor is MIN_KEY else sh.key_floor,
            "pending_count": sh.pending_count,
            "last_buffered_key": (None if sh.pending_count == 0
                                  else sh.last_buffered_key),
            "spill_written": sh.spill_written,
            "spill_read": sh.spill_read,
            "head": [_row_to_json(r) for r in sh.head],
            "buffer": [_row_to_json(r) for r in sh.buffer],
        }
