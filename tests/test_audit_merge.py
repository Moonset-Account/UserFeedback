"""audit_merge 验收测试（unittest，真实断言）。

覆盖题面交叉约束：

* 可接受结果：两种坐标的同改去重后只生效一次、CRLF/emoji 保留、
  多组分簇断言通过后落盘；
* 应被阻止/拒绝：旧文本不符、版本不符（整组原子撤下）、重叠冲突搁置、
  保护断言失效仅回滚所属簇并记录告警编号、未登记断言撤下；
* 边界/交错：UTF-8 字节边界与 UTF-16 代理对边界、相邻区间、
  零长/越界坐标、簇传递关联、临时副本与真实工作区隔离。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from audit_merge import (
    PLUGIN_LINE_UTF16,
    PLUGIN_UTF8_BYTES,
    SpanError,
    Workspace,
    merge_suggestions,
)
from audit_merge.coordinates import (
    line_utf16_to_indices,
    split_lines_preserving,
    utf8_offsets_to_indices,
)

EMOJI = "\U0001f600"  # 星平面，UTF-16 占 2 个码元，UTF-8 占 4 字节


def byte_span(text: str, start_char: int, end_char: int) -> tuple[int, int]:
    return len(text[:start_char].encode("utf-8")), len(text[:end_char].encode("utf-8"))


def utf16_span(line_text: str, start_char: int, end_char: int) -> tuple[int, int]:
    return (
        len(line_text[:start_char].encode("utf-16-le")) // 2,
        len(line_text[:end_char].encode("utf-16-le")) // 2,
    )


def make_workspace() -> Workspace:
    main_py = "hello = 'bad'\r\nprint('ok')\r\n"
    other_py = f"# emoji {EMOJI} line\r\nvalue = 1\r\n"
    return Workspace(
        {"main.py": main_py, "other.py": other_py},
        {"main.py": "v1", "other.py": "v1"},
    )


def pass_assertion(ctx):
    return True


class CoordinateTests(unittest.TestCase):
    def setUp(self):
        self.text = f"a{EMOJI}b\r\nx\r\n"

    def test_utf8_byte_offsets_round_trip(self):
        # emoji 占 4 个 UTF-8 字节，落在字符 1 上
        start, end = byte_span(self.text, 1, 2)
        self.assertEqual(end - start, 4)
        self.assertEqual(utf8_offsets_to_indices(self.text, start, end), (1, 2))

    def test_utf8_offset_inside_multibyte_char_rejected(self):
        start, end = byte_span(self.text, 1, 2)
        with self.assertRaises(SpanError):
            utf8_offsets_to_indices(self.text, start + 1, end)
        with self.assertRaises(SpanError):
            utf8_offsets_to_indices(self.text, start, end - 1)

    def test_utf8_offset_out_of_range_rejected(self):
        total = len(self.text.encode("utf-8"))
        with self.assertRaises(SpanError):
            utf8_offsets_to_indices(self.text, total, total + 1)

    def test_line_utf16_emoji_counts_as_two_code_units(self):
        lines = split_lines_preserving(self.text)
        line0 = lines[0].text
        start_unit, end_unit = utf16_span(line0, 1, 2)
        self.assertEqual(end_unit - start_unit, 2)
        self.assertEqual(
            line_utf16_to_indices(self.text, 0, start_unit, end_unit, lines),
            (1, 2),
        )

    def test_utf16_column_inside_surrogate_pair_rejected(self):
        lines = split_lines_preserving(self.text)
        line0 = lines[0].text
        start_unit, _ = utf16_span(line0, 1, 2)
        with self.assertRaises(SpanError):
            line_utf16_to_indices(self.text, 0, start_unit, start_unit + 1, lines)

    def test_crlf_is_one_line_boundary_and_preserved(self):
        lines = split_lines_preserving("a\r\nb\r\n")
        self.assertEqual([l.text for l in lines], ["a\r\n", "b\r\n"])
        joined = "".join(l.text for l in lines)
        self.assertEqual(joined, "a\r\nb\r\n")

    def test_unknown_line_rejected(self):
        with self.assertRaises(SpanError):
            line_utf16_to_indices("a\n", 5, 0, 1)

    def test_zero_width_span_rejected(self):
        text = "abc"
        start, end = byte_span(text, 1, 1)
        with self.assertRaises(SpanError):
            utf8_offsets_to_indices(text, start, end)


class MergeAcceptanceTests(unittest.TestCase):
    def test_same_edit_from_two_coordinate_systems_applies_once_with_crlf_and_emoji(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        e = s + len("'bad'")
        sb, eb = byte_span(text, s, e)

        utf8_group = {
            "group_id": "g-byte",
            "alert_id": "A1",
            "edits": [
                {
                    "file": "main.py",
                    "plugin": PLUGIN_UTF8_BYTES,
                    "start_byte": sb,
                    "end_byte": eb,
                    "old_text": "'bad'",
                    "new_text": "'good'",
                    "version": "v1",
                }
            ],
        }
        line_group = {
            "group_id": "g-line",
            "alert_id": "A2",
            "edits": [
                {
                    "file": "main.py",
                    "plugin": PLUGIN_LINE_UTF16,
                    "line": 0,
                    "start_utf16": s,
                    "end_utf16": e,
                    "old_text": "'bad'",
                    "new_text": "'good'",
                    "version": "v1",
                }
            ],
        }
        report = merge_suggestions(
            ws, [utf8_group, line_group], {"A1": pass_assertion, "A2": pass_assertion}
        )
        self.assertEqual(report.ok, True)
        self.assertEqual(sorted(report.applied_groups), ["g-byte", "g-line"])
        # 同一处替换只产生一次实际编辑
        self.assertEqual(len(report.applied_edits), 1)
        self.assertEqual(len(report.dedup), 1)
        self.assertEqual(report.dedup[0].merged_groups, ("g-line",))
        self.assertEqual(ws.files["main.py"], "hello = 'good'\r\nprint('ok')\r\n")
        # CRLF 原样保留
        self.assertIn("\r\n", ws.files["main.py"])
        self.assertNotIn("\n", ws.files["main.py"].replace("\r\n", ""))

    def test_emoji_edit_submitted_via_both_coordinates_applies_once(self):
        ws = make_workspace()
        text = ws.files["other.py"]
        lines = split_lines_preserving(text)
        line0 = lines[0].text
        s_rel = line0.index(EMOJI)
        e_rel = s_rel + 1
        s_abs = lines[0].start + s_rel
        e_abs = lines[0].start + e_rel
        sb, eb = byte_span(text, s_abs, e_abs)
        u_start, u_end = utf16_span(line0, s_rel, e_rel)

        groups = [
            {
                "group_id": "emoji-bytes",
                "alert_id": "E1",
                "edits": [
                    {
                        "file": "other.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": EMOJI,
                        "new_text": "OK",
                        "version": "v1",
                    }
                ],
            },
            {
                "group_id": "emoji-units",
                "alert_id": "E2",
                "edits": [
                    {
                        "file": "other.py",
                        "plugin": PLUGIN_LINE_UTF16,
                        "line": 0,
                        "start_utf16": u_start,
                        "end_utf16": u_end,
                        "old_text": EMOJI,
                        "new_text": "OK",
                        "version": "v1",
                    }
                ],
            },
        ]
        report = merge_suggestions(
            ws, groups, {"E1": pass_assertion, "E2": pass_assertion}
        )
        self.assertTrue(report.ok)
        self.assertEqual(len(report.applied_edits), 1)
        self.assertEqual(ws.files["other.py"], "# emoji OK line\r\nvalue = 1\r\n")

    def test_old_text_mismatch_withdraws_whole_group(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        e = s + 5
        sb, eb = byte_span(text, s, e)
        groups = [
            {
                "group_id": "g1",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'wrong'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            }
        ]
        report = merge_suggestions(ws, groups, {"A1": pass_assertion})
        self.assertEqual(report.applied_groups, [])
        self.assertEqual(len(report.withdrawals), 1)
        self.assertEqual(report.withdrawals[0].group_id, "g1")
        self.assertEqual(report.withdrawals[0].stage, "validate")
        # 源码未被改动
        self.assertEqual(ws.files["main.py"], text)

    def test_version_mismatch_withdraws_whole_multifile_group(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        e = s + 5
        sb, eb = byte_span(text, s, e)
        other = ws.files["other.py"]
        s2 = other.index("1")
        sb2, eb2 = byte_span(other, s2, s2 + 1)
        groups = [
            {
                "group_id": "g-multi",
                "alert_id": "A9",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    },
                    {
                        "file": "other.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb2,
                        "end_byte": eb2,
                        "old_text": "1",
                        "new_text": "2",
                        "version": "v-stale",  # 版本对不上
                    },
                ],
            }
        ]
        before = dict(ws.files)
        report = merge_suggestions(ws, groups, {"A9": pass_assertion})
        self.assertEqual(report.applied_groups, [])
        self.assertEqual(len(report.withdrawals), 1)
        self.assertIn("版本", report.withdrawals[0].reason)
        self.assertEqual(ws.files, before)

    def test_overlapping_different_changes_are_shelved(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        e = s + 5
        sb, eb = byte_span(text, s, e)
        # 第二组区间与第一组重叠（少一个字节）但替换内容不同
        sb2, eb2 = byte_span(text, s, e - 1)
        groups = [
            {
                "group_id": "g-a",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            },
            {
                "group_id": "g-b",
                "alert_id": "A2",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb2,
                        "end_byte": eb2,
                        "old_text": "'bad",
                        "new_text": "'B",
                        "version": "v1",
                    }
                ],
            },
        ]
        report = merge_suggestions(
            ws, groups, {"A1": pass_assertion, "A2": pass_assertion}
        )
        self.assertEqual(report.applied_groups, [])
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(
            sorted([report.conflicts[0].left_group, report.conflicts[0].right_group]),
            ["g-a", "g-b"],
        )
        withdrawn_ids = {w.group_id for w in report.withdrawals}
        self.assertEqual(withdrawn_ids, {"g-a", "g-b"})
        self.assertTrue(all(w.stage == "conflict" for w in report.withdrawals))
        self.assertEqual(ws.files["main.py"], text)

    def test_adjacent_non_overlapping_edits_both_apply(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        e = s + 5
        # 两个紧邻区间：[s, s+3) 与 [s+3, e)
        sb1, eb1 = byte_span(text, s, s + 3)
        sb2, eb2 = byte_span(text, s + 3, e)
        groups = [
            {
                "group_id": "g1",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb1,
                        "end_byte": eb1,
                        "old_text": "'ba",
                        "new_text": "'go",
                        "version": "v1",
                    }
                ],
            },
            {
                "group_id": "g2",
                "alert_id": "A2",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb2,
                        "end_byte": eb2,
                        "old_text": "d'",
                        "new_text": "od'",
                        "version": "v1",
                    }
                ],
            },
        ]
        report = merge_suggestions(
            ws, groups, {"A1": pass_assertion, "A2": pass_assertion}
        )
        self.assertTrue(report.ok)
        self.assertEqual(sorted(report.applied_groups), ["g1", "g2"])
        self.assertEqual(ws.files["main.py"], "hello = 'good'\r\nprint('ok')\r\n")

    def test_assertion_failure_rolls_back_only_its_cluster_and_records_alert(self):
        ws = make_workspace()
        main_text = ws.files["main.py"]
        s = main_text.index("'bad'")
        sb, eb = byte_span(main_text, s, s + 5)
        other_text = ws.files["other.py"]
        s2 = other_text.index("1")
        sb2, eb2 = byte_span(other_text, s2, s2 + 1)

        def fail_assertion(ctx):
            return False

        groups = [
            # 簇 1：main.py 上两个组共享文件
            {
                "group_id": "g-main-1",
                "alert_id": "A-MAIN-1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            },
            {
                "group_id": "g-main-2",
                "alert_id": "A-MAIN-2",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_LINE_UTF16,
                        "line": 1,
                        "start_utf16": 7,
                        "end_utf16": 9,
                        "old_text": "ok",
                        "new_text": "OK",
                        "version": "v1",
                    }
                ],
            },
            # 簇 2：独立文件 other.py，断言通过，应不受牵连
            {
                "group_id": "g-other",
                "alert_id": "A-OTHER",
                "edits": [
                    {
                        "file": "other.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb2,
                        "end_byte": eb2,
                        "old_text": "1",
                        "new_text": "2",
                        "version": "v1",
                    }
                ],
            },
        ]
        report = merge_suggestions(
            ws,
            groups,
            {
                "A-MAIN-1": fail_assertion,  # 簇 1 在此失效
                "A-MAIN-2": pass_assertion,
                "A-OTHER": pass_assertion,
            },
        )
        self.assertEqual(report.applied_groups, ["g-other"])
        rolled = {w.group_id for w in report.withdrawals if w.stage == "assertion"}
        self.assertEqual(rolled, {"g-main-1", "g-main-2"})
        # 记录触发失效的告警编号
        self.assertTrue(
            all(w.triggered_alert == "A-MAIN-1" for w in report.withdrawals)
        )
        self.assertIn("A-MAIN-1", report.recorded_alerts)
        self.assertIn("A-MAIN-2", report.recorded_alerts)
        self.assertIn("A-OTHER", report.recorded_alerts)
        # main.py 整体回滚，other.py 正常生效
        self.assertEqual(ws.files["main.py"], main_text)
        self.assertEqual(ws.files["other.py"], other_text.replace("1", "2", 1))
        # 分簇结果：两个簇
        self.assertEqual(len(report.clusters), 2)

    def test_assertion_exception_is_treated_as_failure(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        sb, eb = byte_span(text, s, s + 5)

        def boom(ctx):
            raise RuntimeError("protect!")

        groups = [
            {
                "group_id": "g1",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            }
        ]
        report = merge_suggestions(ws, groups, {"A1": boom})
        self.assertEqual(report.applied_groups, [])
        self.assertEqual(report.withdrawals[0].triggered_alert, "A1")
        self.assertIn("protect!", report.withdrawals[0].reason)
        self.assertEqual(ws.files["main.py"], text)

    def test_unregistered_alert_withdraws_cluster(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        sb, eb = byte_span(text, s, s + 5)
        groups = [
            {
                "group_id": "g1",
                "alert_id": "MISSING",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            }
        ]
        report = merge_suggestions(ws, groups, {})
        self.assertEqual(report.applied_groups, [])
        self.assertEqual(report.withdrawals[0].stage, "assertion")
        self.assertIn("MISSING", report.recorded_alerts)

    def test_transitive_shared_file_clustering_and_isolation(self):
        ws = Workspace(
            {"a.txt": "aaa", "b.txt": "bbbccc", "c.txt": "ccc"},
            {"a.txt": "v1", "b.txt": "v1", "c.txt": "v1"},
        )

        def span(text, old, at=0):
            s = text.index(old, at)
            return byte_span(text, s, s + len(old))

        sa, ea = span(ws.files["a.txt"], "aaa")
        # g1 改 b.txt 中的 bbb，g2 改同一文件后面的 ccc：区间不重叠但共享文件
        sb, eb = span(ws.files["b.txt"], "bbb")
        sb2, eb2 = span(ws.files["b.txt"], "ccc")
        sc, ec = span(ws.files["c.txt"], "ccc")
        groups = [
            {
                "group_id": "g1",
                "alert_id": "AL1",
                "edits": [
                    {"file": "a.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": sa,
                     "end_byte": ea, "old_text": "aaa", "new_text": "A1", "version": "v1"},
                    {"file": "b.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": sb,
                     "end_byte": eb, "old_text": "bbb", "new_text": "B1", "version": "v1"},
                ],
            },
            {
                "group_id": "g2",
                "alert_id": "AL2",
                "edits": [
                    {"file": "b.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": sb2,
                     "end_byte": eb2, "old_text": "ccc", "new_text": "X", "version": "v1"},
                ],
            },
            {
                "group_id": "g3",
                "alert_id": "AL3",
                "edits": [
                    {"file": "c.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": sc,
                     "end_byte": ec, "old_text": "ccc", "new_text": "C1", "version": "v1"},
                ],
            },
        ]
        # g1 与 g2 在 b.txt 的区间不重叠，但共享文件 -> 同簇；该簇断言失败
        def fail(ctx):
            return False

        report = merge_suggestions(
            ws, groups, {"AL1": fail, "AL2": pass_assertion, "AL3": pass_assertion}
        )
        self.assertEqual(report.applied_groups, ["g3"])
        self.assertEqual(ws.files["a.txt"], "aaa")
        self.assertEqual(ws.files["b.txt"], "bbbccc")
        self.assertEqual(ws.files["c.txt"], "C1")
        cluster_sets = [set(c) for c in report.clusters]
        self.assertIn({"g1", "g2"}, cluster_sets)
        self.assertIn({"g3"}, cluster_sets)

    def test_transitive_conflict_shelves_all_connected_groups(self):
        ws = Workspace({"f.txt": "0123456789"}, {"f.txt": "v1"})
        text = ws.files["f.txt"]

        def bs(s, e):
            return byte_span(text, s, e)

        groups = [
            {
                "group_id": "g1",
                "alert_id": "A1",
                "edits": [
                    {"file": "f.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": bs(0, 3)[0],
                     "end_byte": bs(0, 3)[1], "old_text": "012", "new_text": "AAA",
                     "version": "v1"}
                ],
            },
            {
                "group_id": "g2",
                "alert_id": "A2",
                "edits": [
                    {"file": "f.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": bs(2, 5)[0],
                     "end_byte": bs(2, 5)[1], "old_text": "234", "new_text": "BBB",
                     "version": "v1"}
                ],
            },
            {
                "group_id": "g3",
                "alert_id": "A3",
                "edits": [
                    {"file": "f.txt", "plugin": PLUGIN_UTF8_BYTES, "start_byte": bs(4, 7)[0],
                     "end_byte": bs(4, 7)[1], "old_text": "456", "new_text": "CCC",
                     "version": "v1"}
                ],
            },
        ]
        # g1 与 g2 冲突、g2 与 g3 冲突；g1 与 g3 不直接重叠但都被搁置
        report = merge_suggestions(
            ws, groups, {"A1": pass_assertion, "A2": pass_assertion, "A3": pass_assertion}
        )
        self.assertEqual(report.applied_groups, [])
        self.assertEqual({w.group_id for w in report.withdrawals}, {"g1", "g2", "g3"})
        self.assertEqual(ws.files["f.txt"], text)

    def test_group_atomicity_partial_failure_leaves_no_trace(self):
        ws = make_workspace()
        main_text = ws.files["main.py"]
        other_text = ws.files["other.py"]
        s = main_text.index("'bad'")
        sb, eb = byte_span(main_text, s, s + 5)
        groups = [
            {
                "group_id": "g-atomic",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    },
                    {
                        # 第二条旧文本对不上 -> 整组撤下，第一条也不得生效
                        "file": "other.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": 0,
                        "end_byte": 1,
                        "old_text": "!",
                        "new_text": "#",
                        "version": "v1",
                    },
                ],
            }
        ]
        report = merge_suggestions(ws, groups, {"A1": pass_assertion})
        self.assertEqual(report.applied_groups, [])
        self.assertEqual(len(report.withdrawals), 1)
        self.assertEqual(report.withdrawals[0].group_id, "g-atomic")
        self.assertEqual(ws.files["main.py"], main_text)
        self.assertEqual(ws.files["other.py"], other_text)

    def test_assertion_runs_on_temp_copy_not_real_workspace(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        sb, eb = byte_span(text, s, s + 5)
        seen = {}

        def inspect_temp(ctx):
            staged = ctx.read_temp("main.py")
            seen["staged"] = staged
            seen["temp_dir"] = ctx.temp_dir
            seen["temp_path_under_tmp"] = os.path.realpath(ctx.temp_dir).startswith(
                os.path.realpath(tempfile.gettempdir())
            )
            return True

        groups = [
            {
                "group_id": "g1",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            }
        ]
        report = merge_suggestions(ws, groups, {"A1": inspect_temp})
        self.assertEqual(report.applied_groups, ["g1"])
        self.assertIn("'good'", seen["staged"])
        self.assertTrue(seen["temp_path_under_tmp"])
        # 断言执行期间临时副本存在；结束后已清理
        self.assertFalse(os.path.isdir(seen.get("temp_path", "")))

    def test_output_directory_preserves_crlf_on_disk(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        sb, eb = byte_span(text, s, s + 5)
        groups = [
            {
                "group_id": "g1",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": sb,
                        "end_byte": eb,
                        "old_text": "'bad'",
                        "new_text": "'good'",
                        "version": "v1",
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as out:
            report = merge_suggestions(
                ws, groups, {"A1": pass_assertion}, output_dir=out
            )
            self.assertEqual(report.output_dir, os.path.abspath(out))
            with open(os.path.join(out, "main.py"), "rb") as fh:
                raw = fh.read()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_unknown_file_and_bad_coordinates_withdraw_group(self):
        ws = make_workspace()
        groups = [
            {
                "group_id": "g-missing",
                "alert_id": "A1",
                "edits": [
                    {
                        "file": "nope.txt",
                        "plugin": PLUGIN_UTF8_BYTES,
                        "start_byte": 0,
                        "end_byte": 1,
                        "old_text": "x",
                        "new_text": "y",
                        "version": "v1",
                    }
                ],
            }
        ]
        report = merge_suggestions(ws, groups, {"A1": pass_assertion})
        self.assertEqual(len(report.withdrawals), 1)
        self.assertIn("不在工作区", report.withdrawals[0].reason)

        groups2 = [
            {
                "group_id": "g-bad-span",
                "alert_id": "A2",
                "edits": [
                    {
                        "file": "main.py",
                        "plugin": PLUGIN_LINE_UTF16,
                        "line": 99,
                        "start_utf16": 0,
                        "end_utf16": 1,
                        "old_text": "x",
                        "new_text": "y",
                        "version": "v1",
                    }
                ],
            }
        ]
        report2 = merge_suggestions(ws, groups2, {"A2": pass_assertion})
        self.assertEqual(len(report2.withdrawals), 1)
        self.assertEqual(report2.withdrawals[0].stage, "validate")

    def test_malformed_input_raises_value_error(self):
        ws = make_workspace()
        with self.assertRaises(ValueError):
            merge_suggestions(ws, [], {})
        with self.assertRaises(ValueError):
            merge_suggestions(ws, [{"group_id": "x"}], {})
        with self.assertRaises(ValueError):
            merge_suggestions(
                ws,
                [
                    {
                        "group_id": "g",
                        "alert_id": "A",
                        "edits": [
                            {
                                "file": "main.py",
                                "plugin": "weird",
                                "old_text": "a",
                                "new_text": "b",
                                "version": "v1",
                            }
                        ],
                    }
                ],
                {},
            )

    def test_duplicate_group_id_raises(self):
        ws = make_workspace()
        text = ws.files["main.py"]
        s = text.index("'bad'")
        sb, eb = byte_span(text, s, s + 5)
        edit = {
            "file": "main.py",
            "plugin": PLUGIN_UTF8_BYTES,
            "start_byte": sb,
            "end_byte": eb,
            "old_text": "'bad'",
            "new_text": "'good'",
            "version": "v1",
        }
        group = {"group_id": "dup", "alert_id": "A1", "edits": [edit]}
        with self.assertRaises(ValueError):
            merge_suggestions(ws, [group, dict(group, alert_id="A2")], {})

    def test_from_directory_round_trip_with_crlf(self):
        with tempfile.TemporaryDirectory() as src:
            path = os.path.join(src, "sub", "f.txt")
            os.makedirs(os.path.dirname(path))
            with open(path, "wb") as fh:
                fh.write("a\r\nb\r\n".encode("utf-8"))
            ws = Workspace.from_directory(src)
            self.assertEqual(ws.files, {os.path.join("sub", "f.txt"): "a\r\nb\r\n"})
            with tempfile.TemporaryDirectory() as out:
                ws.write_to(out)
                with open(os.path.join(out, "sub", "f.txt"), "rb") as fh:
                    self.assertEqual(fh.read(), b"a\r\nb\r\n")

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            os.makedirs(src)
            main_py = "hello = 'bad'\r\n"
            with open(os.path.join(src, "main.py"), "w", encoding="utf-8", newline="") as fh:
                fh.write(main_py)
            s = main_py.index("'bad'")
            sb, eb = byte_span(main_py, s, s + 5)
            suggestions = [
                {
                    "group_id": "g1",
                    "alert_id": "A1",
                    "edits": [
                        {
                            "file": "main.py",
                            "plugin": PLUGIN_UTF8_BYTES,
                            "start_byte": sb,
                            "end_byte": eb,
                            "old_text": "'bad'",
                            "new_text": "'good'",
                            "version": "v1",
                        }
                    ],
                }
            ]
            sug_path = os.path.join(tmp, "sug.json")
            with open(sug_path, "w", encoding="utf-8") as fh:
                json.dump(suggestions, fh)
            ver_path = os.path.join(tmp, "versions.json")
            with open(ver_path, "w", encoding="utf-8") as fh:
                json.dump({"main.py": "v1"}, fh)
            assertions_dir = os.path.join(tmp, "asrt")
            os.makedirs(assertions_dir)
            with open(os.path.join(assertions_dir, "my_assertions.py"), "w", encoding="utf-8") as fh:
                fh.write("ASSERTIONS = {'A1': lambda ctx: True}\n")
            out = os.path.join(tmp, "out")
            # 不使用 -I：需要通过 PYTHONPATH 定位工作区包与临时断言模块；
            # 子进程 cwd 为临时目录，避免加载工作区中的同名模块。
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([os.getcwd(), assertions_dir]))
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "audit_merge",
                    "--source",
                    src,
                    "--suggestions",
                    sug_path,
                    "--assertions",
                    "my_assertions",
                    "--versions",
                    ver_path,
                    "--output",
                    out,
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=tmp,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["applied_groups"], ["g1"])
            with open(os.path.join(out, "main.py"), "rb") as fh:
                self.assertEqual(fh.read(), b"hello = 'good'\r\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
