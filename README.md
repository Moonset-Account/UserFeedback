# 封条交接核对匹配模块

证物库有若干卷预编号封条，各卷独立编号、号段可能重复。本模块接收领出的
卷（含开卷方向、作废号段）与同一人员的封箱先后记录（含封箱照片抄录的局部
号码、未拍照的已登记封箱），枚举**全部合法分配**，返回每个箱在所有合法分配
中可能的（卷号, 位置）；只有唯一可能时才作确定归属。

样本上限：**4 卷、每卷 8 枚**（超出即抛 `ValueError`）。仅使用 Python 3.11
标准库，无第三方依赖。

## 合法分配的判定规则

1. 每个箱消耗恰好一枚**非作废**封条；单枚封条只能归属一个箱。
2. 每卷只能按开卷方向依次使用。某卷已被 `k` 个箱占用时，下一个使用该卷的
   箱只能拿该卷第 `k+1` 枚非作废封条：
   - 中间跳过的**非作废**封条必须已被更早的箱（含未拍照箱）消耗；
   - **作废**封条由作废号段解释，自动跳过，不占箱但仍占使用位置编号；
   - 卷尾未使用的封条不需要解释。
3. 拍照箱的抄录号码与封条号码做**整串通配匹配**，不做相似度挑选：
   - `?` 匹配单个数字，`*` 匹配任意长度（含空）数字串，其余字符按字面匹配；
   - 未拍照箱（`partial_number=None`）可消耗任意非作废封条。
4. 箱列表顺序即封箱先后顺序；跨卷可任意交错，但同卷分配必须遵循该次序。

## 整体状态

| `MatchResult.status` | 含义 |
| --- | --- |
| `ok` | 存在覆盖全部箱的合法分配；`allocation_count` 为分配总数，逐箱 `possibilities` 只含能出现在某套完整分配中的归属 |
| `conflict` | 每个箱单独看都有候选，但无法同时满足顺序/唯一/容量约束；逐箱仍如实列出局部候选，不挑选“局部最像”的方案 |
| `no_candidate` | 至少一个箱在任何非作废封条上都不匹配；无候选箱的 `possibilities` 为空 |

## 公开 API

```python
from seal_matching import (
    Roll, BoxRecord, SealRef, BoxResult, MatchResult, match_seals,
    DIRECTION_FORWARD, DIRECTION_REVERSE,
    STATUS_OK, STATUS_CONFLICT, STATUS_NO_CANDIDATE,
)
```

### `Roll(roll_id, seals, direction=DIRECTION_FORWARD, void_ranges=())`

- `seals`：卷首到卷尾的物理顺序号码（字符串数字），卷内不得重复，各卷可重复。
- `direction`：`"forward"`（默认）或 `"reverse"`。
- `void_ranges`：作废闭区间 `(起号, 止号)` 列表，端点必须是本卷存在的号码；
  数字号段按数值比较（如 `("002", "003")`）。

### `BoxRecord(box_id, partial_number=None)`

- 列表顺序即封箱先后；`partial_number=None` 表示未拍照但已登记的封箱。
- 支持 `?` / `*` 通配；非法字符或空串抛 `ValueError`。

### `SealRef`

- `roll_id`：卷号；
- `usage_position`：1 起的**使用位置**（开卷顺序第几枚，作废封条同样占位）；
- `physical_position`：1 起的**物理位置**（`Roll.seals` 中的序号，与方向无关）；
- `seal_number`：封条印刷号码。

### `match_seals(rolls, boxes) -> MatchResult`

- `MatchResult.status` / `allocation_count` / `boxes`；
- 每个 `BoxResult` 含 `possibilities: tuple[SealRef, ...]` 与
  `determined: bool`（候选恰为 1 个）；
- `MatchResult.determined_all`：`ok` 且全部箱唯一确定时为真。

### 示例

