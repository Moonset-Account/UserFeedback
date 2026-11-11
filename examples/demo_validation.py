"""端到端演示：合法嵌套更新、各类结构化拒绝、服务端字段保留。

运行（仓库根目录）：
    python3 -I examples/demo_validation.py
"""

from __future__ import annotations

import os
import pprint
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_import import ImportRecord, validate_batch  # noqa: E402


def show(title: str, result) -> None:
    print(f"\n{'=' * 72}\n{title}  (record={result.record_id})\n{'-' * 72}")
    print(f"accepted       : {result.accepted}")
    print(f"schema_version : {result.schema_version}")
    if result.rejections:
        print("rejections:")
        for rej in result.rejections:
            print(
                f"  - [{rej.code.value}] path={rej.field_path} "
                f"rule={rej.rule_id} version={rej.schema_version}"
            )
            print(f"      {rej.message}")
    if result.bound_object is not None:
        print("bound_object:")
        pprint.pprint(result.bound_object, sort_dicts=False, indent=2)
    if result.decisions:
        print("trace (first 6 decisions):")
        for dec in result.decisions[:6]:
            alias = f" alias={dec.alias_rule_id}" if dec.alias_rule_id else ""
            print(
                f"  - path={dec.field_path} action={dec.action} "
                f"version={dec.schema_version} rule={dec.rule_id}{alias}"
            )


def main() -> None:
    existing_user = {
        "user_id": "u-1001",
        "created_at": "2026-01-01T00:00:00Z",
        "preferences": {"prefs_fingerprint": "fp-server-only"},
    }

    records = [
        # 1) 合法：顶层 + 嵌套 + 数组，全程使用别名
        ImportRecord(
            record_id="ok-nested",
            type_name="UserProfile",
            payload={
                "name": "张三",
                "prefs": {"lang": "zh_CN", "tz": "Asia/Shanghai"},
                "contacts": [{"mail": "work@example.com", "tel": "13800000000"}],
            },
            auth_source="admin_console",
            existing_state=existing_user,
        ),
        # 2) 别名冲突：name 与 full_name 同路径不同值
        ImportRecord(
            record_id="bad-alias-conflict",
            type_name="UserProfile",
            payload={"name": "A", "full_name": "B"},
            auth_source="admin_console",
        ),
        # 3) 服务端字段：嵌套层尝试写内部字段
        ImportRecord(
            record_id="bad-server-field",
            type_name="UserProfile",
            payload={"preferences": {"prefs_fingerprint": "tampered"}},
            auth_source="admin_console",
        ),
        # 4) 授权来源不明
        ImportRecord(
            record_id="bad-unknown-source",
            type_name="UserProfile",
            payload={"full_name": "x"},
            auth_source="mystery",
        ),
        # 5) 来源受信任但未被授予该类型
        ImportRecord(
            record_id="bad-not-granted",
            type_name="DeviceConfig",
            payload={"hostname": "h"},
            auth_source="self_service_portal",
        ),
        # 6) 未知类型
        ImportRecord(
            record_id="bad-unknown-type",
            type_name="Ghost",
            payload={"x": 1},
            auth_source="admin_console",
        ),
    ]

    results = validate_batch(records)
    for title, result in zip(
        [
            "1. 合法嵌套更新（别名规范化 + 服务端字段保留）",
            "2. 别名冲突的结构化拒绝",
            "3. 嵌套服务端字段的结构化拒绝",
            "4. 授权来源不明",
            "5. 类型未授权",
            "6. 未知对象类型",
        ],
        results,
    ):
        show(title, result)

    accepted = [r.record_id for r in results if r.accepted]
    rejected = [r.record_id for r in results if not r.accepted]
    print(f"\n{'=' * 72}")
    print(f"批次汇总：通过 {accepted}；拒绝 {rejected}（坏记录互不影响）")


if __name__ == "__main__":
    main()
