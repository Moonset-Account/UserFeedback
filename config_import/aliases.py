"""字段别名表。

别名绑定到具体对象类型，并声明兼容的模式版本范围（闭区间）。
递归处理嵌套对象时，每一层都用当前嵌套类型自己的别名表解析，
因此嵌套字段也可以拥有别名（例如 Contact.mail -> Contact.email）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AliasMapping:
    type_name: str
    alias: str
    canonical: str
    min_version: str
    max_version: str
    rule_id: str

    def supports(self, version: str) -> bool:
        return _version_key(self.min_version) <= _version_key(version) <= _version_key(
            self.max_version
        )


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


# 规则 ID 即授权/校验规则的回溯标识，与 authz.py 中的规则 ID 同体系
ALIAS_TABLES: dict[str, tuple[AliasMapping, ...]] = {
    "UserProfile": (
        AliasMapping("UserProfile", "name", "full_name", "1.0", "1.1",
                     "RULE_ALIAS_USERPROFILE_NAME"),
        AliasMapping("UserProfile", "mail", "email", "1.0", "1.1",
                     "RULE_ALIAS_USERPROFILE_MAIL"),
        AliasMapping("UserProfile", "prefs", "preferences", "1.0", "1.1",
                     "RULE_ALIAS_USERPROFILE_PREFS"),
    ),
    "Preferences": (
        AliasMapping("Preferences", "lang", "locale", "1.0", "1.0",
                     "RULE_ALIAS_PREFERENCES_LANG"),
        AliasMapping("Preferences", "tz", "timezone", "1.0", "1.0",
                     "RULE_ALIAS_PREFERENCES_TZ"),
        AliasMapping("Preferences", "marketing_email", "email_opt_in", "1.0", "1.0",
                     "RULE_ALIAS_PREFERENCES_MARKETING"),
    ),
    "Contact": (
        AliasMapping("Contact", "mail", "email", "1.0", "1.0",
                     "RULE_ALIAS_CONTACT_MAIL"),
        AliasMapping("Contact", "tel", "phone", "1.0", "1.0",
                     "RULE_ALIAS_CONTACT_TEL"),
    ),
    "DeviceConfig": (
        # 同一别名在不同模式版本上映射到不同规范字段，体现版本兼容范围
        AliasMapping("DeviceConfig", "timeout", "timeout_seconds", "1.0", "1.0",
                     "RULE_ALIAS_DEVICECONFIG_TIMEOUT_V1"),
        AliasMapping("DeviceConfig", "timeout", "policy_timeout", "1.1", "1.1",
                     "RULE_ALIAS_DEVICECONFIG_TIMEOUT_V2"),
        AliasMapping("DeviceConfig", "host", "hostname", "1.0", "1.1",
                     "RULE_ALIAS_DEVICECONFIG_HOST"),
    ),
    "NotificationPolicy": (
        AliasMapping("NotificationPolicy", "retain_days", "retention_days",
                     "1.0", "1.0", "RULE_ALIAS_NOTIFPOLICY_RETAIN"),
    ),
}


def resolve_alias(type_name: str, version: str, key: str) -> AliasMapping | None:
    """返回 (type_name, version) 下对 key 生效的别名映射；不存在则 None。"""
    for mapping in ALIAS_TABLES.get(type_name, ()):
        if mapping.alias == key and mapping.supports(version):
            return mapping
    return None
