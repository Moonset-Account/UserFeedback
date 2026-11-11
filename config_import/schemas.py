"""版本化对象模式注册表。

每个对象类型可拥有多个不兼容/兼容版本；字段显式标注 writable：
    writable=True  -> 用户可更新的显示字段
    writable=False -> 只能由服务端设置的内部字段
"""

from __future__ import annotations

from .models import FieldKind, FieldSpec, SchemaVersion

# ---------------------------------------------------------------------------
# 复用的嵌套对象：用户偏好（UserProfile.preferences 引用）
# ---------------------------------------------------------------------------
PREFERENCES_1_0 = SchemaVersion(
    type_name="Preferences",
    version="1.0",
    changelog="初始版本",
    fields={
        "locale": FieldSpec("locale", FieldKind.SCALAR, True, scalar_type=str),
        "timezone": FieldSpec("timezone", FieldKind.SCALAR, True, scalar_type=str),
        "email_opt_in": FieldSpec(
            "email_opt_in", FieldKind.SCALAR, True, scalar_type=bool
        ),
        # 服务端内部字段：偏好指纹由服务端计算，导入不可写
        "prefs_fingerprint": FieldSpec(
            "prefs_fingerprint", FieldKind.SCALAR, False, scalar_type=str
        ),
    },
)

# 复用的嵌套对象：联系方式（UserProfile / DeviceConfig 均引用）
CONTACT_1_0 = SchemaVersion(
    type_name="Contact",
    version="1.0",
    changelog="初始版本",
    fields={
        "email": FieldSpec("email", FieldKind.SCALAR, True, scalar_type=str),
        "phone": FieldSpec("phone", FieldKind.SCALAR, True, scalar_type=str),
        # 服务端内部字段：联系方式校验状态
        "verified": FieldSpec("verified", FieldKind.SCALAR, False, scalar_type=bool),
    },
)

# ---------------------------------------------------------------------------
# UserProfile：1.0 与 1.1 两个版本（1.1 新增 display_name）
# ---------------------------------------------------------------------------
USER_PROFILE_1_0 = SchemaVersion(
    type_name="UserProfile",
    version="1.0",
    changelog="初始版本",
    fields={
        "user_id": FieldSpec("user_id", FieldKind.SCALAR, False, scalar_type=str),
        "full_name": FieldSpec("full_name", FieldKind.SCALAR, True, scalar_type=str),
        "email": FieldSpec("email", FieldKind.SCALAR, True, scalar_type=str),
        "preferences": FieldSpec(
            "preferences", FieldKind.NESTED, True, nested_target="Preferences"
        ),
        "contacts": FieldSpec(
            "contacts", FieldKind.ARRAY, True, item_type="Contact"
        ),
        # 服务端内部字段
        "created_at": FieldSpec(
            "created_at", FieldKind.SCALAR, False, scalar_type=str
        ),
    },
)

USER_PROFILE_1_1 = SchemaVersion(
    type_name="UserProfile",
    version="1.1",
    changelog="新增 display_name 显示字段",
    fields={
        "user_id": FieldSpec("user_id", FieldKind.SCALAR, False, scalar_type=str),
        "full_name": FieldSpec("full_name", FieldKind.SCALAR, True, scalar_type=str),
        "display_name": FieldSpec(
            "display_name", FieldKind.SCALAR, True, scalar_type=str
        ),
        "email": FieldSpec("email", FieldKind.SCALAR, True, scalar_type=str),
        "preferences": FieldSpec(
            "preferences", FieldKind.NESTED, True, nested_target="Preferences"
        ),
        "contacts": FieldSpec(
            "contacts", FieldKind.ARRAY, True, item_type="Contact"
        ),
        "created_at": FieldSpec(
            "created_at", FieldKind.SCALAR, False, scalar_type=str
        ),
    },
)

