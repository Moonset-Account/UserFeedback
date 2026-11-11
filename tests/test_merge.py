"""离线归并场景验证测试。

运行方式（隔离模式，避免数据目录中的同名模块劫持）：
    python3 -I -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crash_merge.cli import run_merge
from crash_merge.mapping import BuildMappingIndex, MappingError, normalize_address
from crash_merge.clustering import cluster
from crash_merge.resolution import resolve_report

DATA = ROOT / "data"


def load_json(name: str):
    with (DATA / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


class AddressNormalizationTests(unittest.TestCase):
    def test_hex_variants_normalize_equally(self):
        # 同一地址的不同书写宽度/大小写必须归一化一致
        self.assertEqual(normalize_address("0x00010a34f20"), normalize_address("0X10A34F20"))
        self.assertEqual(normalize_address(0x10A34F20), "0x10a34f20")

    def test_invalid_address(self):
        self.assertIsNone(normalize_address(""))
        self.assertIsNone(normalize_address("0xzz"))
        self.assertIsNone(normalize_address(None))


class MergeScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports_doc = load_json("sample_reports.json")
        cls.mapping_doc = load_json("sample_build_mapping.json")
        cls.result = run_merge(DATA / "sample_reports.json", DATA / "sample_build_mapping.json")

    def _group_by_id(self):
        return {g["group_id"]: g for g in self.result["groups"]}

    def test_overall_counts(self):
        summary = self.result["summary"]
        self.assertEqual(summary["total_reports"], 12)
        self.assertEqual(summary["group_count"], 3)
        self.assertEqual(summary["grouped_reports"], 8)
        self.assertEqual(summary["pending_review_count"], 4)
        self.assertEqual(
            summary["pending_reason_counts"],
            {"AMBIGUOUS_MAPPING": 1, "MISSING_KEY_FRAME": 1, "MISSING_MAPPING": 2},
        )

    def test_same_defect_across_builds_merges(self):
        # R01(b100)/R02(b101)/R03(b102)/R06(b100) 是同一 use-after-free：
        # 地址完全不同、b102 的中间内联帧符号变化，仍应归为一组
        uaf_groups = [
            g for g in self.result["groups"] if g["fault_category"] == "use-after-free"
        ]
        self.assertEqual(len(uaf_groups), 1)
        group = uaf_groups[0]
        member_ids = sorted(m["report_id"] for m in group["members"])
        self.assertEqual(member_ids, ["R01", "R02", "R03", "R06"])
        self.assertEqual(len(group["builds"]), 3)
        self.assertEqual(
            group["stable_attribution"],
            {
                "source_file": "net/socket_reader.cc",
                "owning_symbol": "mozilla::net::SocketReader::Drain",
                "source_line": 212,
            },
        )
        # 成员间原始地址确实不同，证明归并不依赖地址
        addresses = {m["raw_address"] for m in group["members"]}
        self.assertGreater(len(addresses), 1)
        # R03 的内联深度结构与其他构建不同但最内层归属一致
        r03 = next(m for m in group["members"] if m["report_id"] == "R03")
        self.assertEqual(
            [f["symbol"] for f in r03["resolved_chain"]][1],
            "mozilla::net::detail::FlushQueue",
        )

    def test_same_source_location_different_faults_split(self):
        # R04/R05 (heap-buffer-overflow) 与 R07/R08 (stack-buffer-overflow)
        # 稳定源码归属完全相同，仅故障类别不同 -> 必须各自成组
        media_groups = [
            g
            for g in self.result["groups"]
            if g["stable_attribution"]["source_file"] == "media/frame_buffer.cc"
        ]
        self.assertEqual(len(media_groups), 2)
        categories = {g["fault_category"] for g in media_groups}
        self.assertEqual(categories, {"heap-buffer-overflow", "stack-buffer-overflow"})
        for group in media_groups:
            self.assertEqual(group["stable_attribution"]["source_line"], 88)
            self.assertEqual(group["member_count"], 2)
            # 同组跨构建
            self.assertEqual(len(group["builds"]), 2)

    def test_missing_key_frame_pending(self):
        # R09 崩溃线程没有任何应用帧 -> 保留待核查，不进入任何分组
        pending = {p["report_id"]: p for p in self.result["pending_review"]}
        self.assertEqual(pending["R09"]["reason_code"], "MISSING_KEY_FRAME")
        grouped_ids = {m["report_id"] for g in self.result["groups"] for m in g["members"]}
        self.assertNotIn("R09", grouped_ids)

    def test_missing_mapping_pending(self):
        pending = {p["report_id"]: p for p in self.result["pending_review"]}
        # R10：构建标识在映射中不存在
        self.assertEqual(pending["R10"]["reason_code"], "MISSING_MAPPING")
        self.assertIn("build-b999-unknown", pending["R10"]["reason_detail"])
        # R12：构建已知但地址无映射条目
        self.assertEqual(pending["R12"]["reason_code"], "MISSING_MAPPING")
        self.assertIn("0x55f0deadbeef", pending["R12"]["reason_detail"])

    def test_ambiguous_mapping_pending(self):
        # R11：同一地址挂了两条互不一致的稳定归属链 -> 多义，证据不足
        pending = {p["report_id"]: p for p in self.result["pending_review"]}
        self.assertEqual(pending["R11"]["reason_code"], "AMBIGUOUS_MAPPING")
        self.assertIn("socket_reader.cc", pending["R11"]["reason_detail"])
        self.assertIn("http_transaction.cc", pending["R11"]["reason_detail"])

    def test_group_keeps_representative_and_reviewable_rationale(self):
        for group in self.result["groups"]:
            # 代表记录存在且属于成员集合
            self.assertIn(
                group["representative_report_id"],
                [m["report_id"] for m in group["members"]],
            )
            rep = group["representative_evidence"]
            self.assertEqual(rep["report_id"], group["representative_report_id"])
            self.assertTrue(rep["resolved_chain"])
            # 归并理由可复核：解释归并键、帧选择与构建覆盖
            rationale = "\n".join(group["merge_rationale"])
            self.assertIn(group["fault_category"], rationale)
            self.assertIn(group["stable_attribution"]["source_file"], rationale)
            self.assertIn("首个相关应用帧", rationale)
            self.assertTrue(group["group_id"].startswith("GRP-"))

    def test_corroboration_flag_distinguishes_missing_inline_info(self):
        # R06 报告自带内联层次为空 -> 映射仍可解析，但 report_corroborated=false
        uaf = next(
            g for g in self.result["groups"] if g["fault_category"] == "use-after-free"
        )
        flags = {m["report_id"]: m["report_corroborated"] for m in uaf["members"]}
        self.assertFalse(flags["R06"])
        self.assertTrue(flags["R01"])
        self.assertTrue(flags["R02"])
        self.assertTrue(flags["R03"])
        # 理由中必须点名未印证成员
        rationale = "\n".join(uaf["merge_rationale"])
        self.assertIn("R06", rationale)

    def test_deterministic_output(self):
        again = run_merge(DATA / "sample_reports.json", DATA / "sample_build_mapping.json")
        self.assertEqual(
            json.dumps(self.result, sort_keys=True, ensure_ascii=False),
            json.dumps(again, sort_keys=True, ensure_ascii=False),
        )


class CandidateSymbolCrossCheckTests(unittest.TestCase):
    def test_unmatched_candidate_symbols_is_missing_mapping(self):
        reports_doc = load_json("sample_reports.json")
        index = BuildMappingIndex(load_json("sample_build_mapping.json"))
        report = json.loads(json.dumps(reports_doc["reports"][0]))  # R01 深拷贝
        report["report_id"] = "RX"
        # 候选符号与映射链完全无交集
        report["crash_thread"]["frames"][1]["candidate_symbols"] = ["unrelated::BogusSymbol"]
        resolution = resolve_report(report, index)
        self.assertIsNone(resolution.evidence)
        self.assertEqual(resolution.pending_reason, "MISSING_MAPPING")

    def test_invalid_mapping_document_rejected(self):
        with self.assertRaises(MappingError):
            BuildMappingIndex({"builds": [{"module_build_id": "x"}]})

    def test_duplicate_report_id_flagged_invalid(self):
        reports_doc = load_json("sample_reports.json")
        index = BuildMappingIndex(load_json("sample_build_mapping.json"))
        reports = reports_doc["reports"][:2]
        duplicate = json.loads(json.dumps(reports[1]))
        duplicate["report_id"] = reports[0]["report_id"]
        resolutions = [resolve_report(r, index) for r in [*reports, duplicate]]
        result = cluster(resolutions, [*reports, duplicate])
        ids = {p["report_id"]: p["reason_code"] for p in result["pending_review"]}
        self.assertEqual(ids[reports[0]["report_id"]], "INVALID_REPORT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
