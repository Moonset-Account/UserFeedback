"""恢复模块的本地合成数据测试（unittest）。

注意：这些测试只在本地用内存存储和 ``io.BytesIO`` 合成数据，不接触任何真实
数据库、凭据或网络。按本次任务约束，测试代码已编写但**未在本会话中运行**，
README 中有同样的标注。
"""

from __future__ import annotations

import io
import os
import struct
import tempfile
import unittest

from db_recovery.journal import (
    JournalCorrupt,
    JournalWriter,
    TruncatedJournal,
    read_journal,
)
from db_recovery.page import build_page, parse_page
from db_recovery.recovery import RecoveryError, recover
from db_recovery.store import DirectoryPageStore, MemoryPageStore

PAGE_SIZE = 256


def make_page(commit_seq: int, payload: bytes) -> bytes:
    return build_page(commit_seq, payload, PAGE_SIZE)


def page_payload(blob: bytes) -> bytes:
    return parse_page(blob).payload


def make_journal(blocks) -> io.BytesIO:
    """按 (调用描述) 列表构造日志流。

    块形式：
    * ``("w", txn, page_id, offset, replacement)``
    * ``("c", txn, commit_seq)``
    """
    buf = io.BytesIO()
    writer = JournalWriter(buf)
    for block in blocks:
        if block[0] == "w":
            _, txn, page_id, offset, replacement = block
            writer.write(txn, page_id, offset, replacement)
        else:
            _, txn, commit_seq = block
            writer.commit(txn, commit_seq)
    buf.seek(0)
    return buf


