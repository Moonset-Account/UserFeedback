"""页面级崩溃恢复主流程。

输入：

* ``current``：当前页文件存储（可能含已落盘提交号、也可能损坏或越过恢复点）；
* ``backup``：旧的完整页备份存储；
* ``journal``：追加日志（文件路径或可读二进制流）；
* ``recovery_point``：可选恢复点（提交号上界）。``None`` 表示不设上界，
  恢复到日志中最后一个提交；给定时只应用 ``commit_seq <= recovery_point`` 的事务。

恢复规则（与题面逐条对应）：

1. 先完整扫描日志。写记录按事务归组、保持日志原次序；提交记录给出提交号，
   提交号在日志中必须严格递增，否则报错。
2. 只有带提交记录、且提交号未越过恢复点的事务才合格；未提交事务的跨页写
   整体排除——由于补丁按事务整体判定资格，单事务涉及的任意页都不会被
   部分应用。
3. 逐页选择基线：

   * 当前页校验正确且提交号未越过恢复点 → 从自身提交号续做，不读备份；
   * 当前页损坏、缺失或越过恢复点 → 从备份重建（备份页号计入读取清单），
     备份同样损坏或缺失则恢复失败。
4. 补丁在内存中应用：写偏移与替换字节超出当前页载荷边界即损坏，返回错误且
   不发布任何页。
5. 所有页都在内存中规划完成后才统一写回；写回仅针对内容实际变化的页，
   因此对恢复结果再执行一次恢复，备份读取清单与改写清单均为空。
6. 日志末条欠字节：截断位置通过结果返回，欠字节之前的完整记录照常恢复；
   中段完整记录校验失败由日志读取层抛出 :class:`JournalCorrupt`（携带坏位置
   字节偏移），恢复直接失败、不发布任何页。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import BinaryIO, Optional

from .journal import (
    CommitRecord,
    TruncatedJournal,
    WriteRecord,
    read_journal,
)
from .page import PAGE_HEADER_SIZE, Page, build_page, parse_page
from .store import PageStore


class RecoveryError(Exception):
    """恢复无法继续（备份损坏/缺失、补丁越界、日志语义非法等）。"""


@dataclass
class RecoveryResult:
    """一次恢复的产出与清单。"""

    recovery_point: int
    """本次实际采用的恢复点（提交号上界）。"""

    applied_commit_seqs: list[int]
    """实际应用的提交号，按日志中的提交先后排序。"""

    backup_pages_read: list[int]
    """实际读取过的备份页号（排序去重）。"""

    rewritten_pages: list[int]
    """实际写回过的当前页号（排序去重）。"""

    truncated: Optional[TruncatedJournal] = None
    """日志末条欠字节信息；无欠字节时为 ``None``。"""

    @property
    def ok(self) -> bool:
        """恢复是否正常完成（当前实现下正常返回即为 ``True``）。"""
        return True


@dataclass
class _TxnState:
    writes: list[WriteRecord] = field(default_factory=list)
    committed: bool = False
    commit_seq: int = 0
    commit_position: int = -1


def _open_journal(journal) -> tuple[BinaryIO, bool]:
    """接受文件路径或已打开的二进制流，返回 (流, 是否由本函数打开)。"""
    if isinstance(journal, (str, bytes, os.PathLike)):
        return open(journal, "rb"), True
    return journal, False


def _scan_journal(journal) -> tuple[
    list[WriteRecord], list[CommitRecord], Optional[TruncatedJournal]
]:
    stream, owned = _open_journal(journal)
    try:
        return read_journal(stream)
    finally:
        if owned:
            stream.close()


def _build_transactions(
    writes: list[WriteRecord], commits: list[CommitRecord]
) -> dict[int, _TxnState]:
    """把日志记录整理成事务状态机，同时做语义校验。"""
    txns: dict[int, _TxnState] = {}

    for record in writes:
        state = txns.get(record.transaction_id)
        if state is None:
            state = _TxnState()
            txns[record.transaction_id] = state
        elif state.committed:
            raise RecoveryError(
                f"write of transaction {record.transaction_id} at byte "
                f"{record.position} appears after its commit"
            )
        state.writes.append(record)

    last_commit_seq = 0
    for record in commits:
        if record.commit_seq <= last_commit_seq:
            raise RecoveryError(
                f"commit seq {record.commit_seq} at byte {record.position} "
                "is not strictly increasing"
            )
        last_commit_seq = record.commit_seq

        state = txns.get(record.transaction_id)
        if state is None:
            # 没有任何写记录的空事务：允许提交，但不影响任何页。
            state = _TxnState()
            txns[record.transaction_id] = state
        if state.committed:
            raise RecoveryError(
                f"transaction {record.transaction_id} committed twice "
                f"(second commit at byte {record.position})"
            )
        state.committed = True
        state.commit_seq = record.commit_seq
        state.commit_position = record.position

    return txns


def _load_baseline(
    page_id: int,
    current: PageStore,
    backup: PageStore,
    recovery_point: int,
    backup_read: list[int],
) -> Page:
    """按规则选择一页的基线内容。"""
    blob = current.read_page(page_id)
    if blob is not None:
        try:
            page = parse_page(blob)
        except ValueError:
            page = None  # 当前页损坏
        if page is not None and page.commit_seq <= recovery_point:
            return page

    # 当前页缺失、损坏或越过恢复点：从备份重建。
    backup_blob = backup.read_page(page_id)
    if backup_blob is None:
        raise RecoveryError(
            f"page {page_id} needs rebuild from backup but backup page is missing"
        )
    try:
        backup_page = parse_page(backup_blob)
    except ValueError as exc:
        raise RecoveryError(
            f"page {page_id} backup is corrupt and cannot rebuild: {exc}"
        ) from exc
    if backup_page.commit_seq > recovery_point:
        raise RecoveryError(
            f"page {page_id} backup commit seq {backup_page.commit_seq} exceeds "
            f"recovery point {recovery_point}"
        )
    backup_read.append(page_id)
    return backup_page


def _apply_writes(
    payload: bytes, page_writes: list[WriteRecord], page_id: int
) -> bytes:
    """在内存中按原次序给一页打补丁，越界即损坏。"""
    buf = bytearray(payload)
    for record in page_writes:
        end = record.offset + len(record.replacement)
        if record.offset < 0 or end > len(buf):
            raise RecoveryError(
                f"write at journal byte {record.position} on page {page_id} "
                f"exceeds payload bounds (offset={record.offset}, "
                f"replacement={len(record.replacement)} bytes, "
                f"payload={len(buf)} bytes)"
            )
        buf[record.offset:end] = record.replacement
    return bytes(buf)


def recover(
    current: PageStore,
    backup: PageStore,
    journal,
    recovery_point: Optional[int] = None,
    page_size: Optional[int] = None,
) -> RecoveryResult:
    """执行一次页面级恢复，见模块文档字符串。"""
    writes, commits, truncated = _scan_journal(journal)

    txns = _build_transactions(writes, commits)

    if recovery_point is None:
        recovery_point = max((c.commit_seq for c in commits), default=0)
    if recovery_point < 0:
        raise RecoveryError("recovery_point must be non-negative")

    # 合格事务：已提交且提交号未越过恢复点。未提交事务的写整体排除。
    eligible: list[_TxnState] = []
    applied_seqs: list[int] = []
    for record in commits:
        state = txns[record.transaction_id]
        if state.committed and record.commit_seq <= recovery_point:
            eligible.append(state)
            applied_seqs.append(record.commit_seq)

    # 候选页：当前存储、备份存储、合格事务补丁涉及的页号并集。
    candidate_ids = set(current.list_pages()) | set(backup.list_pages())
    per_page_writes: dict[int, list[WriteRecord]] = {}
    for state in eligible:
        for record in state.writes:
            candidate_ids.add(record.page_id)
            per_page_writes.setdefault(record.page_id, []).append(record)

    backup_read: list[int] = []
    staged: dict[int, bytes] = {}

    for page_id in sorted(candidate_ids):
        baseline = _load_baseline(
            page_id, current, backup, recovery_point, backup_read
        )
        page_writes = per_page_writes.get(page_id, [])

        if page_writes:
            new_payload = _apply_writes(baseline.payload, page_writes, page_id)
            # 页的新提交号取本页补丁中最后一条所属事务的提交号；
            # 同一事务内保持原次序，提交号天然不下降。
            last_txn_id = page_writes[-1].transaction_id
            new_commit_seq = txns[last_txn_id].commit_seq
            if new_commit_seq < baseline.commit_seq:
                raise RecoveryError(
                    f"page {page_id} would roll back from commit "
                    f"{baseline.commit_seq} to {new_commit_seq}"
                )
        else:
            new_payload = baseline.payload
            new_commit_seq = baseline.commit_seq

        effective_page_size = page_size
        if effective_page_size is None:
            old_blob = current.read_page(page_id)
            if old_blob is not None:
                effective_page_size = len(old_blob)
            else:
                backup_blob = backup.read_page(page_id)
                effective_page_size = (
                    len(backup_blob)
                    if backup_blob is not None
                    else PAGE_HEADER_SIZE + len(new_payload)
                )
        new_blob = build_page(new_commit_seq, new_payload, effective_page_size)

        old_blob = current.read_page(page_id)
        if old_blob != new_blob:
            staged[page_id] = new_blob

    # 全部规划成功后才统一发布，保证失败时不产生任何部分写入。
    for page_id, blob in staged.items():
        current.write_page(page_id, blob)

    return RecoveryResult(
        recovery_point=recovery_point,
        applied_commit_seqs=applied_seqs,
        backup_pages_read=sorted(set(backup_read)),
        rewritten_pages=sorted(staged.keys()),
        truncated=truncated,
    )
