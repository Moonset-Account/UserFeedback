# 离线密钥启用仪式保管人调度器

仅使用 Python 3.11.6 标准库实现。给定不超过 12 名保管人的可用时段、可持有槽位、管理线/代理替班关系，以及每轮必须同时在场的槽位，程序会：

1. 枚举每轮所有合法值守配置；
2. 检查同一轮一人一槽、时段/槽位资格、同一把密钥双人保管独立性；
3. 检查相邻轮的连续性与交接条件；
4. 用前向动态规划求总换人次数最少的序列并重建路径；
5. 列出每轮可启用的密钥和每次交接的参与者；
6. 若某轮单看可配人、但从首轮无法连续抵达，定位第一个不可达轮次；若某轮本身无合法配置，则定位本地不可配轮次。

不包含联网、真实目标、凭据读取、依赖安装或任何攻击/利用逻辑。

## 运行测试

```bash
python3 --version
# Python 3.11.6

python3 -m unittest discover -s tests -v
```

也可以运行命令行：

```bash
python3 -m key_ceremony examples/basic.json
python3 -m key_ceremony examples/unreachable.json   # 预期退出码 2
cat examples/basic.json | python3 -m key_ceremony
python3 solution.py examples/basic.json
```

CLI 成功且存在可行序列时退出码为 `0`；规范合法但仪式不可行时为 `2`；输入 JSON/结构错误为 `1`。

## 公开 API

主要入口位于 `key_ceremony`：

```python
from key_ceremony import (
    load_spec,
    schedule_ceremony,
    schedule_from_dict,
    schedule_from_json,
    find_first_unreachable_round,
    minimum_change_schedule,
    CeremonyInputError,
)

result = schedule_ceremony(spec_dict)          # ScheduleResult 对象
payload = result.to_dict()                     # JSON 可序列化字典

payload = schedule_from_dict(spec_dict)        # 直接返回字典
payload = schedule_from_json(json_text)        # 解析 JSON 文本并返回字典
```

根目录的 `solution.py` 也提供便捷函数：

```python
from solution import solve, schedule

payload = solve(spec_dict)       # dict
result = schedule(spec_dict)     # ScheduleResult
```

`schedule_ceremony(..., handover_trigger="disjoint_crew")` 可将交接触发条件从“两个固定席都换人”切换为“新旧两组人完全不重叠”。默认采用更贴近题面字面的固定席判断。

## 输入格式

最小完整示例：

```json
{
  "people": [
    {
      "id": "A",
      "management_line": "L1",
      "eligible_slots": ["k1:a", "k1:b"],
      "availability": [["2026-11-26T09:00:00", "2026-11-26T17:00:00"]],
      "available_rounds": ["morning"]
    },
    {
      "id": "B",
      "management_line": "L2",
      "eligible_slots": ["k1:a", "k1:b"],
      "availability": [["2026-11-26T09:00:00", "2026-11-26T17:00:00"]],
      "available_rounds": ["morning"]
    }
  ],
  "slots": [
    {"id": "k1:a", "key_id": "k1", "seat": "a"},
    {"id": "k1:b", "key_id": "k1", "seat": "b"}
  ],
  "proxy_pairs": [["C", "D"]],
  "management_groups": [["A", "E"]],
  "rounds": [
    {
      "id": "morning",
      "time": "2026-11-26T10:00:00",
      "required_slots": ["k1:a", "k1:b"]
    }
  ]
}
```

### 人员字段

- `id` / `person_id` / `name`：唯一标识。
- `management_line` / `line`：同一非空管理线的人互为独立性冲突。
- `eligible_slots` / `can_hold_slots` / `custody_slots`：可持有槽位；省略或 `"*"` 表示不限制。
- `availability` / `available_windows`：可用区间列表，区间可为数字或 ISO 时间字符串。时间区间按闭区间处理，因此 `17:00` 结束的区间可覆盖 `17:00` 整。
- `available_rounds`：可写轮次 ID；整数按 **从 1 开始的轮次号** 解释。
- `available_round_indices`：显式使用从 0 开始的轮次下标。
- `unavailable_rounds` / `unavailable_round_indices`：禁排轮次。
- `proxies` / `substitutes` / `deputies`：也可写在人员对象上，会与顶层 `proxy_pairs` 一样按无向代理/替班冲突处理。

人员数量超过 12 会抛出 `CeremonyInputError`。

### 槽位与密钥

- `slots` 可为列表或 `{slot_id: {...}}` 映射。
- 槽位字段：`id`、`key_id`/`key`、`seat`、`eligible_people`。
- 也可用顶层 `keys: [{"id": "k1", "seats": ["k1:a", "k1:b"]}]` 显式声明双人保管的两个固定席。
- 若槽位 ID 形如 `key:seat` 且未显式提供 `key_id`，会自动推断。
- 当前防御模型要求每把启用双人保管的密钥恰有两个固定槽/席。未关联密钥的普通槽位仍遵守一人一槽，但不参与双人独立性和密钥启用判断。
- 如果显式提供了 `slots`，轮次不能引用既未在 `slots` 也未在 `keys` 中声明的槽位；完全省略 `slots` 时会根据轮次引用合成裸槽位。

### 冲突关系

冲突图是无向图，边来自：

- 相同的非空 `management_line`；
- `management_groups` / `same_management_line_groups`；
- 顶层 `proxy_pairs` / `substitute_pairs` / `backup_pairs` / `deputy_pairs`；
- 人员对象中的 `proxies` / `substitutes` 等。

