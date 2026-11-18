"""归属模块验收测试：覆盖可接受结果、应被拒绝结果及边界/交错场景。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from traceattribution import (  # noqa: E402
    Status,
    TraceConfig,
    attribute,
    extract_body,
    protect_pack,
    protect_text,
    scan_file,
)

KEY = b"round1-acceptance-key"
BATCH = "B2026-11"
COPY_A = "C01"
COPY_B = "C02"

#: 位置槽 0..3 按文件顺序轮转分配：intro->0, setup->1, notes->2, faq->3
SOURCE_FILES = {
    "intro.md": "# 培训手册\n第一章 概述\n本章介绍系统边界。\n",
    "setup.py": "import os\n\n\ndef main():\n    return os.name\n",
    "notes.txt": "要点一\n要点二\n要点三\n要点四\n",
    "faq.md": "# 常见问题\n问：如何安装？\n答：见第二章。\n",
}


def make_config(**overrides):
    params = dict(key=KEY, positions=4, threshold=3)
    params.update(overrides)
    return TraceConfig(**params)


class AttributionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.pack_a = protect_pack(SOURCE_FILES, BATCH, COPY_A, self.config)
        self.pack_b = protect_pack(SOURCE_FILES, BATCH, COPY_B, self.config)

    # ---- 可接受结果 -------------------------------------------------

    def test_confirm_with_unordered_subset_and_duplicates(self):
        """随意排序、混入重复文件，集满阈值的不同位置 -> 确认归属。"""
        evidence = [
            ("random-name-7", self.pack_a["notes.txt"]),   # pos 2
            ("random-name-3", self.pack_a["intro.md"]),    # pos 0
            ("dup-of-notes", self.pack_a["notes.txt"]),    # 重复，只计一次
            ("random-name-9", self.pack_a["faq.md"]),      # pos 3
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.CONFIRMED)
        self.assertEqual(result.batch, BATCH)
        self.assertEqual(result.copy_no, COPY_A)
        self.assertEqual(result.positions, frozenset({0, 2, 3}))
        self.assertEqual(result.files_seen, 4)
        self.assertEqual(result.files_unique, 3)

    def test_body_content_preserved_after_protection(self):
        """写入标记后正文内容保持一致，且写入结果确定。"""
        for name, original in SOURCE_FILES.items():
            protected = self.pack_a[name]
            self.assertNotEqual(protected, original)  # 确实写入了标记
            self.assertEqual(extract_body(protected), original)
        again = protect_pack(SOURCE_FILES, BATCH, COPY_A, self.config)
        self.assertEqual(again, self.pack_a)  # 确定性

    # ---- 应被阻止/拒绝的结果 ---------------------------------------

    def test_conflict_when_two_copies_mixed(self):
        """混有两个副本的有效标记 -> 冲突。"""
        evidence = [
            ("a1", self.pack_a["intro.md"]),
            ("a2", self.pack_a["notes.txt"]),
            ("b1", self.pack_b["faq.md"]),
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.CONFLICT)
        self.assertEqual(
            result.identities, frozenset({(BATCH, COPY_A), (BATCH, COPY_B)})
        )

    def test_insufficient_when_below_threshold(self):
        """不同位置数未达阈值 -> 证据不足。"""
        evidence = [
            ("f1", self.pack_a["intro.md"]),   # pos 0
            ("f2", self.pack_a["setup.py"]),   # pos 1
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.INSUFFICIENT)
        self.assertEqual(result.positions, frozenset({0, 1}))

    def test_tampered_body_fails_integrity(self):
        """正文被篡改后完整性校验失败，标记无效 -> 证据不足。"""
        tampered = {
            name: text.replace("概述", "改述", 1) if name == "intro.md" else text
            for name, text in self.pack_a.items()
        }
        tampered["notes.txt"] = tampered["notes.txt"].replace("要点二", "要点X", 1)
        result = attribute(tampered, self.config)
        # 仅剩 setup.py(pos1)、faq.md(pos3) 两个有效位置，低于阈值 3
        self.assertEqual(result.status, Status.INSUFFICIENT)
        self.assertEqual(result.positions, frozenset({1, 3}))
        self.assertEqual(result.invalid_markers, 2)

    def test_wrong_key_rejects_all_markers(self):
        """核查密钥不匹配时所有标记无效 -> 证据不足（归属被阻止）。"""
        result = attribute(self.pack_a, make_config(key=b"another-key"))
        self.assertEqual(result.status, Status.INSUFFICIENT)
        self.assertEqual(result.valid_markers, 0)
        self.assertEqual(result.invalid_markers, 4)

    # ---- 边界与交错场景 ---------------------------------------------

    def test_boundary_exact_threshold_confirms(self):
        """边界：不同位置数恰好等于阈值 -> 确认。"""
        evidence = [
            ("f0", self.pack_a["intro.md"]),   # pos 0
            ("f1", self.pack_a["setup.py"]),   # pos 1
            ("f2", self.pack_a["notes.txt"]),  # pos 2
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.CONFIRMED)
        self.assertEqual(result.positions, frozenset({0, 1, 2}))

    def test_duplicates_counted_once(self):
        """同一文件重复多次不累积位置数，仍证据不足。"""
        evidence = [("copy-%d" % i, self.pack_a["intro.md"]) for i in range(6)]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.INSUFFICIENT)
        self.assertEqual(result.files_seen, 6)
        self.assertEqual(result.files_unique, 1)
        self.assertEqual(result.positions, frozenset({0}))

    def test_conflict_even_when_one_copy_meets_threshold(self):
        """交错：副本 A 已集满阈值，但混入副本 B 一个有效标记 -> 仍冲突。"""
        evidence = [
            ("a0", self.pack_a["intro.md"]),
            ("a1", self.pack_a["setup.py"]),
            ("a2", self.pack_a["notes.txt"]),
            ("b0", self.pack_b["faq.md"]),
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.CONFLICT)
        self.assertEqual(
            result.identities, frozenset({(BATCH, COPY_A), (BATCH, COPY_B)})
        )

    def test_interleaved_invalid_markers_do_not_block_valid_attribution(self):
        """交错：有效 A 标记与已篡改（无效）的 B 标记混合 -> 无效标记被丢弃，确认 A。"""
        tampered_b = self.pack_b["faq.md"].replace("如何安装", "如何卸载", 1)
        evidence = [
            ("x1", tampered_b),                  # 完整性失败，不计入
            ("x2", self.pack_a["intro.md"]),
            ("x3", self.pack_a["setup.py"]),
            ("x4", self.pack_a["notes.txt"]),
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.CONFIRMED)
        self.assertEqual(result.copy_no, COPY_A)
        self.assertEqual(result.invalid_markers, 1)

    def test_malformed_marker_lines_are_ignored(self):
        """格式非法的标记行不影响归属判定，仅计入 invalid。"""
        injected = self.pack_a["intro.md"] + "# @trace1 batch=broken\n"
        evidence = [
            ("f0", injected),
            ("f1", self.pack_a["setup.py"]),
            ("f2", self.pack_a["notes.txt"]),
        ]
        result = attribute(evidence, self.config)
        self.assertEqual(result.status, Status.CONFIRMED)
        self.assertEqual(result.invalid_markers, 1)

    def test_no_markers_is_insufficient(self):
        """完全无标记的素材 -> 证据不足。"""
        result = attribute(SOURCE_FILES, self.config)
        self.assertEqual(result.status, Status.INSUFFICIENT)
        self.assertEqual(result.valid_markers, 0)
        self.assertIsNone(result.batch)


class ProtectionGuardTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_protect_rejects_body_with_marker_like_lines(self):
        """正文已含标记保留位行时拒绝写入，防止归属歧义。"""
        body = "标题\n# @trace1 batch=X copy=Y pos=0 mac=" + "0" * 64 + "\n"
        with self.assertRaises(ValueError):
            protect_text(body, BATCH, COPY_A, [0], self.config)

    def test_protect_rejects_out_of_range_slot(self):
        with self.assertRaises(ValueError):
            protect_text("正文\n", BATCH, COPY_A, [4], self.config)

    def test_config_rejects_invalid_threshold(self):
        with self.assertRaises(ValueError):
            make_config(threshold=5)  # threshold > positions

    def test_scan_file_parses_markers(self):
        protected = protect_text("第一行\n第二行\n", BATCH, COPY_A, [0, 3], self.config)
        scanned = scan_file(protected)
        self.assertEqual([m.pos for m in scanned.markers], [0, 3])
        self.assertEqual(scanned.malformed, 0)
        self.assertEqual(extract_body(protected), "第一行\n第二行\n")


if __name__ == "__main__":
    unittest.main()