```python
rolls = [
    Roll("卷A", ("2001", "2002", "2003", "2004"),
         void_ranges=[("2003", "2003")]),
    Roll("卷B", ("2001", "2002"), direction=DIRECTION_REVERSE),
]
boxes = [
    BoxRecord("箱1", "2001"),
    BoxRecord("箱2", None),     # 未拍照登记，消耗 2002
    BoxRecord("箱3", "2004"),   # 2003 已作废，可直接跳过
]
result = match_seals(rolls, boxes)
result.status             # "ok"
result.allocation_count   # 1
```

## 算法

- 预计算每卷的使用顺序、作废位置与非作废封条序列；
- 先按“号码整串匹配且未作废”生成每箱局部候选；
- 以 `(箱序号, 各卷已占用非作废封条数)` 为状态做记忆化回溯，
  `ways(b, used)` 计算剩余箱的合法分配数；顺序约束体现为
  “下一枚候选槽位必须恰为 `used[r]`”，唯一性由槽位占用天然保证；
- 依据记忆表重建每箱在所有完整合法解中出现过的归属并去重。
- 状态空间上界约 `(箱数+1) × (每卷容量+1)^卷数`，在 4×8 规模内很小。

## 运行方法

```bash
# 常规测试（Python 3.11.6 实测）
python3 -m unittest discover -s tests -v

# 隔离模式（模拟工作区存在不可信同名模块，不读取用户 site 包）
python3 -I run_tests_isolated.py

# 独立暴力枚举的随机交叉验证（默认 400 组，可调种子/组数）
python3 scratch/crosscheck.py --seed 77 --trials 500
```

## 目录结构

```
seal_matching/
  __init__.py   # 公开 API 导出
  model.py      # 数据类型与状态常量
  core.py       # 校验、预计算与记忆化回溯
tests/
  test_seal_matching.py   # 25 个 unittest 用例
scratch/
  crosscheck.py           # 独立暴力参照 + 随机合成数据交叉验证（非交付测试）
run_tests_isolated.py     # python -I 下的测试启动器
```

## 已验证范围（基于实际执行与断言）

`python3 -m unittest discover -s tests -v` 实测 25/25 通过，覆盖：

- 正向/反向开卷的使用位置与物理位置；
- 作废号段（含前导零按数值比较）允许跳过、作废封条仍占使用位置；
- 未拍照的已登记封箱解释跳号，且未拍照箱同样受前缀顺序约束；
- 跨卷重复号段产生的多解歧义、逐箱候选集合与合法分配计数（1/2/4 等）；
- 多卷交错使用遵循封箱先后；顺序矛盾、无法解释的跳号、单枚封条复用过箱、
  容量不足均报 `conflict`，且不丢弃逐箱局部候选；
- 号码完全不匹配报 `no_candidate`，且与 `conflict` 同时存在时优先
  `no_candidate`；
- `?` / `*` 通配整串匹配语义，无隐式前缀/相似度匹配；
- 空箱序列、卷尾剩余封条、结果顺序与输入一致、值对象语义；
- 输入校验：卷数/每卷枚数上限、卷号与箱号唯一、卷内号码唯一且为数字、
  方向合法、作废端点存在、通配字符合法等。

`scratch/crosscheck.py` 用独立暴力枚举（枚举全部指派组合并逐条检查约束）
对 1200 组随机合成场景（种子 20261128/77/909，规模 1–4 卷、1–8 枚、
0–8 箱，含随机作废段、反向开卷、重复号段、通配与未拍照箱）比对状态、
合法分配总数与逐箱候选集合，全部一致；另在 `python3 -I` 隔离模式下
复测 25/25 通过。

## 未覆盖 / 边界声明

- 未实现持久化、并发与大规模输入（题面限定 4 卷×8 枚，超限直接拒绝）；
- 号码限定为数字字符串，非数字通配模式会被拒绝；
- 交叉验证脚本属于开发期独立参照，不计入交付测试集。
