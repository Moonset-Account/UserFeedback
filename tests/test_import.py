# -*- coding: utf-8 -*-
"""catalog_import 的离线单元测试（仅标准库 unittest）。

每个用例对应原始需求的一条明确要求；case_id 稳定，供核验映射。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog_import
from catalog_import import CatalogImportError, ImportConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
NS = "urn:example:catalog:v1"


def make_config(**overrides):
    params = {
        "allowed_namespaces": [NS],
        "allowed_fields": ["name", "email", "phone"],
        "max_depth": 8,
        "max_nodes": 1000,
        "max_field_length": 64,
    }
    params.update(overrides)
    return ImportConfig(**params)


def parse_string(text, config=None):
    return catalog_import.parse_chunks([text.encode("utf-8")], config or make_config())


def expect_error(testcase, text, category, config=None):
    with testcase.assertRaises(CatalogImportError) as ctx:
        parse_string(text, config)
    testcase.assertEqual(ctx.exception.category, category)
    return ctx.exception


class SecurityRejectionTests(unittest.TestCase):
    """DTD、实体声明、外部引用在解析时拒绝。"""

    def test_sec01_doctype_rejected(self):
        # case_id: SEC-01 内部 DTD 子集（含实体声明）整份拒绝
        xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE catalog [<!ENTITY xxe "forbidden">]>\n'
            '<catalog xmlns="%s"><supplier id="A"><name>&xxe;</name></supplier></catalog>'
        ) % NS
        err = expect_error(self, xml, catalog_import.CATEGORY_DTD_FORBIDDEN)
        self.assertIsNotNone(err.line)

    def test_sec02_external_dtd_rejected(self):
        # case_id: SEC-02 外部 DTD 引用（SYSTEM 标识符）拒绝
        xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE catalog SYSTEM "file:///etc/passwd">\n'
            '<catalog xmlns="%s"/>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_DTD_FORBIDDEN)

    def test_sec03_external_entity_rejected(self):
        # case_id: SEC-03 外部实体声明（XXE 典型载荷）随 DOCTYPE 一并拒绝
        xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE catalog [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
            '<catalog xmlns="%s"><supplier id="A"><name>&xxe;</name></supplier></catalog>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_DTD_FORBIDDEN)

    def test_sec04_undefined_entity_reference_rejected(self):
        # case_id: SEC-04 未声明的实体引用按实体类错误拒绝（非普通格式错误）
        xml = (
            '<catalog xmlns="%s"><supplier id="A"><name>&undefined;</name></supplier></catalog>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_ENTITY_FORBIDDEN)

    def test_sec05_parameter_entity_rejected(self):
        # case_id: SEC-05 参数实体声明随 DOCTYPE 一并拒绝
        xml = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE catalog [<!ENTITY %% pe "<!ENTITY x \'y\'>"> %%pe;]>\n'
            '<catalog xmlns="%s"/>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_DTD_FORBIDDEN)


class LimitTests(unittest.TestCase):
    """深度、节点总数、单字段长度分别设限。"""

    def test_lim01_depth_exceeded(self):
        # case_id: LIM-01 嵌套深度超限
        xml = (
            '<catalog xmlns="%s"><supplier id="A"><name>x</name></supplier></catalog>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_DEPTH_EXCEEDED,
                     make_config(max_depth=2))

    def test_lim02_node_count_exceeded(self):
        # case_id: LIM-02 节点总数超限
        xml = (
            '<catalog xmlns="%s"><supplier id="A"><name>x</name><email>y</email></supplier></catalog>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_NODE_COUNT_EXCEEDED,
                     make_config(max_nodes=3))

    def test_lim03_field_length_exceeded(self):
        # case_id: LIM-03 单字段长度超限
        xml = (
            '<catalog xmlns="%s"><supplier id="A"><name>%s</name></supplier></catalog>'
        ) % (NS, "x" * 65)
        expect_error(self, xml, catalog_import.CATEGORY_FIELD_LENGTH_EXCEEDED,
                     make_config(max_field_length=64))

    def test_lim04_limits_independent(self):
        # case_id: LIM-04 三类上限相互独立：仅字段超限时报字段类错误
        xml = (
            '<catalog xmlns="%s"><supplier id="A"><name>%s</name></supplier></catalog>'
        ) % (NS, "x" * 65)
        expect_error(self, xml, catalog_import.CATEGORY_FIELD_LENGTH_EXCEEDED,
                     make_config(max_depth=8, max_nodes=1000, max_field_length=64))


class NamespaceTests(unittest.TestCase):
    """命名空间按 URI 比对，与前缀无关。"""

    def test_ns01_prefix_replacement_equivalent(self):
        # case_id: NS-01 替换前缀的等价目录输出完全相同的标准化记录
        config = make_config()
        normal = catalog_import.parse_file(
            os.path.join(SAMPLES, "catalog_normal.xml"), config)
        replaced = catalog_import.parse_file(
            os.path.join(SAMPLES, "catalog_ns_prefix_replaced.xml"), config)
        self.assertEqual(normal, replaced)
        self.assertEqual(len(normal), 2)

    def test_ns02_uri_mismatch_rejected(self):
        # case_id: NS-02 相同前缀绑定到未授权 URI 时拒绝（比对 URI 而非前缀）
        xml = (
            '<c:catalog xmlns:c="urn:attacker:fake">'
            '<c:supplier id="A"><c:name>x</c:name></c:supplier></c:catalog>'
        )
        expect_error(self, xml, catalog_import.CATEGORY_NAMESPACE_NOT_ALLOWED)

    def test_ns03_no_namespace_rejected(self):
        # case_id: NS-03 无命名空间文档不在允许列表内
        xml = '<catalog><supplier id="A"><name>x</name></supplier></catalog>'
        expect_error(self, xml, catalog_import.CATEGORY_NAMESPACE_NOT_ALLOWED)


class StructureAndAuditTests(unittest.TestCase):
    """有效记录标准化输出；非法文档给出位置与分类。"""

    def test_out01_normal_sample_normalized(self):
        # case_id: OUT-01 普通目录输出标准化对象
        records = catalog_import.parse_file(
            os.path.join(SAMPLES, "catalog_normal.xml"), make_config())
        self.assertEqual(records, [
            {"id": "S-001", "fields": {
                "name": "Acme Parts Ltd",
                "email": "sales@acme.example",
                "phone": "+1-555-0100",
            }},
            {"id": "S-002", "fields": {
                "name": "北京精工部件",
                "email": "sales@bj-seiko.example",
            }},
        ])

    def test_out02_over_limit_sample(self):
        # case_id: OUT-02 超限样例整份拒绝并分类
        with self.assertRaises(CatalogImportError) as ctx:
            catalog_import.parse_file(
                os.path.join(SAMPLES, "catalog_over_limit.xml"), make_config())
        self.assertEqual(ctx.exception.category,
                         catalog_import.CATEGORY_FIELD_LENGTH_EXCEEDED)
        self.assertIsNotNone(ctx.exception.line)

    def test_err01_malformed_xml_reports_position(self):
        # case_id: ERR-01 结构不合法文档报告行/列位置与分类
        xml = (
            '<catalog xmlns="%s">\n<supplier id="A">\n<name>broken</supplier>\n</catalog>'
        ) % NS
        err = expect_error(self, xml, catalog_import.CATEGORY_MALFORMED_XML)
        self.assertEqual(err.line, 3)
        self.assertIsNotNone(err.column)

    def test_err02_unknown_field_rejected(self):
        # case_id: ERR-02 允许字段列表之外的元素被拒绝
        xml = (
            '<catalog xmlns="%s"><supplier id="A"><secret>x</secret></supplier></catalog>'
        ) % NS
        expect_error(self, xml, catalog_import.CATEGORY_FIELD_NOT_ALLOWED)

    def test_err03_missing_record_id(self):
        # case_id: ERR-03 记录缺少 id 属性属于结构不合法
        xml = '<catalog xmlns="%s"><supplier><name>x</name></supplier></catalog>' % NS
        expect_error(self, xml, catalog_import.CATEGORY_STRUCTURE_INVALID)

    def test_err04_wrong_root_element(self):
        # case_id: ERR-04 根元素不符属于结构不合法
        xml = '<orders xmlns="%s"/>' % NS
        expect_error(self, xml, catalog_import.CATEGORY_STRUCTURE_INVALID)

    def test_io01_chunked_feeding_multibyte(self):
        # case_id: IO-01 分段读取：逐字节喂入（UTF-8 多字节被切断）结果一致
        path = os.path.join(SAMPLES, "catalog_normal.xml")
        with open(path, "rb") as fileobj:
            data = fileobj.read()
        config = make_config()
        whole = catalog_import.parse_chunks([data], config)
        bytewise = catalog_import.parse_chunks(
            [data[i:i + 1] for i in range(len(data))], config)
        self.assertEqual(whole, bytewise)
        self.assertEqual(len(bytewise), 2)


if __name__ == "__main__":
    unittest.main()