# ---------------------------------------------------------------------------
# DeviceConfig：1.0 与 1.1（1.1 将 timeout_seconds 更名 policy_timeout）
# ---------------------------------------------------------------------------
DEVICE_CONFIG_1_0 = SchemaVersion(
    type_name="DeviceConfig",
    version="1.0",
    changelog="初始版本",
    fields={
        "device_id": FieldSpec("device_id", FieldKind.SCALAR, False, scalar_type=str),
        "hostname": FieldSpec("hostname", FieldKind.SCALAR, True, scalar_type=str),
        "timeout_seconds": FieldSpec(
            "timeout_seconds", FieldKind.SCALAR, True, scalar_type=int
        ),
        "tags": FieldSpec("tags", FieldKind.ARRAY, True, item_type=str),
        "contact": FieldSpec(
            "contact", FieldKind.NESTED, True, nested_target="Contact"
        ),
        # 服务端内部字段：登记时间与合规状态
        "enrolled_at": FieldSpec(
            "enrolled_at", FieldKind.SCALAR, False, scalar_type=str
        ),
        "compliance_state": FieldSpec(
            "compliance_state", FieldKind.SCALAR, False, scalar_type=str
        ),
    },
)

DEVICE_CONFIG_1_1 = SchemaVersion(
    type_name="DeviceConfig",
    version="1.1",
    changelog="timeout_seconds 更名为 policy_timeout",
    fields={
        "device_id": FieldSpec("device_id", FieldKind.SCALAR, False, scalar_type=str),
        "hostname": FieldSpec("hostname", FieldKind.SCALAR, True, scalar_type=str),
        "policy_timeout": FieldSpec(
            "policy_timeout", FieldKind.SCALAR, True, scalar_type=int
        ),
        "tags": FieldSpec("tags", FieldKind.ARRAY, True, item_type=str),
        "contact": FieldSpec(
            "contact", FieldKind.NESTED, True, nested_target="Contact"
        ),
        "enrolled_at": FieldSpec(
            "enrolled_at", FieldKind.SCALAR, False, scalar_type=str
        ),
        "compliance_state": FieldSpec(
            "compliance_state", FieldKind.SCALAR, False, scalar_type=str
        ),
    },
)

# ---------------------------------------------------------------------------
# NotificationPolicy：单版本
# ---------------------------------------------------------------------------
NOTIFICATION_POLICY_1_0 = SchemaVersion(
    type_name="NotificationPolicy",
    version="1.0",
    changelog="初始版本",
    fields={
        "policy_id": FieldSpec("policy_id", FieldKind.SCALAR, False, scalar_type=str),
        "channel": FieldSpec("channel", FieldKind.SCALAR, True, scalar_type=str),
        "enabled": FieldSpec("enabled", FieldKind.SCALAR, True, scalar_type=bool),
        "retention_days": FieldSpec(
            "retention_days", FieldKind.SCALAR, True, scalar_type=int
        ),
        # 服务端内部字段：最近下发时间
        "last_dispatched_at": FieldSpec(
            "last_dispatched_at", FieldKind.SCALAR, False, scalar_type=str
        ),
    },
)

SCHEMA_REGISTRY: dict[str, dict[str, SchemaVersion]] = {
    "UserProfile": {
        "1.0": USER_PROFILE_1_0,
        "1.1": USER_PROFILE_1_1,
    },
    "DeviceConfig": {
        "1.0": DEVICE_CONFIG_1_0,
        "1.1": DEVICE_CONFIG_1_1,
    },
    "NotificationPolicy": {
        "1.0": NOTIFICATION_POLICY_1_0,
    },
    # 嵌套对象类型也登记在同一注册表中
    "Preferences": {"1.0": PREFERENCES_1_0},
    "Contact": {"1.0": CONTACT_1_0},
}

# 未显式指定版本时选用的默认版本
DEFAULT_VERSIONS: dict[str, str] = {
    "UserProfile": "1.1",
    "DeviceConfig": "1.1",
    "NotificationPolicy": "1.0",
    "Preferences": "1.0",
    "Contact": "1.0",
}


def get_schema(type_name: str, version: str) -> SchemaVersion | None:
    return SCHEMA_REGISTRY.get(type_name, {}).get(version)


def get_latest_schema(type_name: str) -> SchemaVersion | None:
    versions = SCHEMA_REGISTRY.get(type_name)
    if not versions:
        return None
    default_version = DEFAULT_VERSIONS.get(type_name)
    if default_version and default_version in versions:
        return versions[default_version]
    # 兜底：按版本字符串取最大
    return versions[sorted(versions)[-1]]
