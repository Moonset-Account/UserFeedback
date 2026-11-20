"""schema_registry 的合成数据单元测试。

注意：按任务要求，本测试文件仅作为源码提供，尚未实际运行。
所有数据均为本地合成，不访问外部服务。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schema_registry import (  # noqa: E402
    INT,
    STRING,
    ArrayType,
    CompatibilityError,
    Field,
    ReadError,
    RecordType,
    RegistryError,
    SchemaRegistry,
)
from schema_registry.resolution import ResolutionError, resolve_record_fields  # noqa: E402


def old_schema_v1():
    """旧写入模式 v1：订单消息。"""
    item = RecordType(
        "Item",
        (
            Field("sku", STRING),
            Field("count", INT),
            Field("note", STRING, nullable=True),
        ),
    )
    return RecordType(
        "Order",
        (
            Field("order_id", INT),
            Field("title", STRING),
            Field("customer", STRING, nullable=True),
            Field("items", ArrayType(item)),
        ),
    )


def new_reader_schema():
    """新读取模式：改名 + 新增字段 + 嵌套改名。"""
    item = RecordType(
        "Item",
        (
            Field("sku", STRING),
            Field("quantity", INT, aliases=("count",)),
            Field("note", STRING, nullable=True),
            Field("warehouse", STRING, default="main"),
        ),
    )
    return RecordType(
        "Order",
        (
            Field("order_id", INT),
            Field("title", STRING),
            Field("customer", STRING, nullable=True),
            Field("items", ArrayType(item)),
            Field("priority", INT, default=0),
        ),
    )


class ResolutionTests(unittest.TestCase):
    def test_original_name_wins_over_alias(self):
        old = RecordType("R", (Field("a", INT), Field("b", STRING)))
        new = RecordType("R", (Field("a", INT, aliases=("b",)),))
        mapping = resolve_record_fields(new, old)
        self.assertEqual(mapping["a"].name, "a")  # 原名优先，不取别名 b

    def test_alias_unique_hit(self):
        old = RecordType("R", (Field("legacy", INT),))
        new = RecordType("R", (Field("fresh", INT, aliases=("legacy",))))
        mapping = resolve_record_fields(new, old)
        self.assertEqual(mapping["fresh"].name, "legacy")

    def test_alias_ambiguous_rejected(self):
        old = RecordType("R", (Field("x", INT), Field("y", INT)))
        new = RecordType("R", (Field("f", INT, aliases=("x", "y"))))
        with self.assertRaises(ResolutionError):
            resolve_record_fields(new, old)

    def test_old_field_not_assigned_twice(self):
        old = RecordType("R", (Field("v", INT),))
        new = RecordType(
            "R",
            (
                Field("a", INT, aliases=("v",)),
                Field("b", INT, aliases=("v",)),
            ),
        )
        with self.assertRaises(ResolutionError):
            resolve_record_fields(new, old)

    def test_missing_field_maps_to_none(self):
        old = RecordType("R", (Field("a", INT),))
        new = RecordType("R", (Field("a", INT), Field("b", STRING)))
        mapping = resolve_record_fields(new, old)
        self.assertIsNone(mapping["b"])


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.reg = SchemaRegistry()
        self.v1 = self.reg.register_writer(old_schema_v1())

    def test_compatible_reader_registered(self):
        self.reg.register_reader("order-reader", new_reader_schema())
        self.assertTrue(self.reg.is_reader_registered("order-reader"))

    def test_type_mismatch_reports_path_and_version(self):
        bad = RecordType("Order", (Field("order_id", STRING),))
        with self.assertRaises(CompatibilityError) as ctx:
            self.reg.register_reader("bad", bad)
        self.assertEqual(ctx.exception.old_version, self.v1)
        self.assertEqual(ctx.exception.issues[0].path, "order_id")

    def test_array_element_incompatibility_located(self):
        item = RecordType(
            "Item",
            (
                Field("sku", INT),  # 旧的是 STRING
                Field("quantity", INT, aliases=("count",)),
                Field("note", STRING, nullable=True),
            ),
        )
        bad = RecordType(
            "Order",
            (
                Field("order_id", INT),
                Field("title", STRING),
                Field("customer", STRING, nullable=True),
                Field("items", ArrayType(item)),
            ),
        )
        with self.assertRaises(CompatibilityError) as ctx:
            self.reg.register_reader("bad-items", bad)
        self.assertEqual(ctx.exception.old_version, self.v1)
        self.assertEqual(ctx.exception.issues[0].path, "items[].sku")

    def test_nullable_old_field_requires_nullable_new(self):
        bad = RecordType(
            "Order",
            (
                Field("order_id", INT),
                Field("title", STRING),
                Field("customer", STRING),  # 旧的可空，新的不可空
            ),
        )
        with self.assertRaises(CompatibilityError) as ctx:
            self.reg.register_reader("bad-null", bad)
        self.assertEqual(ctx.exception.issues[0].path, "customer")

    def test_missing_field_needs_typed_default(self):
        ok = RecordType("Order", (Field("order_id", INT), Field("tag", STRING, default="x")))
        self.reg.register_reader("ok-default", ok)

        bad = RecordType("Order", (Field("order_id", INT), Field("tag", STRING, default=1)))
        with self.assertRaises(RegistryError):
            self.reg.register_reader("bad-default", bad)

        no_default = RecordType("Order", (Field("order_id", INT), Field("tag", STRING)))
        with self.assertRaises(CompatibilityError) as ctx:
            self.reg.register_reader("no-default", no_default)
        self.assertEqual(ctx.exception.issues[0].path, "tag")

    def test_nullable_new_field_may_omit_default(self):
        ok = RecordType(
            "Order",
            (Field("order_id", INT), Field("tag", STRING, nullable=True)),
        )
        self.reg.register_reader("nullable-implicit", ok)

    def test_retired_writer_not_compared(self):
        bad = RecordType("Order", (Field("order_id", STRING),))
        self.reg.retire_writer(self.v1)
        self.reg.register_reader("after-retire", bad)  # 不再比较，登记成功
        self.assertEqual(self.reg.retained_versions(), [])

    def test_first_incompatible_old_version_reported(self):
        # v2 与 v1 结构相同；新读取模式对两者同一位置不兼容，应报 v1
        self.reg.register_writer(old_schema_v1())
        bad = RecordType("Order", (Field("order_id", STRING),))
        with self.assertRaises(CompatibilityError) as ctx:
            self.reg.register_reader("multi", bad)
        self.assertEqual(ctx.exception.old_version, 1)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.reg = SchemaRegistry()
        self.v1 = self.reg.register_writer(old_schema_v1())
        self.reg.register_reader("order-reader", new_reader_schema())

    def test_read_applies_aliases_and_defaults(self):
        message = {
            "order_id": 7,
            "title": "books",
            "customer": None,  # 显式空值，新类型可空，须接纳
            "items": [
                {"sku": "A-1", "count": 3, "note": None},
                {"sku": "B-2", "count": 1, "note": "gift"},
            ],
        }
        out = self.reg.read("order-reader", self.v1, message)
        self.assertEqual(out["order_id"], 7)
        self.assertIsNone(out["customer"])
        self.assertEqual(out["priority"], 0)  # 缺字段才用缺省值
        self.assertEqual(out["items"][0]["quantity"], 3)  # 别名 count -> quantity
        self.assertEqual(out["items"][0]["warehouse"], "main")
        self.assertIsNone(out["items"][0]["note"])
        self.assertEqual(out["items"][1]["note"], "gift")

    def test_explicit_null_rejected_when_not_nullable(self):
        message = {
            "order_id": 7,
            "title": None,  # 新类型不可空
            "customer": "c",
            "items": [],
        }
        with self.assertRaises(ReadError):
            self.reg.read("order-reader", self.v1, message)

    def test_unregistered_reader_cannot_read(self):
        with self.assertRaises(RegistryError):
            self.reg.read("ghost", self.v1, {})

    def test_failed_registration_does_not_unlock_read(self):
        bad = RecordType("Order", (Field("order_id", STRING),))
        with self.assertRaises(CompatibilityError):
            self.reg.register_reader("bad", bad)
        with self.assertRaises(RegistryError):
            self.reg.read("bad", self.v1, {"order_id": 1})


if __name__ == "__main__":
    unittest.main()
