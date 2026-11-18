"""离线写入回执的随机数（nonce）复用核查库。

核查输入（均为元数据，不含密钥内容）：

* 密钥材料的等价关系版本（mapping_version）：某一版本下，多个密钥别名
  可能指向同一份底层密钥材料，需要按材料等价类合并判断；
* 随机数标记（nonce）：每次实际加密使用的随机数；
* 逻辑写入编号（logical_write_id）：一次逻辑写入；
* 实际加密操作编号（operation_id）：同一次操作重复上报只算一次，超时后
  以新操作编号重新执行的加密即使逻辑写入编号相同，也独立核对。

规则摘要：

1. 相同 operation_id 的重复上报去重；若重复上报内容冲突，则该操作标记为
   待确认，不参与复用结论。
2. 同一等价关系版本、同一密钥材料等价类、同一 nonce 下出现两个不同操作，
   即构成需要复核的操作对（包括同一逻辑写入超时重执行的情况）。
3. 同一份材料通过两个密钥别名使用时按材料等价类合并判断。
4. 成功换钥前后的等价关系版本不同，记录分开判断，不会跨版本配对。
5. 别名在对应版本中缺少映射时，该条记录返回待确认；与其同版本、同 nonce、
   同（可解析的）密钥材料类的操作也无法确认，一并列入待确认。
6. 输出只使用不透明的密钥材料类标识（material-class-*），不回传别名、
   材料标识原文或任何密钥内容。
"""

from .core import (
    AuditInputError,
    AuditResult,
    EncryptionRecord,
    EquivalenceTable,
    PendingItem,
    ReusePair,
    audit,
)

__all__ = [
    "AuditInputError",
    "AuditResult",
    "EncryptionRecord",
    "EquivalenceTable",
    "PendingItem",
    "ReusePair",
    "audit",
]
