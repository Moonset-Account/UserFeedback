# DNS label-index integrity validator

校验 DNS 解码模块在**展开名称之前**产出的标签索引，让后续模块只消费通过
完整性检查的资源记录。纯标准库实现，无第三方依赖。

## 输入

- **节点表** `nodes: Mapping[int, NodeRecord]` —— 以报文线格式位置为键，
  每条 `NodeRecord` 描述：
  - `label_length`：标签长度（根标签为 `0`）
  - `is_label_start`：该位置是否为合法标签起点
  - `next_position`：后继位置；`None` 即名称终止标记
- **资源记录索引** `records: Iterable[ResourceRecord]` —— 每条记录给出
  `record_id` 与其引用名称的起始位置 `name_start`
- 调用方预算（仅关键字参数）：
  - `domain_size`：合法位置域 `[0, domain_size)`
  - `max_expanded_bytes`：单个名称允许的最大展开字节数
    （每个标签计 `1 + label_length`，终止根标签计 `1`）
  - `jump_budget`：单条名称允许的最大压缩指针跳转次数
    （后继不等于物理相邻位置 `pos + 1 + label_length` 即计一次跳转）

## 输出

每个**被引用的名称**返回一条冻结的 `NameReport`，包含判定 `ok`、首个
不符合规则的位置 `error_position`、受影响的记录编号
`affected_records`（保持资源记录索引顺序），以及诊断用的
`expanded_bytes`、`jumps_used`、`path`。报告按名称在输入中的首次出现
顺序排列，多条记录引用同一名称时合并为一条报告。

## 可区分的错误码

| `IntegrityError`          | 触发条件 |
| ------------------------- | -------- |
| `OUT_OF_BOUNDS`           | 位置落在 `[0, domain_size)` 之外 |
| `INCOMPLETE_EVIDENCE`     | 位置在域内但节点表缺记录，或非根节点带终止标记 |
| `NON_LABEL_START`         | 位置已索引但不是合法标签起点 |
| `LOOP`                    | 单条名称路径再次到达同一位置 |
| `EXPANSION_EXCEEDED`      | 展开字节数超过调用方上限 |
| `JUMP_BUDGET_EXCEEDED`    | 压缩指针跳转超过调用方预算 |

共享尾部（多条名称独立引用同一段后缀）不会被判为循环：循环检测的访问
集合是每条名称独立维护的。

## 使用示例

```python
from dns_index_validator import (
    NodeRecord, ResourceRecord, validate_name_index,
)

nodes = {
    0: NodeRecord(0, 3, True, 4),
    4: NodeRecord(4, 3, True, 8),       # 后继即物理相邻位置，非跳转
    8: NodeRecord(8, 3, True, 40),      # 压缩跳转进入共享尾部
    40: NodeRecord(40, 3, True, 44),
    44: NodeRecord(44, 3, True, 48),
    48: NodeRecord(48, 0, True, None),  # 根标签 / 终止标记
}
records = [ResourceRecord(1, 0)]

reports = validate_name_index(
    nodes, records,
    domain_size=60,
    max_expanded_bytes=30,
    jump_budget=2,
)
assert reports[0].ok and reports[0].expanded_bytes == 21
```

## 运行测试

支持隔离模式运行（不读取环境变量与当前目录注入）：

```bash
python3 -I tests/run_tests.py
```
