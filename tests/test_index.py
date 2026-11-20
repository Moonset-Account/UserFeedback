"""``PortCompositeIndex`` 的本地合成数据单元测试。

所有数据均为测试内手工构造的普通字典，不依赖任何外部资源。
"""

import unittest

from port_index import PortCompositeIndex


def doc(doc_id, ports):
    """便捷构造文档。ports 形如 [("west", 1000), ...]。"""
    return {
        "id": doc_id,
        "ports": [{"room": room, "speed": speed} for room, speed in ports],
    }


class PortIndexStoryTest(unittest.TestCase):
    """题面描述的完整场景：西区慢口/东区快口 -> 西区快口。"""

    def setUp(self):
        self.index = PortCompositeIndex()
        # d1：西区慢口 + 东区快口；d2：同元素满足的对照文档。
        self.index.add_document(doc("d1", [("west", 1000), ("east", 9000)]))
        self.index.add_document(doc("d2", [("west", 8000)]))

    def test_before_replacement_same_element_excludes_doc(self):
        result = self.index.query("west", 5000)
        # 只有 d2 的 west 端口自身达到速率下限；d1 的 west 口只有 1000。
        self.assertEqual(result.doc_ids, ("d2",))
        self.assertEqual(result.fetches, 0)
        self.assertEqual(result.candidate_docs, 1)
        self.assertEqual(result.entries_examined, 1)
        self.assertTrue(result.same_element)

    def test_replacement_to_west_fast_makes_same_element_match(self):
        # 将 d1 从 "西区慢口 + 东区快口" 换成 "西区快口"。
        self.index.replace_document(doc("d1", [("west", 9000)]))

        result = self.index.query("west", 5000)
        self.assertEqual(set(result.doc_ids), {"d1", "d2"})
        # 同元素查询由索引直接给出结果，不回表。
        self.assertEqual(result.fetches, 0)
        self.assertEqual(len(result.doc_ids), len(set(result.doc_ids)))

    def test_split_element_match_before_replacement(self):
        # 跨元素语义：d1 的 west 口满足机房、east 快口满足速率，应命中。
        result = self.index.query("west", 5000, same_element=False)
        self.assertEqual(set(result.doc_ids), {"d1", "d2"})
        # west 分片有 d1、d2 两篇候选，均需回表确认。
        self.assertEqual(result.candidate_docs, 2)
        self.assertEqual(result.fetches, 2)
        self.assertFalse(result.same_element)


class DuplicatePortTest(unittest.TestCase):
    """重复端口的条目维护与删除语义。"""

    def test_removing_one_duplicate_keeps_document_matched(self):
        index = PortCompositeIndex()
        index.add_document(doc("d1", [("west", 8000), ("west", 8000)]))

        # 两个完全相同的端口各产生一条索引条目。
        self.assertEqual(index.entry_count, 2)
        self.assertEqual(index.query("west", 5000).doc_ids, ("d1",))

        # 删掉其中一个重复端口，另一个仍然满足条件。
        index.replace_document(doc("d1", [("west", 8000)]))
        self.assertEqual(index.entry_count, 1)

        result = index.query("west", 5000)
        self.assertEqual(result.doc_ids, ("d1",))
        self.assertEqual(result.fetches, 0)

        # 再删掉最后一个端口，文档不再命中，但索引条目同步更新。
        index.replace_document(doc("d1", [("east", 100)]))
        self.assertEqual(index.query("west", 5000).doc_ids, ())
        self.assertEqual(index.entry_count, 1)

    def test_duplicate_matching_ports_return_document_once(self):
        index = PortCompositeIndex()
        index.add_document(doc("d1", [("west", 9000), ("west", 9000)]))

        result = index.query("west", 5000)
        self.assertEqual(result.doc_ids, ("d1",))
        # 扫描了两条条目，但候选文档与最终结果都只计一次。
        self.assertEqual(result.entries_examined, 2)
        self.assertEqual(result.candidate_docs, 1)


class QuerySemanticsTest(unittest.TestCase):
    """同元素 / 跨元素两种语义的差异。"""

    def test_split_element_requires_both_existential_predicates(self):
        index = PortCompositeIndex()
        # d1：有 west 口但没有任何高速率端口。
        index.add_document(doc("d1", [("west", 1000), ("east", 2000)]))
        # d2：west 口与高速率端口由不同元素承担。
        index.add_document(doc("d2", [("west", 1000), ("east", 9000)]))

        same = index.query("west", 5000)
        self.assertEqual(same.doc_ids, ())

        split = index.query("west", 5000, same_element=False)
        # d1 回表后因不存在高速率端口被过滤；d2 跨元素命中。
        self.assertEqual(split.doc_ids, ("d2",))
        self.assertEqual(split.candidate_docs, 2)
        self.assertEqual(split.entries_examined, 2)
        self.assertEqual(split.fetches, 2)

    def test_speed_bound_is_inclusive(self):
        index = PortCompositeIndex()
        index.add_document(doc("d1", [("west", 5000)]))
        self.assertEqual(index.query("west", 5000).doc_ids, ("d1",))
        self.assertEqual(index.query("west", 5001).doc_ids, ())

    def test_multiple_matching_documents_deduplicated(self):
        index = PortCompositeIndex()
        index.add_document(doc("d1", [("west", 6000), ("west", 7000)]))
        index.add_document(doc("d2", [("west", 8000)]))
        index.add_document(doc("d3", [("east", 8000)]))

        result = index.query("west", 5000)
        self.assertEqual(result.doc_ids, ("d1", "d2"))
        self.assertEqual(result.candidate_docs, 2)
        self.assertEqual(result.fetches, 0)


class LifecycleTest(unittest.TestCase):
    """新增 / 替换 / 删除的生命周期与错误处理。"""

    def test_delete_removes_entries_and_document(self):
        index = PortCompositeIndex()
        index.add_document(doc("d1", [("west", 8000), ("east", 100)]))
        index.add_document(doc("d2", [("west", 8000)]))
        self.assertEqual(index.entry_count, 3)

        index.delete_document("d1")
        self.assertNotIn("d1", index)
        self.assertEqual(index.entry_count, 1)
        self.assertEqual(index.query("west", 5000).doc_ids, ("d2",))

    def test_add_replace_delete_error_contract(self):
        index = PortCompositeIndex()
        index.add_document(doc("d1", [("west", 100)]))

        with self.assertRaises(ValueError):
            index.add_document(doc("d1", [("west", 200)]))
        with self.assertRaises(KeyError):
            index.replace_document(doc("missing", [("west", 100)]))
        with self.assertRaises(KeyError):
            index.delete_document("missing")

    def test_validation_rejects_bad_documents(self):
        index = PortCompositeIndex()
        with self.assertRaises((TypeError, ValueError)):
            index.add_document({"id": "", "ports": []})
        with self.assertRaises((TypeError, ValueError)):
            index.add_document({"id": "d1", "ports": "not-a-list"})
        with self.assertRaises((TypeError, ValueError)):
            index.add_document(
                {"id": "d1", "ports": [{"room": "west", "speed": "1G"}]}
            )
        with self.assertRaises((TypeError, ValueError)):
            index.add_document(
                {"id": "d1", "ports": [{"room": "west", "speed": True}]}
            )

    def test_document_copy_is_independent_of_input(self):
        index = PortCompositeIndex()
        payload = doc("d1", [("west", 1000)])
        index.add_document(payload)
        payload["ports"][0]["speed"] = 1  # 外部篡改入参
        self.assertEqual(index.get_document("d1")["ports"][0]["speed"], 1000)


if __name__ == "__main__":
    unittest.main()
