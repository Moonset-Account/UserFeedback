"""序列配置与异常参数的校验测试。"""

import unittest

from sequence_allocator import SequenceCatalog, SequenceConfig, SequenceConfigError


class TestSequenceConfig(unittest.TestCase):
    def test_valid_default_config(self):
        cfg = SequenceConfig(name="s")
        self.assertEqual(cfg.start_value, 1)
        self.assertEqual(cfg.increment_by, 1)
        self.assertEqual(cfg.cache_size, 20)
        self.assertFalse(cfg.cycle)
        self.assertTrue(cfg.ascending)

    def test_descending_config(self):
        cfg = SequenceConfig(name="s", start_value=10, increment_by=-3)
        self.assertFalse(cfg.ascending)

    def test_invalid_name(self):
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="")

    def test_zero_increment_rejected(self):
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="s", increment_by=0)

    def test_non_integer_rejected(self):
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="s", increment_by=1.5)  # type: ignore[arg-type]
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="s", start_value=True)  # bool 不算整数

    def test_bounds_order(self):
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="s", min_value=10, max_value=1)

    def test_start_outside_bounds(self):
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="s", start_value=0, min_value=1, max_value=10)

    def test_cache_size_must_be_positive(self):
        with self.assertRaises(SequenceConfigError):
            SequenceConfig(name="s", cache_size=0)

    def test_validate_increment_rules(self):
        cfg = SequenceConfig(name="s", start_value=10, min_value=1, max_value=10)
        with self.assertRaises(SequenceConfigError):
            cfg.validate_increment(0)
        with self.assertRaises(SequenceConfigError):
            cfg.validate_increment(1.5)  # type: ignore[arg-type]
        with self.assertRaises(SequenceConfigError):
            cfg.validate_increment(True)  # type: ignore[arg-type]
        # 改步长的越界判断延迟到预留时按 watermark 处理，这里不提前拒绝。
        cfg.validate_increment(100)
        cfg.validate_increment(-100)
        cfg.validate_increment(-9)
        cfg.validate_increment(1)

    def test_catalog_duplicate_create(self):
        catalog = SequenceCatalog()
        catalog.create_sequence("dup", cache_size=2)
        with self.assertRaises(SequenceConfigError):
            catalog.create_sequence("dup", cache_size=2)


if __name__ == "__main__":
    unittest.main()
