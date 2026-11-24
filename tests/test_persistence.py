"""预留状态保存/恢复与进程退出空缺的测试。"""

import json
import os
import tempfile
import unittest

from sequence_allocator import SequenceCatalog, SequenceConfigError


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="seqtest-")
        self.path = os.path.join(self.tmpdir, "state.json")

    def test_save_and_load_roundtrip(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "s1", start_value=10, increment_by=2,
            min_value=0, max_value=100, cache_size=4, cycle=True,
        )
        catalog.create_sequence(
            "s2", start_value=5, increment_by=-1,
            min_value=1, max_value=5, cache_size=2, cycle=False,
        )
        s1 = catalog.open_session("s1")
        [s1.nextval() for _ in range(3)]
        catalog.alter_increment("s1", 3)
        s1.close()

        catalog.save(self.path)
        restored = SequenceCatalog.load(self.path)

        self.assertEqual(sorted(restored.list_sequences()), ["s1", "s2"])
        cfg = restored.get_config("s1")
        self.assertEqual(cfg.increment_by, 3)
        self.assertTrue(cfg.cycle)
        state = restored.get_state("s1")
        self.assertEqual(state.version, 2)
        self.assertEqual(state.reclaimed_batches, 1)
        self.assertEqual(state.watermark, catalog.get_state("s1").watermark)

    def test_process_exit_leaves_gap(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "s", start_value=1, increment_by=1,
            min_value=1, max_value=1000, cache_size=10,
        )
        session = catalog.open_session("s")
        self.assertEqual(session.nextval(), 1)  # 预留 1..10，仅消费 1
        catalog.save(self.path)
        # 模拟进程退出：丢弃目录与会话。
        del catalog, session

        restored = SequenceCatalog.load(self.path)
        new_session = restored.open_session("s")
        # watermark=10，2..10 永不补发 -> 空缺。
        self.assertEqual(new_session.nextval(), 11)
        new_session.close()

    def test_restored_catalog_keeps_allocating_consistently(self):
        catalog = SequenceCatalog()
        catalog.create_sequence(
            "s", start_value=1, increment_by=1,
            min_value=1, max_value=100, cache_size=3,
        )
        session = catalog.open_session("s")
        [session.nextval() for _ in range(4)]  # 1,2,3,4；watermark=6
        session.close()
        catalog.save(self.path)

        restored = SequenceCatalog.load(self.path)
        s = restored.open_session("s")
        self.assertEqual([s.nextval() for _ in range(3)], [7, 8, 9])
        s.close()

    def test_save_uses_atomic_replace(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("s", cache_size=2)
        catalog.open_session("s").nextval()
        catalog.save(self.path)
        leftovers = [
            name for name in os.listdir(self.tmpdir) if name.startswith(".seqcatalog-")
        ]
        self.assertEqual(leftovers, [])
        with open(self.path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["format_version"], 1)
        self.assertIn("s", payload["sequences"])

    def test_load_rejects_bad_format(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"format_version": 999, "sequences": {}}, fh)
        with self.assertRaises(SequenceConfigError):
            SequenceCatalog.load(self.path)

        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"sequences": {}}, fh)
        with self.assertRaises(SequenceConfigError):
            SequenceCatalog.load(self.path)

    def test_load_rejects_corrupt_state(self):
        bad_payload = {
            "format_version": 1,
            "sequences": {
                "s": {
                    "config": {
                        "name": "s", "start_value": 1, "increment_by": 1,
                        "min_value": 1, "max_value": 10, "cache_size": 2,
                        "cycle": False,
                    },
                    "state": {"watermark": 1, "version": 0, "reclaimed_batches": 0},
                }
            },
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(bad_payload, fh)
        with self.assertRaises(SequenceConfigError):
            SequenceCatalog.load(self.path)


if __name__ == "__main__":
    unittest.main()
