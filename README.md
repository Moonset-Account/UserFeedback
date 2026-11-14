# 传感器批次格式头预检（教学接收端）

接收端在**分配任何接收缓冲区之前**调用本预检，依据格式头中的两个计数
字段和实际文件大小判断批次是否装得下。全程只做整数运算，**不会按声明
长度创建大对象**，因此声明长度再夸张也不会引发大内存分配。

仅依赖 Python 3 标准库，无第三方依赖、无需联网。

## 目录结构

```
workspace/
├── app.py                     # CLI 主入口
├── header_precheck.py         # 预检核心库（纯函数，无 I/O）
├── tools/
│   └── local_verify.py        # 本地核验包装器（unittest + 样例断言）
├── tests/
│   ├── __init__.py
│   └── test_header_precheck.py# 回归测试（稳定 case_id）
├── verify_report.json         # 运行核验后生成的证据报告
└── README.md
```

## 预检规则

调用时传入五个非负整数：

| 参数 | 含义 | 范围约束 |
| --- | --- | --- |
| `record_count` | 记录条数（头计数字段 1） | uint32，0 … 4294967295 |
| `record_length` | 每条记录长度（字节，头计数字段 2） | uint32，0 … 4294967295 |
| `header_length` | 格式头长度（字节） | uint32，0 … 4294967295 |
| `actual_file_size` | 实际文件字节数 | 任意非负整数 |
| `resource_budget` | 调用方给定的资源预算（字节） | 任意非负整数 |

约定：

- 格式可表达的最大总长度为 **32 位无符号整数上限**
  `UINT32_MAX = 2^32 - 1 = 4294967295`。
- 声明总长度 `declared_total = header_length + record_count * record_length`。
- **条数为零时载荷长度必须为零**：`record_count == 0` 时
  `record_length` 也必须为 0，否则判字段越界。
- 检查按固定顺序短路，返回**首个**失败原因：
  字段越界 → 声明总长无法表示 → 内容截断 → 尾部多余 → 预算不足。

### 结果代码（稳定标识）

| 代码 | 触发条件 |
| --- | --- |
| `OK` | 全部检查通过 |
| `FIELD_OUT_OF_RANGE` | 参数不是非负整数；某个头字段超过 uint32 上限；或零批次却声明非零记录长度 |
| `TOTAL_LENGTH_UNREPRESENTABLE` | 乘加后的声明总长度超过 `UINT32_MAX` |
| `CONTENT_TRUNCATED` | 实际文件字节数 < 声明总长度（内容截断） |
| `TRAILING_EXCESS` | 实际文件字节数 > 声明总长度（尾部多余） |
| `BUDGET_EXCEEDED` | 声明总长度 > 调用方资源预算 |

## 本地入口（可重跑）

工作区根目录下执行：

```bash
# 1) 一键核验：发现并运行 tests/ 下全部 unittest，再跑样例/边界断言，
#    生成 verify_report.json；全部通过退出 0，否则非零。
/usr/local/bin/python3 tools/local_verify.py

# 2) 只跑单元测试（等价于 discover 方式）
/usr/local/bin/python3 -m unittest discover -s tests -v

# 3) CLI 直接预检（成功退出 0，拒绝退出 1，参数用法错误退出 2）
/usr/local/bin/python3 app.py \
    --record-count 10 --record-length 32 --header-length 16 \
    --actual-file-size 336 --resource-budget 4096
```

### CLI 输入格式

五个必填参数，均为十进制非负整数字符串：
`--record-count`、`--record-length`、`--header-length`、
`--actual-file-size`、`--resource-budget`。

### CLI 输出语义

标准输出一行 JSON（UTF-8），字段：

- `code`：上表中的稳定结果代码；
- `ok`：布尔值，是否通过；
- `declared_total_length`：声明总长度；字段越界导致无法计算时为 `null`；
- `reason`：人类可读的中文说明；
- `input`：回显本次输入参数。

退出码：`0` 通过；`1` 预检拒绝；`2` 命令行参数无效。

## 需求到测试的映射

| 原始需求 | 覆盖用例（稳定 case_id） | 断言内容 |
| --- | --- | --- |
| 分配缓冲区前判断是否装得下，不按声明长度创建大对象 | `NoLargeAllocationTests.test_astronomical_declared_length_returns_instantly_without_buffer`、`..._big_field_overflow_does_not_compute_payload_buffer` | 传入 10^30×10^30 的声明长度，1 秒内仅做整数运算返回；字段越界时短路且 `declared_total_length is None` |
| 最大总长度为 uint32 上限 | `boundary-declared-total-equals-uint32-max`、`boundary-declared-total-over-uint32-max-by-one`、`boundary-record-count-equals-uint32-max` | 总长恰为 2^32−1 通过；恰为 2^32 判无法表示 |
| 条数为零时载荷长度为零 | `ok-zero-count-zero-payload`、`zero-count-nonzero-record-length-rejected`、`zero-count-header-only-trailing-excess` | 零批次零记录长度通过；非零记录长度判字段越界；零批次多字节判尾部多余 |
| 计数范围检查 | `boundary-record-count-over-uint32-max`、`boundary-record-length-over-uint32-max`、`boundary-header-length-over-uint32-max`、`InvalidArgumentTests`（负数/非整数/bool） | 超 uint32、负数、字符串、浮点、`None`、`bool` 均判字段越界 |
| 乘加后总长度检查 | `multiply-overflow-unrepresentable`、`order-unrepresentable-before-truncation` | 各字段在范围内但乘积超限，判无法表示并给出实际乘积值 |
| 实际长度：截断 / 尾部多余 | `content-truncated-one-byte`、`trailing-excess-one-byte` | 差 1 字节分别命中两类代码 |
| 资源预算检查 | `budget-exceeded-by-one`、`ok-single-record-exact-budget` | 超预算 1 字节判预算不足；预算恰好等于声明总长通过 |
| 结果必须明确区分五类原因 | `test_failure_codes_are_explicit_and_distinct` | 五个失败代码互不相同且都被至少一个用例覆盖 |
| 检查顺序明确（首个失败原因） | `order-field-before-unrepresentable`、`order-unrepresentable-before-truncation`、`order-truncation-before-budget`、`order-trailing-before-budget` | 同时存在多个问题时按约定优先级返回 |
| 正常小样例回归 | `ok-small-sample`（10×32+16=336） | 端到端通过，声明总长精确等于 336 |
| CLI 行为 | `CliTests` 三个用例 | 退出码 0/1/2 与 JSON 字段 |

`tools/local_verify.py` 还会独立执行 9 条样例/边界断言
（`sample-*`、`boundary-*`），逐条在 `verify_report.json` 中保留
case_id、输入、实际输出与期望值。

## 限制与设计说明

- 本实现只做**头预检**：不解析、不拷贝、不缓冲任何载荷字节；调用方
  拿到 `OK` 后才应按 `declared_total_length` 分配资源。
- Python 整数无固定位宽、不会静默回绕，乘加溢出由显式比较
  `> UINT32_MAX` 判定，避免了 C 语言式整数回绕漏洞。
- `bool` 虽是 `int` 子类，预检显式拒绝布尔值，防止 `True/False`
  被静默当成 `1/0`。
- `actual_file_size` 与 `resource_budget` 只要求非负整数，不设
  uint32 上限（实际文件与预算可以大于 4 GiB）；受 uint32 表达约束的
  只有三个头字段与声明总长度。
- 不使用 pip、venv 或任何第三方库；不访问网络与外部系统。