冲突只限制同一把密钥的两个保管席及其交接参与者。两个有冲突的人在同一轮分别服务于不同密钥是允许的；测试明确覆盖了这一交错边界。

## 调度与交接语义

### 每轮合法性

一个轮次配置合法，当且仅当：

1. 每个必需槽位都分配了人员；
2. 人员处于该轮可用时段/可用轮次；
3. 人员满足槽位资格；
4. 同一人在该轮最多持有一个槽；
5. 同一把密钥的两个持有者在冲突图中不相邻。

### 换人代价

默认将槽位视为跨轮稳定的固定席。对于相邻两轮都出现的同一个槽位：

- 持有者相同：代价 0；
- 持有者不同：代价 1。

总代价即“换人次数”。一轮新增或移除的槽位不属于替换旧保管人，因此默认不计为换人；输出中仍列出 `added_slots` 和 `removed_slots`。

### 交接条件

对同一把双席密钥，如果两个固定席在相邻轮都换成了不同的人，则该转移必须安排一次交接：

- 新任两个持席人都参与；
- 至少一名上一轮该密钥的旧持席人参与；
- 所选旧持席人与两名新任者，以及新任两人之间，均不得存在管理线/代理冲突；
- 程序会确定性选择第一个可行的旧持席人，并在 `eligible_old_holders` 中列出所有可行旧持席人。

如果两席中只有一席换人，默认不触发正式交接，因为另一席仍保持连续。

默认 `both_seats` 按固定席逐席比较。若使用 `disjoint_crew`，只有新旧两组人完全不重叠时才要求交接；交换两席但原班人马仍在密钥上不会触发。

> 题面没有规定独立的交接时长或交接时刻，因此本实现把交接建模为相邻轮之间的有序事件，并校验其参与者独立性；时钟可用区间用于轮次时间匹配。输出给出参与者，但不虚构交接持续时间。

## 输出结构

可行结果示例字段：

```json
{
  "feasible": true,
  "minimum_changes": 2,
  "total_handovers": 1,
  "rounds": [
    {
      "index": 0,
      "round_id": "r1",
      "assignments": {"k:a": "A", "k:b": "B"},
      "enabled_keys": ["k"]
    }
  ],
  "transitions": [
    {
      "from_round_index": 0,
      "to_round_index": 1,
      "change_count": 2,
      "changed_slots": ["k:a", "k:b"],
      "handovers": [
        {
          "key_id": "k",
          "old_holders": ["A", "B"],
          "new_holders": ["C", "D"],
          "selected_old_holder": "A",
          "eligible_old_holders": ["A", "B"],
          "participants": ["A", "C", "D"]
        }
      ]
    }
  ],
  "handovers": [],
  "first_unreachable_round": null,
  "local_infeasible_round": null
}
```

`handovers` 是从各 `transitions[*].handovers` 展平的便捷列表，并附带前后轮 ID/下标。

不可行时有两种定位：

- `local_infeasible_round`：该轮本身没有任何合法配置；
- `first_unreachable_round`：截至该轮之前均有可达配置，该轮也有本地合法配置，但没有任何合法连续交接转移可以抵达它。

## 算法与复杂度

每轮使用人员位掩码和最小剩余值（MRV）回溯枚举合法配置，并用预计算的 `independent[mask]` 表在 \(O(1)\) 时间判断一个参与者集合是否两两独立。

设第 \(r\) 轮合法配置数为 \(S_r\)，槽位数为 \(m\)。前向 DP 为：

\[
dp[r][j] = \min_i(dp[r-1][i] + \text{transition\_cost}(i,j))
\]

只接受通过交接检查的转移；保存父指针以重建序列。对相同槽位顺序使用配置元组直接索引，并利用全局最小 DP 值和非负代价剪枝。最坏时间复杂度为 \(O(\sum S_{r-1}S_r m)\)，空间复杂度为 \(O(\sum S_r)\)。人员数不超过 12，位掩码只有 4096 个状态；测试包含 12 人/4 槽边界和 6 人以内的穷举最优性对照。

## 验证范围

`tests/` 中的本地合成测试覆盖：

- 零换人、单席换人、双席换人；
- 交接参与者独立性及“第二个旧席才可行”的选择；
- 管理线冲突、代理/替班冲突、不同密钥之间允许冲突人员交错；
- 一人一轮不能持两槽；
- 资格过滤、数字时间窗、ISO 时间和闭区间端点；
- 缺少一个席位列时密钥不启用；
- 两轮密钥的并行/跨密钥人员交错交接；
- 单轮本地不可配与“单轮可配但交接不可达”的区别；
- 第一个不可达轮次发生在第 2 个转移之后的情况；
- 超过 12 人、重复 ID、未知槽位等输入错误；
- 恰好 12 人的性能/边界场景；
- CLI 文件、stdin、退出码和根目录 `solution.py` 包装器；
- 一个小规模三阶段用例与独立的 `itertools.product` 全序列穷举结果比较，最小值一致；
- 每个可行结果都由共享断言器重新检查可用性、资格、一人一槽、密钥独立性、交接独立性、换人计数和输出一致性。

最近一次实际复核使用 Python 3.11.6 执行题目指定命令，结果为 `Ran 26 tests ... OK`；其中包含 80 个固定随机种子的小规模 DP/全穷举一致性用例。上述结论来自这些实际执行和断言，而不是仅靠静态阅读。
