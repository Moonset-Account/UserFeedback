"""绑定前校验模块的回归测试。

运行方式（仓库根目录）：
    python3 -I -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import os
import sys
import unittest

# 兼容 python3 -I（隔离模式不把 cwd 加入 sys.path）：
# 将仓库根目录（本文件的上一级）显式加入模块搜索路径
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config_import import (  # noqa: E402
    AUTH_SOURCES,
    ImportRecord,
    RejectionCode,
    validate_batch,
    validate_record,
)
from config_import import authz  # noqa: E402


def codes(result) -> set[RejectionCode]:
    return {rej.code for rej in result.rejections}


def rejection_paths(result) -> set[str]:
    return {rej.field_path for rej in result.rejections}


def decision_paths(result) -> set[str]:
    return {dec.field_path for dec in result.decisions}


class NestedBindingTests(unittest.TestCase):
    """合法嵌套更新（含别名、对象数组）可以通过，并正确模拟绑定。"""

    def setUp(self) -> None:
        # 服务端当前已持久化的对象，包含服务端内部字段的原值
        self.existing = {
            "user_id": "u-1001",
            "full_name": "旧姓名",
            "display_name": "旧昵称",
            "email": "old@example.com",
            "created_at": "2026-01-01T00:00:00Z",
            "preferences": {
                "locale": "en_US",
                "timezone": "UTC",
                "email_opt_in": False,
                "prefs_fingerprint": "fp-server-only",
            },
            "contacts": [],
        }
        # 输入同时使用顶层别名（name/mail/prefs）与嵌套别名
        # （lang/tz、mail/tel）
        self.payload = {
            "name": "张三",
            "display_name": "阿三",
            "mail": "zhangsan@example.com",
            "prefs": {
                "lang": "zh_CN",
                "tz": "Asia/Shanghai",
                "marketing_email": True,
            },
            "contacts": [
                {"mail": "work@example.com", "tel": "13800000000"},
                {"email": "home@example.com"},
            ],
        }
        self.record = ImportRecord(
            record_id="rec-nested-ok",
            type_name="UserProfile",
            payload=self.payload,
            auth_source="admin_console",
            existing_state=self.existing,
        )

    def test_nested_update_accepted_and_bound(self) -> None:
        result = validate_record(self.record)

        self.assertTrue(result.accepted, msg=result.rejections)
        self.assertEqual(result.schema_version, "1.1")
        bound = result.bound_object

        # 顶层别名规范化为规范字段
        self.assertEqual(bound["full_name"], "张三")
        self.assertEqual(bound["email"], "zhangsan@example.com")
        self.assertEqual(bound["display_name"], "阿三")

        # 嵌套对象递归合并，嵌套别名同样规范化
        self.assertEqual(
            bound["preferences"],
            {
                "locale": "zh_CN",
                "timezone": "Asia/Shanghai",
                "email_opt_in": True,
                # 服务端字段原值保留
                "prefs_fingerprint": "fp-server-only",
            },
        )

        # 对象数组逐元素绑定，元素内别名同样规范化为规范键
        self.assertEqual(
            bound["contacts"],
            [
                {
                    "email": "work@example.com",
                    "phone": "13800000000",
                },
                {"email": "home@example.com"},
            ],
        )

        # 顶层服务端字段原值保留
        self.assertEqual(bound["user_id"], "u-1001")
        self.assertEqual(bound["created_at"], "2026-01-01T00:00:00Z")

    def test_trace_carries_version_path_and_rules(self) -> None:
        result = validate_record(self.record)
        self.assertTrue(result.accepted)

        paths = decision_paths(result)
        # 字段路径可回溯到嵌套层与数组元素
        self.assertIn("$.full_name", paths)
        self.assertIn("$.preferences.locale", paths)
        self.assertIn("$.preferences.timezone", paths)
        self.assertIn("$.preferences.email_opt_in", paths)
        self.assertIn("$.contacts[0].email", paths)
        self.assertIn("$.contacts[0].phone", paths)

        # 每条判定带生效的模式版本与授权规则
        for dec in result.decisions:
            self.assertIn(dec.schema_version, {"1.1", "1.0"})
            self.assertTrue(dec.rule_id)
            self.assertTrue(dec.field_path.startswith("$"))

        # 别名判定可回溯到具体别名规则
        alias_decisions = [d for d in result.decisions if d.alias_rule_id]
        alias_rules = {d.alias_rule_id for d in alias_decisions}
        self.assertIn("RULE_ALIAS_USERPROFILE_NAME", alias_rules)
        self.assertIn("RULE_ALIAS_PREFERENCES_LANG", alias_rules)
        self.assertIn("RULE_ALIAS_CONTACT_MAIL", alias_rules)

        # name -> full_name 的来源键与规范路径都被记录
        name_decision = next(d for d in result.decisions if d.source_key == "name")
        self.assertEqual(name_decision.field_path, "$.full_name")


class ServerFieldTests(unittest.TestCase):
    """服务端内部字段：禁止写入，且已持久化的原值保持不变。"""

    def test_top_level_server_field_rejected(self) -> None:
        record = ImportRecord(
            record_id="rec-server-top",
            type_name="UserProfile",
            payload={
                "full_name": "李四",
                "created_at": "2099-01-01T00:00:00Z",  # 服务端字段
            },
            auth_source="admin_console",
            existing_state={
                "user_id": "u-2002",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        result = validate_record(record)

        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.SERVER_FIELD_FORBIDDEN, codes(result))
        self.assertIn("$.created_at", rejection_paths(result))
        # 即使记录被拒，顶层结果也回填已解析的生效模式版本
        self.assertEqual(result.schema_version, "1.1")
        rejection = next(
            r for r in result.rejections
            if r.code is RejectionCode.SERVER_FIELD_FORBIDDEN
        )
        self.assertEqual(rejection.rule_id, authz.RULE_SERVER_FIELD)
        self.assertEqual(rejection.schema_version, "1.1")
        # 记录被整体拒绝，不产生绑定对象，原值自然不可能被覆盖
        self.assertIsNone(result.bound_object)

    def test_nested_server_field_rejected(self) -> None:
        record = ImportRecord(
            record_id="rec-server-nested",
            type_name="UserProfile",
            payload={
                "preferences": {
                    "locale": "ja_JP",
                    "prefs_fingerprint": "tampered",  # 嵌套服务端字段
                }
            },
            auth_source="admin_console",
        )
        result = validate_record(record)

        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.SERVER_FIELD_FORBIDDEN, codes(result))
        self.assertIn("$.preferences.prefs_fingerprint", rejection_paths(result))

    def test_untouched_server_fields_keep_original_value(self) -> None:
        existing = {
            "user_id": "u-3003",
            "created_at": "2026-02-02T00:00:00Z",
            "preferences": {"prefs_fingerprint": "fp-keep"},
        }
        record = ImportRecord(
            record_id="rec-server-keep",
            type_name="UserProfile",
            payload={"full_name": "王五", "preferences": {"locale": "fr_FR"}},
            auth_source="admin_console",
            existing_state=existing,
        )
        result = validate_record(record)

        self.assertTrue(result.accepted, msg=result.rejections)
        self.assertEqual(result.bound_object["user_id"], "u-3003")
        self.assertEqual(
            result.bound_object["created_at"], "2026-02-02T00:00:00Z"
        )
        self.assertEqual(
            result.bound_object["preferences"]["prefs_fingerprint"], "fp-keep"
        )


class BatchIsolationTests(unittest.TestCase):
    """一条不合规记录只影响其自身，同批其它记录照常通过。"""

    def test_bad_record_does_not_poison_batch(self) -> None:
        good = ImportRecord(
            record_id="good-1",
            type_name="NotificationPolicy",
            payload={"channel": "email", "enabled": True, "retention_days": 30},
            auth_source="admin_console",
        )
        bad_server_field = ImportRecord(
            record_id="bad-1",
            type_name="NotificationPolicy",
            payload={
                "channel": "sms",
                "last_dispatched_at": "2099-01-01T00:00:00Z",
            },
            auth_source="admin_console",
        )
        bad_type = ImportRecord(
            record_id="bad-2",
            type_name="NoSuchType",
            payload={"x": 1},
            auth_source="admin_console",
        )
        good_2 = ImportRecord(
            record_id="good-2",
            type_name="DeviceConfig",
            payload={"hostname": "host-7", "tags": ["a", "b"]},
            auth_source="partner_sync",
        )

        results = validate_batch([good, bad_server_field, bad_type, good_2])
        by_id = {r.record_id: r for r in results}

        self.assertTrue(by_id["good-1"].accepted)
        self.assertTrue(by_id["good-2"].accepted)
        self.assertFalse(by_id["bad-1"].accepted)
        self.assertFalse(by_id["bad-2"].accepted)

        self.assertEqual(
            codes(by_id["bad-1"]), {RejectionCode.SERVER_FIELD_FORBIDDEN}
        )
        self.assertEqual(codes(by_id["bad-2"]), {RejectionCode.UNKNOWN_TYPE})

        # 好记录的绑定结果不受坏记录影响
        self.assertEqual(by_id["good-1"].bound_object["channel"], "email")
        self.assertEqual(by_id["good-2"].bound_object["hostname"], "host-7")


class NonMutationTests(unittest.TestCase):
    """相同对象重复校验：结果幂等，且原始输入始终不被修改。"""

    def _make_record(self) -> ImportRecord:
        return ImportRecord(
            record_id="rec-idempotent",
            type_name="DeviceConfig",
            payload={
                "host": "edge-01",          # 1.1 下 host -> hostname
                "timeout": 120,             # 1.1 下 timeout -> policy_timeout
                "tags": ["prod", "edge"],
                "contact": {"mail": "ops@example.com", "tel": "10086"},
            },
            auth_source="partner_sync",
            existing_state={
                "device_id": "d-9001",
                "enrolled_at": "2026-03-03T00:00:00Z",
                "compliance_state": "compliant",
            },
        )

    def test_repeated_validation_is_idempotent_and_non_mutating(self) -> None:
        record = self._make_record()
        payload_snapshot = copy.deepcopy(record.payload)
        existing_snapshot = copy.deepcopy(record.existing_state)

        first = validate_record(record)
        second = validate_record(record)
        third = validate_batch([record])[0]

        self.assertTrue(first.accepted, msg=first.rejections)
        # 原始输入与既有状态三次校验后保持不变
        self.assertEqual(record.payload, payload_snapshot)
        self.assertEqual(record.existing_state, existing_snapshot)

        # 重复校验结果一致（幂等）
        self.assertEqual(second.accepted, first.accepted)
        self.assertEqual(second.bound_object, first.bound_object)
        self.assertEqual(
            tuple((d.field_path, d.action) for d in second.decisions),
            tuple((d.field_path, d.action) for d in first.decisions),
        )
        self.assertEqual(third.bound_object, first.bound_object)

        # 绑定对象与原始输入是独立副本：篡改绑定结果不影响原始输入
        first.bound_object["hostname"] = "mutated"
        first.bound_object["tags"].append("mutated-tag")
        self.assertEqual(record.payload, payload_snapshot)
        again = validate_record(record)
        self.assertEqual(again.bound_object["hostname"], "edge-01")
        self.assertEqual(again.bound_object["tags"], ["prod", "edge"])

    def test_alias_resolution_does_not_rewrite_input_keys(self) -> None:
        record = self._make_record()
        validate_record(record)
        # 原始 payload 仍保留别名键，不会被替换成规范键
        self.assertIn("host", record.payload)
        self.assertIn("timeout", record.payload)
        self.assertNotIn("hostname", record.payload)
        self.assertNotIn("policy_timeout", record.payload)


class AliasConflictTests(unittest.TestCase):
    """互相冲突的别名（同一规范路径、不同取值）给出结构化拒绝。"""

    def test_conflicting_aliases_rejected(self) -> None:
        record = ImportRecord(
            record_id="rec-alias-conflict",
            type_name="UserProfile",
            payload={
                "name": "名字A",       # name -> full_name
                "full_name": "名字B",  # 规范键，取值不同 -> 冲突
                "email": "a@example.com",
            },
            auth_source="admin_console",
        )
        result = validate_record(record)

        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.ALIAS_CONFLICT, codes(result))
        self.assertIn("$.full_name", rejection_paths(result))
        rejection = next(
            r for r in result.rejections
            if r.code is RejectionCode.ALIAS_CONFLICT
        )
        self.assertEqual(rejection.rule_id, authz.RULE_ALIAS_CONFLICT)
        self.assertEqual(rejection.detail["canonical"], "full_name")
        self.assertEqual(rejection.detail["source_keys"], ["name", "full_name"])

    def test_duplicate_alias_same_value_accepted(self) -> None:
        record = ImportRecord(
            record_id="rec-alias-dup-same",
            type_name="UserProfile",
            payload={
                "name": "相同名字",
                "full_name": "相同名字",  # 同值重复，不构成冲突
            },
            auth_source="admin_console",
        )
        result = validate_record(record)
        self.assertTrue(result.accepted, msg=result.rejections)
        self.assertEqual(result.bound_object["full_name"], "相同名字")

    def test_nested_alias_conflict_rejected(self) -> None:
        record = ImportRecord(
            record_id="rec-alias-conflict-nested",
            type_name="UserProfile",
            payload={
                "preferences": {
                    "lang": "zh_CN",
                    "locale": "en_US",  # 与 lang -> locale 冲突
                }
            },
            auth_source="admin_console",
        )
        result = validate_record(record)
        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.ALIAS_CONFLICT, codes(result))
        self.assertIn("$.preferences.locale", rejection_paths(result))


class AuthorizationTests(unittest.TestCase):
    """授权来源不明、不受信任、未授予类型时的结构化拒绝。"""

    def test_unknown_auth_source(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-auth-unknown",
                type_name="UserProfile",
                payload={"full_name": "x"},
                auth_source="mystery_source",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(codes(result), {RejectionCode.UNKNOWN_AUTH_SOURCE})

    def test_untrusted_auth_source(self) -> None:
        self.assertIn("legacy_csv_feed", AUTH_SOURCES)
        result = validate_record(
            ImportRecord(
                record_id="rec-auth-untrusted",
                type_name="UserProfile",
                payload={"full_name": "x"},
                auth_source="legacy_csv_feed",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(codes(result), {RejectionCode.UNTRUSTED_AUTH_SOURCE})
        rejection = result.rejections[0]
        self.assertEqual(rejection.rule_id, authz.RULE_SOURCE_UNTRUSTED)
        self.assertEqual(rejection.schema_version, "1.1")

    def test_type_not_granted(self) -> None:
        # 自助门户只有 UserProfile 授权，不能导入 DeviceConfig
        result = validate_record(
            ImportRecord(
                record_id="rec-auth-grant",
                type_name="DeviceConfig",
                payload={"hostname": "h"},
                auth_source="self_service_portal",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(codes(result), {RejectionCode.TYPE_NOT_GRANTED})
        rejection = result.rejections[0]
        self.assertEqual(
            rejection.detail["grant_rule_id"], "RULE_GRANT_SELF_SERVICE_PORTAL"
        )


class SchemaAndFieldTests(unittest.TestCase):
    """未知类型/版本、未知字段、类型不匹配与版本化别名。"""

    def test_unknown_type(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-unknown-type",
                type_name="Ghost",
                payload={"a": 1},
                auth_source="admin_console",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(codes(result), {RejectionCode.UNKNOWN_TYPE})
        self.assertEqual(result.rejections[0].field_path, "$")

    def test_unknown_schema_version(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-unknown-version",
                type_name="UserProfile",
                payload={"full_name": "x"},
                auth_source="admin_console",
                schema_version="9.9",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(codes(result), {RejectionCode.UNKNOWN_SCHEMA_VERSION})
        self.assertEqual(
            result.rejections[0].detail["available_versions"], ["1.0", "1.1"]
        )

    def test_unknown_field(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-unknown-field",
                type_name="UserProfile",
                payload={"full_name": "x", "hobby": "chess"},
                auth_source="admin_console",
            )
        )
        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.UNKNOWN_FIELD, codes(result))
        self.assertIn("$.hobby", rejection_paths(result))

    def test_scalar_type_mismatch(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-type-mismatch",
                type_name="DeviceConfig",
                # policy_timeout 需要 int；bool 不得冒充 int
                payload={"policy_timeout": True},
                auth_source="partner_sync",
            )
        )
        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.TYPE_MISMATCH, codes(result))

    def test_array_type_mismatch(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-array-mismatch",
                type_name="DeviceConfig",
                payload={"tags": "not-a-list"},
                auth_source="partner_sync",
            )
        )
        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.TYPE_MISMATCH, codes(result))

    def test_malformed_payload(self) -> None:
        result = validate_record(
            ImportRecord(
                record_id="rec-malformed",
                type_name="UserProfile",
                payload=["not", "an", "object"],  # type: ignore[arg-type]
                auth_source="admin_console",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(codes(result), {RejectionCode.MALFORMED_PAYLOAD})

    def test_version_specific_alias_mapping(self) -> None:
        # 1.0：timeout -> timeout_seconds
        v1 = validate_record(
            ImportRecord(
                record_id="rec-alias-v1",
                type_name="DeviceConfig",
                schema_version="1.0",
                payload={"timeout": 45},
                auth_source="partner_sync",
            )
        )
        self.assertTrue(v1.accepted, msg=v1.rejections)
        self.assertEqual(v1.bound_object["timeout_seconds"], 45)
        self.assertEqual(v1.schema_version, "1.0")

        # 1.1：同一别名 timeout -> policy_timeout
        v2 = validate_record(
            ImportRecord(
                record_id="rec-alias-v2",
                type_name="DeviceConfig",
                schema_version="1.1",
                payload={"timeout": 45},
                auth_source="partner_sync",
            )
        )
        self.assertTrue(v2.accepted, msg=v2.rejections)
        self.assertEqual(v2.bound_object["policy_timeout"], 45)
        alias_rule = next(
            d.alias_rule_id for d in v2.decisions if d.source_key == "timeout"
        )
        self.assertEqual(alias_rule, "RULE_ALIAS_DEVICECONFIG_TIMEOUT_V2")

    def test_pinned_old_version_rejects_new_field(self) -> None:
        # 1.0 没有 display_name，即使默认版本是 1.1
        result = validate_record(
            ImportRecord(
                record_id="rec-pin-v1",
                type_name="UserProfile",
                schema_version="1.0",
                payload={"display_name": "新字段"},
                auth_source="admin_console",
            )
        )
        self.assertFalse(result.accepted)
        self.assertIn(RejectionCode.UNKNOWN_FIELD, codes(result))
        self.assertEqual(result.rejections[0].schema_version, "1.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
