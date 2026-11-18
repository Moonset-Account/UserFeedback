"""nonce_audit 的验收测试（unittest）。

覆盖题面的交叉约束：
* 相同操作重复上报只算一次（去重 + 冲突待确认）；
* 超时后重新执行、写入编号相同但操作编号不同的加密独立核对（应报复用）；
* 同一材料通过两个密钥别名使用时合并判断（应报复用）；
* 成功换钥前后版本分开（同 nonce 也不得配对）；
* 映射缺失返回待确认，并关联无法定论的相邻操作；
* 输出不回传密钥别名/材料标识等密钥相关内容。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nonce_audit import (  # noqa: E402
    AuditInputError,
    EncryptionRecord,
    EquivalenceTable,
    audit,
)


def build_equivalence() -> EquivalenceTable:
    # v1：alias-a 与 alias-b 背后是同一份密钥材料；alias-c 独立。
    # v2：换钥成功后的新版本，alias-a/alias-b 指向新材料，仍彼此等价。
    return EquivalenceTable.from_alias_groups(
        {
            "v1": [["alias-a", "alias-b"], ["alias-c"]],
            "v2": [["alias-a", "alias-b"], ["alias-c"]],
        }
    )


def rec(op, write, nonce, alias, version, source):
    return EncryptionRecord(
        operation_id=op,
        logical_write_id=write,
        nonce=nonce,
        key_alias=alias,
        mapping_version=version,
        source_id=source,
    )


class NonceAuditAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.table = build_equivalence()
        self.records = [
            # 1) 同操作 op-1 被两个来源重复上报、内容一致 -> 只算一次，无结论。
            rec("op-1", "w-1", "n-1", "alias-a", "v1", "receipt-1"),
            rec("op-1", "w-1", "n-1", "alias-a", "v1", "receipt-2"),
            # 2) 同材料两个别名 + 同 nonce -> 合并判断，需复核 (op-2, op-3)。
            rec("op-2", "w-2", "n-shared", "alias-a", "v1", "receipt-1"),
            rec("op-3", "w-3", "n-shared", "alias-b", "v1", "receipt-2"),
            # 3) 超时重执行：相同写入 w-4、相同 nonce、不同操作编号 -> 独立核对，
            #    需复核 (op-4, op-5)。
            rec("op-4", "w-4", "n-retry", "alias-a", "v1", "receipt-1"),
            rec("op-5", "w-4", "n-retry", "alias-a", "v1", "receipt-3"),
            # 4) 换钥后同别名同 nonce，但版本不同 -> 不得配对。
            rec("op-6", "w-5", "n-rotated", "alias-a", "v1", "receipt-1"),
            rec("op-7", "w-6", "n-rotated", "alias-a", "v2", "receipt-4"),
            # 5) 换钥后 v2 内部的两个别名等价复用 -> 需复核 (op-8, op-9)。
            rec("op-8", "w-7", "n-v2", "alias-a", "v2", "receipt-4"),
            rec("op-9", "w-8", "n-v2", "alias-b", "v2", "receipt-4"),
            # 6) 映射缺失：v1 中不存在 alias-x -> 待确认；
            #    op-11 与它同版本同 nonce，无法确认是否也构成复用 -> 一并关联。
            rec("op-10", "w-9", "n-unknown", "alias-x", "v1", "receipt-5"),
            rec("op-11", "w-10", "n-unknown", "alias-a", "v1", "receipt-1"),
            # 7) 重复上报但内容冲突（nonce 不一致）-> 该操作待确认，不参与配对。
            rec("op-12", "w-11", "n-conflict-a", "alias-a", "v1", "receipt-1"),
            rec("op-12", "w-11", "n-conflict-b", "alias-a", "v1", "receipt-6"),
            # 8) 独立材料 alias-c 即使同 nonce 也不得与 alias-a 配对。
            rec("op-13", "w-12", "n-mat", "alias-c", "v1", "receipt-2"),
            rec("op-14", "w-13", "n-mat", "alias-a", "v1", "receipt-1"),
        ]
        self.result = audit(self.records, self.table)

    def _pair_keys(self):
        return {
            (pair.operation_id_a, pair.operation_id_b)
            for pair in self.result.reuse_pairs
        }

    def test_acceptable_reuse_pairs_are_reported(self):
        # 可接受的核查结果：三类真实复用均被识别。
        self.assertEqual(
            self._pair_keys(),
            {("op-2", "op-3"), ("op-4", "op-5"), ("op-8", "op-9")},
        )

    def test_alias_equivalence_merges_material(self):
        pair = next(
            pair for pair in self.result.reuse_pairs
            if (pair.operation_id_a, pair.operation_id_b) == ("op-2", "op-3")
        )
        self.assertEqual(pair.nonce, "n-shared")
        self.assertEqual(pair.mapping_version, "v1")
        self.assertEqual(pair.logical_write_ids, ("w-2", "w-3"))
        self.assertEqual(pair.source_ids, ("receipt-1", "receipt-2"))

    def test_timeout_retry_with_same_write_id_is_independently_checked(self):
        # 边界/交错场景：相同逻辑写入编号的两次实际加密必须独立核对并报出。
        pair = next(
            p for p in self.result.reuse_pairs
            if (p.operation_id_a, p.operation_id_b) == ("op-4", "op-5")
        )
        self.assertEqual(pair.logical_write_ids, ("w-4",))
        self.assertEqual(pair.nonce, "n-retry")
        self.assertEqual(pair.mapping_version, "v1")

    def test_duplicate_reports_count_once(self):
        # op-1 经两个来源重复上报、业务字段一致 -> 正常去重，不产生任何操作对；
        # 去重计数为 2（op-1 与 op-12 各有一条重复上报）。
        self.assertEqual(self.result.deduplicated_reports, 2)
        self.assertEqual(self.result.unique_operations, 14)
        reported = {
            op for pair in self.result.reuse_pairs
            for op in (pair.operation_id_a, pair.operation_id_b)
        }
        self.assertNotIn("op-1", reported)
        pending_ops = {item.operation_id for item in self.result.pending}
        self.assertNotIn("op-1", pending_ops)

    def test_key_rotation_records_are_separated(self):
        # 应被阻止的结果：换钥前后（v1/v2）即使同 nonce 也不得配对。
        self.assertNotIn(("op-6", "op-7"), self._pair_keys())
        for pair in self.result.reuse_pairs:
            self.assertNotIn(
                (pair.operation_id_a, pair.operation_id_b),
                {("op-6", "op-7"), ("op-7", "op-6")},
            )

    def test_distinct_materials_are_not_paired(self):
        # 应被阻止的结果：不同密钥材料即使 nonce 相同也不配对。
        self.assertNotIn(("op-13", "op-14"), self._pair_keys())

    def test_missing_mapping_returns_pending_with_peers(self):
        pending_by_op = {item.operation_id: item for item in self.result.pending}
        missing = pending_by_op["op-10"]
        self.assertEqual(missing.reason, "mapping_missing")
        self.assertEqual(missing.mapping_version, "v1")
        self.assertEqual(missing.nonce, "n-unknown")
        # 别名未在 v1 中登记，仅回传布尔标记，不回传别名原文。
        self.assertFalse(missing.key_alias_present)
        # 同版本同 nonce 的相邻操作无法确认，关联到待确认项中。
        self.assertEqual(missing.peer_operation_ids, ("op-11",))
        # op-11 因相邻关系无法定论，本身不进入复用对。
        reported = {
            op for pair in self.result.reuse_pairs
            for op in (pair.operation_id_a, pair.operation_id_b)
        }
        self.assertNotIn("op-11", reported)

    def test_conflicting_duplicate_is_pending_and_excluded(self):
        pending_by_op = {item.operation_id: item for item in self.result.pending}
        conflict = pending_by_op["op-12"]
        self.assertEqual(conflict.reason, "duplicate_conflict")
        self.assertEqual(conflict.source_ids, ("receipt-1", "receipt-6"))
        self.assertTrue(conflict.key_alias_present)
        reported = {
            op for pair in self.result.reuse_pairs
            for op in (pair.operation_id_a, pair.operation_id_b)
        }
        self.assertNotIn("op-12", reported)

    def test_output_never_contains_key_material_or_aliases(self):
        # 安全约束：序列化输出不得包含别名、材料标识原文，只允许不透明类标识。
        serialized = repr(self.result.to_dict())
        for secret_like in ("alias-a", "alias-b", "alias-c", "material-v1",
                            "material-v2", "alias-x"):
            self.assertNotIn(secret_like, serialized)
        for pair in self.result.reuse_pairs:
            self.assertTrue(pair.key_class.startswith("material-class-"))

    def test_key_class_stable_and_scoped_by_version(self):
        # 同一 (版本, 材料) 的复用对共享不透明类标识；不同版本编号独立。
        classes = {
            (pair.mapping_version, pair.key_class)
            for pair in self.result.reuse_pairs
        }
        v1_classes = {c for v, c in classes if v == "v1"}
        v2_classes = {c for v, c in classes if v == "v2"}
        self.assertEqual(len(v1_classes), 1)
        self.assertEqual(len(v2_classes), 1)
        # 换钥后即使别名相同，也是不同的不透明材料类。
        self.assertNotEqual(v1_classes.pop(), v2_classes.pop())


class DictInputAndErrorTest(unittest.TestCase):
    def test_dict_records_and_dict_table_are_accepted(self):
        table = {
            "v1": {"k1": "mat-1", "k2": "mat-1"},
        }
        records = [
            {
                "operation_id": "o1",
                "logical_write_id": "w1",
                "nonce": "n1",
                "key_alias": "k1",
                "mapping_version": "v1",
                "source_id": "s1",
            },
            {
                "operation_id": "o2",
                "logical_write_id": "w2",
                "nonce": "n1",
                "key_alias": "k2",
                "mapping_version": "v1",
                "source_id": "s2",
            },
        ]
        result = audit(records, table)
        self.assertEqual(len(result.reuse_pairs), 1)
        self.assertEqual(
            (result.reuse_pairs[0].operation_id_a,
             result.reuse_pairs[0].operation_id_b),
            ("o1", "o2"),
        )

    def test_missing_version_is_treated_as_pending(self):
        # 整个版本缺失与别名缺失一样，安全地返回待确认而非抛错。
        table = EquivalenceTable.from_alias_groups({"v1": [["alias-a"]]})
        records = [
            rec("op-x", "w-x", "n-x", "alias-a", "v9", "src"),
        ]
        result = audit(records, table)
        self.assertEqual(len(result.reuse_pairs), 0)
        self.assertEqual(len(result.pending), 1)
        self.assertEqual(result.pending[0].reason, "mapping_missing")
        self.assertFalse(result.pending[0].key_alias_present)

    def test_empty_input(self):
        table = EquivalenceTable.from_alias_groups({})
        result = audit([], table)
        self.assertEqual(result.reuse_pairs, ())
        self.assertEqual(result.pending, ())
        self.assertFalse(result.has_findings())
        self.assertEqual(result.to_dict()["reuse_pairs"], [])

    def test_malformed_record_raises(self):
        table = EquivalenceTable.from_alias_groups({"v1": [["alias-a"]]})
        with self.assertRaises(AuditInputError):
            audit(
                [{"operation_id": "o1"}],  # 缺少其余必填字段
                table,
            )
        with self.assertRaises(AuditInputError):
            audit(["not-a-record"], table)  # 非法类型


if __name__ == "__main__":
    unittest.main(verbosity=2)