class BasicRecoveryTests(unittest.TestCase):
    def test_committed_transactions_applied_in_commit_order_and_idempotent(self):
        backup = MemoryPageStore({1: make_page(0, b"A" * 16)})
        current = MemoryPageStore({1: make_page(0, b"A" * 16)})
        journal = make_journal(
            [
                ("w", 10, 1, 0, b"BB"),
                ("c", 10, 1),
                ("w", 11, 1, 2, b"CCC"),
                ("c", 11, 2),
            ]
        )

        result = recover(current, backup, journal)
        self.assertEqual(result.applied_commit_seqs, [1, 2])
        self.assertEqual(result.rewritten_pages, [1])
        self.assertEqual(result.backup_pages_read, [])
        self.assertEqual(page_payload(current.read_page(1)), b"BBCCC" + b"A" * 11)
        self.assertEqual(parse_page(current.read_page(1)).commit_seq, 2)

        # 对恢复结果再开一次：两张清单都应为空。
        journal.seek(0)
        again = recover(current, backup, journal)
        self.assertEqual(again.backup_pages_read, [])
        self.assertEqual(again.rewritten_pages, [])
        self.assertEqual(again.applied_commit_seqs, [1, 2])

    def test_uncommitted_cross_page_writes_excluded_entirely(self):
        backup = MemoryPageStore(
            {
                1: make_page(0, b"1" * 8),
                2: make_page(0, b"2" * 8),
            }
        )
        current = MemoryPageStore(
            {
                1: make_page(0, b"1" * 8),
                2: make_page(0, b"2" * 8),
            }
        )
        # 事务 20 跨页 1/2 但从未提交；事务 21 只改页 2 并提交。
        journal = make_journal(
            [
                ("w", 20, 1, 0, b"XX"),
                ("w", 20, 2, 0, b"YY"),
                ("w", 21, 2, 4, b"ZZ"),
                ("c", 21, 1),
            ]
        )

        result = recover(current, backup, journal)
        self.assertEqual(result.applied_commit_seqs, [1])
        self.assertEqual(result.rewritten_pages, [2])
        self.assertEqual(page_payload(current.read_page(1)), b"1" * 8)
        self.assertEqual(page_payload(current.read_page(2)), b"2222ZZ22")

    def test_resume_from_current_page_commit_seq(self):
        # 当前页已经落盘到提交号 2，日志包含 1..3；重放 1/2 应得到相同内容，
        # 只有提交 3 的补丁产生实际改写，且全程不读备份。
        backup = MemoryPageStore({1: make_page(0, b"A" * 8)})
        current = MemoryPageStore({1: make_page(2, b"AABBCC" + b"A" * 2)})
        journal = make_journal(
            [
                ("w", 1, 1, 0, b"AA"),
                ("c", 1, 1),
                ("w", 2, 1, 2, b"BB"),
                ("c", 2, 2),
                ("w", 3, 1, 4, b"CC"),
                ("c", 3, 3),
            ]
        )

        result = recover(current, backup, journal)
        self.assertEqual(result.backup_pages_read, [])
        self.assertEqual(result.rewritten_pages, [1])
        # 从自身提交号 2 续做：补丁 1/2 幂等重放后内容不变，仅提交 3 生效。
        self.assertEqual(page_payload(current.read_page(1)), b"AABBCCAA")
        self.assertEqual(parse_page(current.read_page(1)).commit_seq, 3)

    def test_corrupt_current_page_rebuilt_from_backup(self):
        good = make_page(0, b"OLD" + b"-" * 5)
        backup = MemoryPageStore({7: good})
        current = MemoryPageStore({7: b"garbage-not-a-page"})
        journal = make_journal([("w", 1, 7, 0, b"NEW"), ("c", 1, 1)])

        result = recover(current, backup, journal)
        self.assertEqual(result.backup_pages_read, [7])
        self.assertEqual(result.rewritten_pages, [7])
        self.assertEqual(page_payload(current.read_page(7)), b"NEW" + b"-" * 5)

    def test_recovery_point_excludes_later_commits(self):
        backup = MemoryPageStore({1: make_page(0, b"0" * 8)})
        current = MemoryPageStore({1: make_page(0, b"0" * 8)})
        journal = make_journal(
            [
                ("w", 1, 1, 0, b"aa"),
                ("c", 1, 1),
                ("w", 2, 1, 2, b"bb"),
                ("c", 2, 2),
                ("w", 3, 1, 4, b"cc"),
                ("c", 3, 3),
            ]
        )

        result = recover(current, backup, journal, recovery_point=2)
        self.assertEqual(result.recovery_point, 2)
        self.assertEqual(result.applied_commit_seqs, [1, 2])
        self.assertEqual(page_payload(current.read_page(1)), b"aabb0000")
        self.assertEqual(parse_page(current.read_page(1)).commit_seq, 2)

    def test_current_page_past_recovery_point_rebuilt_from_backup(self):
        backup = MemoryPageStore({1: make_page(0, b"B" * 8)})
        # 当前页提交号 5 越过恢复点 2，必须从备份重建。
        current = MemoryPageStore({1: make_page(5, b"X" * 8)})
        journal = make_journal(
            [
                ("w", 1, 1, 0, b"11"),
                ("c", 1, 1),
                ("w", 5, 1, 0, b"55"),
                ("c", 5, 5),
            ]
        )

        result = recover(current, backup, journal, recovery_point=2)
        self.assertEqual(result.backup_pages_read, [1])
        self.assertEqual(page_payload(current.read_page(1)), b"11" + b"B" * 6)
        self.assertEqual(parse_page(current.read_page(1)).commit_seq, 1)

    def test_torn_tail_is_truncated_and_earlier_records_apply(self):
        journal_buf = make_journal(
            [
                ("w", 1, 1, 0, b"QQ"),
                ("c", 1, 1),
            ]
        )
        good_bytes = journal_buf.getvalue()
        # 追加一条写记录的前 7 个字节（头部 17 字节都不够），模拟末条欠字节。
        torn = bytearray(good_bytes) + b"\x00" * 7
        stream = io.BytesIO(bytes(torn))

        backup = MemoryPageStore({1: make_page(0, b"z" * 8)})
        current = MemoryPageStore({1: make_page(0, b"z" * 8)})
        result = recover(current, backup, stream)

        self.assertIsNotNone(result.truncated)
        self.assertEqual(result.truncated.position, len(good_bytes))
        self.assertEqual(result.truncated.missing, -1)
        self.assertEqual(result.applied_commit_seqs, [1])
        self.assertEqual(page_payload(current.read_page(1)), b"QQzzzzzz")

    def test_middle_record_checksum_failure_stops_without_publishing(self):
        good = make_journal(
            [
                ("w", 1, 1, 0, b"aa"),
                ("c", 1, 1),
                ("w", 2, 1, 2, b"bb"),
                ("c", 2, 2),
            ]
        ).getvalue()

        # 解析第一条记录长度，定位第二条记录并翻转其 CRC 首字节。
        first_len = struct.unpack(
            ">I", good[5:9]
        )[0]
        second_start = 17 + first_len + 4
        second_body_len = struct.unpack(">I", good[second_start + 5:second_start + 9])[0]
        crc_pos = second_start + 17 + second_body_len
        damaged = bytearray(good)
        damaged[crc_pos] ^= 0xFF

        backup = MemoryPageStore({1: make_page(0, b"0" * 8)})
        current = MemoryPageStore({1: make_page(0, b"0" * 8)})
        before = current.read_page(1)

        with self.assertRaises(JournalCorrupt) as ctx:
            recover(current, backup, io.BytesIO(bytes(damaged)))
        self.assertEqual(ctx.exception.position, second_start)
        # 没有任何页被发布。
        self.assertEqual(current.read_page(1), before)

    def test_multi_page_recovery_lists_and_idempotency(self):
        backup = MemoryPageStore(
            {
                1: make_page(0, b"a" * 8),
                2: make_page(0, b"b" * 8),
                3: make_page(0, b"c" * 8),
            }
        )
        current = MemoryPageStore(
            {
                1: make_page(0, b"a" * 8),
                2: make_page(0, b"b" * 8),
                3: make_page(0, b"c" * 8),
            }
        )
        journal = make_journal(
            [
                ("w", 1, 1, 0, b"11"),
                ("w", 1, 2, 0, b"22"),
                ("c", 1, 1),
                ("w", 2, 3, 0, b"33"),
                ("c", 2, 2),
            ]
        )

        result = recover(current, backup, journal)
        self.assertEqual(result.rewritten_pages, [1, 2, 3])
        self.assertEqual(result.backup_pages_read, [])

        journal.seek(0)
        again = recover(current, backup, journal)
        self.assertEqual(again.rewritten_pages, [])
        self.assertEqual(again.backup_pages_read, [])

    def test_missing_backup_for_rebuild_is_fatal_and_publishes_nothing(self):
        backup = MemoryPageStore({})  # 备份里没有页 9
        current = MemoryPageStore({9: b"broken"})
        journal = make_journal([("w", 1, 9, 0, b"x"), ("c", 1, 1)])

        with self.assertRaises(RecoveryError):
            recover(current, backup, journal)
        self.assertEqual(current.read_page(9), b"broken")

    def test_out_of_bounds_write_is_fatal_and_publishes_nothing(self):
        backup = MemoryPageStore({1: make_page(0, b"0" * 8)})
        current = MemoryPageStore({1: make_page(0, b"0" * 8)})
        journal = make_journal(
            [
                ("w", 1, 1, 6, b"TOO-LONG"),  # 6 + 8 > 8
                ("c", 1, 1),
            ]
        )
        before = current.read_page(1)
        with self.assertRaises(RecoveryError):
            recover(current, backup, journal)
        self.assertEqual(current.read_page(1), before)

    def test_within_transaction_write_order_is_preserved(self):
        backup = MemoryPageStore({1: make_page(0, b"0" * 8)})
        current = MemoryPageStore({1: make_page(0, b"0" * 8)})
        journal = make_journal(
            [
                ("w", 1, 1, 0, b"AB"),
                ("w", 1, 1, 0, b"CD"),  # 同事务后写覆盖先写
                ("c", 1, 1),
            ]
        )
        recover(current, backup, journal)
        self.assertEqual(page_payload(current.read_page(1)), b"CD000000")


