"""授权来源注册表与字段写权限规则。

授权判定分两层：
1. 来源本身必须受信任（trusted），且被授予目标对象类型（type_grants）；
2. 即使来源通过类型授权，server_only 字段对任何来源都不可写。

每条规则都有稳定的规则 ID，校验结果通过规则 ID 回溯到授权依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthSource:
    source_id: str
    display_name: str
    trusted: bool
    type_grants: frozenset[str]
    grant_rule_id: str


# 规则 ID 集中定义，校验器与测试都引用这些常量，避免魔法字符串
RULE_SOURCE_TRUSTED = "RULE_AUTHZ_SOURCE_TRUSTED"
RULE_SOURCE_UNTRUSTED = "RULE_AUTHZ_SOURCE_UNTRUSTED"
RULE_TYPE_GRANT = "RULE_AUTHZ_TYPE_GRANT"
RULE_TYPE_NOT_GRANTED = "RULE_AUTHZ_TYPE_NOT_GRANTED"
RULE_WRITABLE_FIELD = "RULE_FIELD_USER_WRITABLE"
RULE_SERVER_FIELD = "RULE_FIELD_SERVER_ONLY"
RULE_UNKNOWN_FIELD = "RULE_FIELD_UNKNOWN"
RULE_ALIAS_COMPATIBLE = "RULE_ALIAS_COMPATIBLE"
RULE_TYPE_MISMATCH = "RULE_FIELD_TYPE_MISMATCH"
RULE_ALIAS_CONFLICT = "RULE_ALIAS_CONFLICT"
RULE_MALFORMED_PAYLOAD = "RULE_PAYLOAD_MALFORMED"
RULE_UNKNOWN_TYPE = "RULE_SCHEMA_UNKNOWN_TYPE"
RULE_UNKNOWN_VERSION = "RULE_SCHEMA_UNKNOWN_VERSION"


AUTH_SOURCES: dict[str, AuthSource] = {
    "admin_console": AuthSource(
        source_id="admin_console",
        display_name="管理控制台",
        trusted=True,
        type_grants=frozenset(
            {"UserProfile", "DeviceConfig", "NotificationPolicy"}
        ),
        grant_rule_id="RULE_GRANT_ADMIN_CONSOLE",
    ),
    "self_service_portal": AuthSource(
        source_id="self_service_portal",
        display_name="自助门户",
        trusted=True,
        # 门户只能更新用户档案，不能碰设备配置 / 通知策略
        type_grants=frozenset({"UserProfile"}),
        grant_rule_id="RULE_GRANT_SELF_SERVICE_PORTAL",
    ),
    "partner_sync": AuthSource(
        source_id="partner_sync",
        display_name="合作伙伴同步",
        trusted=True,
        type_grants=frozenset({"DeviceConfig"}),
        grant_rule_id="RULE_GRANT_PARTNER_SYNC",
    ),
    # 不受信任的来源：已注册但信任标记为 False，模拟授权来源不明/失效
    "legacy_csv_feed": AuthSource(
        source_id="legacy_csv_feed",
        display_name="遗留 CSV 通道（已停用信任）",
        trusted=False,
        type_grants=frozenset({"UserProfile", "DeviceConfig"}),
        grant_rule_id="RULE_GRANT_LEGACY_CSV",
    ),
}


def get_auth_source(source_id: str) -> AuthSource | None:
    return AUTH_SOURCES.get(source_id)
