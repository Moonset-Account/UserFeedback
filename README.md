# 外发审查模块（outbound_review）

识别外发表格中与**内部名单同一条完整记录**匹配的行。内部名单已用固定测试钥匙
预处理：每个值先按列规范化，再做列绑定的 HMAC-SHA256 摘要。审查模块接收分片
（分页）送来的表格单元、列名与逻辑行边界，重建可能跨页的逻辑行，并仅在
**同一逻辑行中至少两个不同指定列指向同一个内部行标识**时判定命中。

## 核心规则（对应题面交叉约束）

1. **命中阈值**：同一外发逻辑行内，至少 2 个**不同**指定列，经摘要比对后指向
   同一个内部行代号（`row_code`），才算命中。
2. **同列只算一列**：同一列在一行中出现多次（含跨页重复、规范化后等价），
   计数时仍只算 1 列；所有触发单元的位置都会保留以便复核。
3. **同姓 / 同部门不误伤**：一个（列, 摘要）可对应多个内部行；仅当多个不同列
   收敛到**同一个**行代号时才命中。两个列分别指向不同行不算命中。
4. **逻辑行可跨页**：以 `logical_row` 为边界合并多个分片；同一逻辑行被页打断
   后仍整体判定。
5. **重复页眉不算数据**：显式 `row_type="header"`，或一行中每个单元的值都是其
   列名（大小写不敏感的结构性识别），均跳过且计入 `header_rows_skipped`。
6. **输出不含原值**：报告只含内部行代号、命中列名、外发单元位置（页 / 物理行 /
   列 / 单元引用）；预处理后的名单仅存摘要，原值与摘要都不会出现在命中报告里。

## 目录与入口

```
outbound_review/
  digest.py    # 固定测试钥匙、按列规范化、列绑定 HMAC-SHA256
  roster.py    # RosterEntry / InternalIndex（(列,摘要)->行代号 倒排索引）
  review.py    # ReviewAssembler 分片装配 + 命中判定；run_review 便捷入口
  cli.py       # 命令行：preprocess / review 两个子命令
tests/
  test_outbound_review.py   # unittest 验收用例（15 个）
```

### Python API（主要入口）

```python
from outbound_review import InternalIndex, run_review, TEST_KEY

# 1) 用固定测试钥匙构建内部名单倒排索引（生产中也可载入已预处理的摘要名单）
index = InternalIndex.build(raw_roster_rows, key=TEST_KEY)

# 2) 喂入分片并审查
report = run_review(index, shards)
for hit in report.hits:
    hit.row_code            # 内部行代号
    hit.matched_columns     # 命中的不同列（>=2）
    hit.cell_locations      # 外发单元位置，供复核定位
```

### 命令行入口

```bash
# 原始名单 JSON -> 仅含摘要的预处理名单
python3 -m outbound_review.cli preprocess \
    --roster roster_raw.json --out roster_digests.json

# 预处理名单 + 外发分片 -> 审查报告（不含原值）
python3 -m outbound_review.cli review \
    --roster roster_digests.json --shards shards.json --out report.json
# 可选 --columns name,email 限定指定列；默认使用名单中出现的全部列
```

## 数据契约

分片单元：

```json
{
  "logical_row": "L1",
  "column": "email",
  "value": "a@b.com",
  "physical_row": 2,
  "cell_ref": "B2",
  "row_type": "data"
}
```

- `logical_row`：逻辑行标识，跨页相同即同一行；页眉行可留空。
- `row_type`：`data`（默认）或 `header`；页眉也支持按内容自动识别。
- 已交付单元按 `cell_ref` 去重，重投不会产生重复位置。

按列规范化（`digest.py`）：

| 列 | 规范化 |
| --- | --- |
| `name` / `department` | NFKC、折叠空白、小写化 |
| `email` | 去空白、小写化 |
| `employee_id` | 去空白、大写化 |
| `phone` | 仅保留数字 |
| 其它列 | 通用文本规范化 |

摘要消息为 `列名 + 0x1F + 规范化值`，使不同列即使原值相同也不可互换比较；
比较使用恒定时间的 `hmac.compare_digest`。

## 验证范围（tests/test_outbound_review.py，15 个用例）

真实断言，覆盖一个可接受结果、一个被阻止结果，以及影响二者的边界 / 交错场景：

- **可接受命中**：name+email（+department）同指 R-1001，校验代号、命中列与位置。
- **应被阻止**：仅 name 一列、即使同列重复 3 个单元也不命中。
- **边界 / 交错**：
  - 两人同姓且同部门，name/department 各指两行，靠 email 消歧只命中正确行；
  - 两列分别指向不同内部行 → 不命中；
  - 两个逻辑行跨页交错，分别装配、分别判定；
  - 逻辑行跨页 + 重复页眉跳过 + 同列多单元只算一列；
  - 启发式页眉识别（值即列名，无显式标记）；
  - 非指定列不参与计数；空单元不参与匹配；重复投递去重。
- **安全断言**：预处理名单与命中报告的序列化结果中均不出现任何原值。
- **CLI 端到端**：子进程真实执行 `preprocess` 与 `review`，校验文件产物与命中结果。

运行方式：

```bash
python3 -I -m unittest discover -s tests -v
```

`-I` 隔离模式下测试文件会自行把仓库根加入 `sys.path`，不依赖环境变量或已安装包。

## 安全说明与边界

- `TEST_KEY` 仅为本地 / 测试固定钥匙，非生产凭据；生产应通过受控方式注入密钥。
- 本模块只做本地、确定性的防御性比对，不发起任何网络访问或目标探测。
- 报告刻意不回显摘要值，避免把摘要当作可逆标识外泄；定位信息只保留外发单元坐标。