class JournalReaderTests(unittest.TestCase):
    def test_torn_body_reports_missing_byte_count(self):
        good = make_journal([("w", 1, 1, 0, b"abcdef"), ("c", 1, 1)]).getvalue()
        # 只欠 3 字节（CRC 尾部缺 3 字节），头部与长度完整。
        torn = good[:-3]
        writes, commits, truncated = read_journal(io.BytesIO(torn))
        self.assertEqual(len(writes), 1)
        self.assertEqual(commits, [])
        self.assertIsInstance(truncated, TruncatedJournal)
        self.assertEqual(truncated.missing, 3)

    def test_bad_magic_raises_with_position(self):
        good = bytearray(
            make_journal([("w", 1, 1, 0, b"a"), ("c", 1, 1)]).getvalue()
        )
        first_len = struct.unpack(">I", good[5:9])[0]
        second_start = 17 + first_len + 4
        good[second_start:second_start + 4] = b"XXXX"
        with self.assertRaises(JournalCorrupt) as ctx:
            read_journal(io.BytesIO(bytes(good)))
        self.assertEqual(ctx.exception.position, second_start)

    def test_empty_journal(self):
        writes, commits, truncated = read_journal(io.BytesIO(b""))
        self.assertEqual(writes, [])
        self.assertEqual(commits, [])
        self.assertIsNone(truncated)

    def test_non_increasing_commit_seq_rejected(self):
        backup = MemoryPageStore({1: make_page(0, b"0" * 8)})
        current = MemoryPageStore({1: make_page(0, b"0" * 8)})
        journal = make_journal(
            [
                ("w", 1, 1, 0, b"a"),
                ("c", 1, 2),
                ("w", 2, 1, 1, b"b"),
                ("c", 2, 2),  # 提交号重复
            ]
        )
        with self.assertRaises(RecoveryError):
            recover(current, backup, journal)


class DirectoryStoreTests(unittest.TestCase):
    def test_directory_store_roundtrip_and_recovery_idempotency(self):
        with tempfile.TemporaryDirectory() as root:
            current_dir = os.path.join(root, "current")
            backup_dir = os.path.join(root, "backup")
            journal_path = os.path.join(root, "journal.log")
            current = DirectoryPageStore(current_dir)
            backup = DirectoryPageStore(backup_dir)

            current.write_page(1, make_page(0, b"d" * 8))
            backup.write_page(1, make_page(0, b"d" * 8))

            with open(journal_path, "wb") as handle:
                writer = JournalWriter(handle)
                writer.write(1, 1, 0, b"xy")
                writer.commit(1, 1)

            result = recover(current, backup, journal_path)
            self.assertEqual(result.rewritten_pages, [1])
            self.assertEqual(result.backup_pages_read, [])
            self.assertEqual(page_payload(current.read_page(1)), b"xydddddd")

            again = recover(current, backup, journal_path)
            self.assertEqual(again.rewritten_pages, [])
            self.assertEqual(again.backup_pages_read, [])
            self.assertIn(1, current.list_pages())


if __name__ == "__main__":
    unittest.main()
