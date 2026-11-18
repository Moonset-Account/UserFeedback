"""最小使用示例：核查离线加密回执中的随机数复用。

运行方式（工作区根目录）：
    python3 -I examples/demo_audit.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nonce_audit import EncryptionRecord, EquivalenceTable, audit  # noqa: E402


def main() -> None:
    # 密钥材料等价关系：v1 中 alias-a/alias-b 指向同一份材料，alias-c 独立；
    # v2 是成功换钥后的新版本。
    table = EquivalenceTable.from_alias_groups(
        {
            "v1": [["alias-a", "alias-b"], ["alias-c"]],
            "v2": [["alias-a", "alias-b"]],
        }
    )

    records = [
        # 同一操作经两个来源重复上报、内容一致 -> 只算一次
        EncryptionRecord("op-1", "w-1", "n-9", "alias-a", "v1", "rcpt-1"),
        EncryptionRecord("op-1", "w-1", "n-9", "alias-a", "v1", "rcpt-2"),
        # 同一份材料经另一别名使用且 nonce 相同 -> 需复核的操作对
        EncryptionRecord("op-2", "w-2", "n-9", "alias-b", "v1", "rcpt-2"),
        # 换钥后的同 nonce 记录 -> 版本不同，不与 op-1/op-2 配对
        EncryptionRecord("op-3", "w-3", "n-9", "alias-a", "v2", "rcpt-3"),
        # 别名在 v1 中没有映射 -> 待确认
        EncryptionRecord("op-4", "w-4", "n-0", "alias-x", "v1", "rcpt-4"),
    ]

    result = audit(records, table)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
