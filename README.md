# nonce_audit —— 离线写入回执随机数复用核查库

面向存储组的离线核查场景：基于加密组件的写入回执元数据（**不含密钥内容**），
找出同一密钥材料下随机数（nonce）被多次使用的实际加密操作对，并输出每条
结论的来源以便复核。

## 实际入口

| 用途 | 入口 |
| --- | --- |
| 库 API | `nonce_audit.audit(records, equivalence)` |
| 核心类型 | `nonce_audit.EncryptionRecord` / `EquivalenceTable` / `AuditResult` / `ReusePair` / `PendingItem` / `AuditInputError` |
| 验收测试 | `python3 -I -m unittest discover -s tests -v` |
| 使用示例 | `python3 -I examples/demo_audit.py` |

无第三方依赖，标准库 Python 3.11 即可运行。

### 输入模型

`EncryptionRecord`（亦接受同构字典）：

| 字段 | 含义 |
| --- | --- |
| `operation_id` | 实际加密的操作编号；相同编号的重复上报只算一次 |
| `logical_write_id` | 逻辑写入编号；超时重执行可能与首次相同 |
| `nonce` | 随机数标记，相等即视为取值相同 |
| `key_alias` | 本次加密使用的密钥别名 |
| `mapping_version` | 本次加密生效的密钥等价关系版本 |
| `source_id` | 上报来源（回执文件/通道），用于追溯 |

`EquivalenceTable` 描述各版本下「密钥别名 → 不透明材料标识」的映射；
可用 `EquivalenceTable.from_alias_groups({"v1": [["alias-a", "alias-b"], ...]})`
按别名等价类快速构造。换钥成功后应登记为新版本。

### 输出模型（`AuditResult.to_dict()`）

- `reuse_pairs`：需要复核的操作对，含双方操作编号、涉及的逻辑写入编号、
  nonce、**不透明**密钥材料类标识 `material-class-N`、等价关系版本及全部来源；
- `pending`：无法自动定论的操作，含原因（`mapping_missing` /
  `duplicate_conflict`）、来源、别名是否在该版本登记（仅布尔值）以及关联的
  相邻操作 `peer_operation_ids`；
- `deduplicated_reports` / `unique_operations`：去重统计。

## 核查规则与题面约束的对应

1. **相同操作重复上报只算一次**：按 `operation_id` 去重；`source_id` 仅表示
   来源，不同来源的一致上报属正常重复。若重复上报的业务字段互相冲突，该操作
   标记 `duplicate_conflict` 待确认，不参与复用结论。
2. **超时重执行独立核对**：判定单位是 `operation_id`。相同 `logical_write_id`
   但操作编号不同的两次加密，只要同版本、同材料类、同 nonce 即报为复用对。
3. **同一材料的两个别名合并判断**：先经等价关系表把别名归并到材料等价类，
   再按材料类分桶比较 nonce。
4. **换钥前后分开**：分桶键包含 `mapping_version`，跨版本记录永不配对；
   不透明材料类标识也按版本独立编号。
5. **映射缺失返回待确认**：版本缺失或别名未登记时，该记录以
   `mapping_missing` 进入 `pending`；同版本同 nonce 的已解析操作无法确认是否
   同样构成复用，作为 `peer_operation_ids` 一并列出，且不会产生复用结论。
6. **不回传密钥内容**：输出只含 `material-class-N` 不透明标识；待确认项不含
   别名原文，仅回传别名是否已登记的布尔值。材料标识仅在库内归并使用。

## 验证范围（tests/test_nonce_audit.py，14 个用例，真实断言）

- 可接受结果：别名等价合并报复用、超时重执行（同写入编号）报复用、换钥后
  新版本内部复用报复用；
- 应被阻止/拒绝的结果：换钥前后同 nonce 不配对、不同密钥材料同 nonce 不配对、
  重复上报冲突的操作不进入结论、映射缺失不产生复用对；
- 边界与交错场景：多来源一致重复上报的去重与计数、待确认项对相邻操作的关联、
  输出序列化结果不含任何别名/材料标识原文、材料类标识按版本稳定且相互独立；
- 输入健壮性：字典形式输入、整个版本缺失、空输入、缺字段/非法类型抛
  `AuditInputError`。

运行：

```bash
python3 -I -m unittest discover -s tests -v
```

## 目录结构

```
nonce_audit/
    __init__.py      # 公共 API 导出
    core.py          # 数据模型与 audit 核查逻辑
tests/
    test_nonce_audit.py
examples/
    demo_audit.py
README.md
```
