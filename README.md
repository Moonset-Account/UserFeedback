# 匿名通话中继号分配与回拨解析

匿名通话服务用一小池中继号撮合联系人。来电只有主叫号码和拨入的中继号，
本模块负责：

1. **号码分配**：对一批至多 14 条含起止时间的预约，求使用中继号最少的
   分配；已对客户公布的预约固定原号。
2. **回拨解析**：根据主叫号码 + 拨入中继号 + 呼叫时刻，唯一确定通话
   另一端；冷却期内的旧关系回拨返回“已结束”；陌生主叫得不到任何身份
   信息。

仅使用 Python 3.11 标准库，无第三方依赖。

## 形式化规则

预约 `Reservation(caller, callee, start, end, cooldown, pinned_relay)`
的占用区间为半开区间 `[start, end + cooldown)`（有效期 + 回拨冷却期）。

两条预约 **冲突**（不可共用同一中继号）当且仅当：

1. 占用区间相交（半开：端点相接不算重叠）；并且
2. 至少共享一端号码（主叫或被叫相同）。

由此构造冲突图，最少中继号数 = 冲突图色数。`n <= 14`，使用
DSATUR 风格的精确回溯求最小着色，以最大团（Bron–Kerbosch）作为下界。

回拨解析（半开区间）：

| 条件 | 结果 | 是否返回对端 |
| --- | --- | --- |
| 主叫与该中继号上任何预约都无关 | `DENIED`（陌生主叫） | 否 |
| 呼叫时刻在某条预约有效期 `[start, end)` 内且唯一 | `CONNECTED` | 是，唯一对端 |
| 同一时刻存在多条可接通预约 | 抛 `AmbiguousRoutingError`（合法分配下不会出现） | — |
| 有效期已过但仍在冷却期 `[end, end+cooldown)` 内 | `ENDED`（旧关系结束，禁止转到新联系人） | 否 |
| 预约尚未开始，或冷却期也已过 | `DENIED`（不泄露身份） | 否 |

接通判定优先于冷却判定。

## 公开 API

```python
from anon_relay import (
    # 数据模型
    Reservation,   # 不可变预约 dataclass
    Allocation,    # 分配结果：mapping / relays / relay_of() / reservations_on()
    CallResult,    # 回拨结果：status / relay / caller / other_party / reservation_id
    CallStatus,    # CONNECTED / ENDED / DENIED

    # 分配
    RelayAllocator,          # RelayAllocator(reservations, relay_pool=None).allocate()
    conflicts,               # conflicts(a, b) -> bool

    # 回拨
    resolve_callback,        # resolve_callback(caller, relay, at, reservations, allocation)
    AmbiguousRoutingError,

    # 异常
    AllocationError,
    InvalidReservationError,   # 预约非法 / 超过 14 条 / 固定号不在池中
    ConflictingPinError,       # 两个冲突的已公布预约被固定到同一中继号
    InsufficientRelaysError,   # 号码池太小
)
from anon_relay.allocator import MAX_RESERVATIONS  # 预约数量上限 14
from anon_relay.demo import build_demo, run_demo
```

关键参数说明：

- `Reservation.pinned_relay`：已公布预约的固定中继号；`None` 表示自由排号。
- `RelayAllocator(relay_pool=...)`：给定可用号码池时只能使用池内号码，
  颜色按池顺序映射；不给定时自动生成 `R01, R02, ...`。
- 时间是任意可比较标量（如 Unix 时间戳、分钟序号），所有区间均为半开。

## 运行方法

```bash
# 测试（题目指定命令）
python3 -m unittest discover -s tests -v

# 题面演示：两个预约各有一端相同的排号与回拨结果
python3 -m anon_relay.demo

# 隔离模式边界复核
python3 -I scripts/edge_checks.py
```

## 演示场景（`anon_relay/demo.py`）

```
R1: A(100) <-> B(200)，有效期 [0, 60)，  冷却 30，占用 [0, 90)
R2: A(100) <-> C(300)，有效期 [60, 120)，冷却 30，占用 [60, 150)
```

占用区间在 `[60, 90)` 重叠且共享 A → 冲突，最少 2 个中继号：
`R1 -> R01`，`R2 -> R02`。回拨断言覆盖：

- 有效期内双方互拨，唯一接通对端；
- 陌生主叫（999）任何时刻拨入均 `DENIED`，结果不含任何身份字段；
- `t=60` 半开边界：R1 刚结束进入冷却（`ENDED`），R2 刚好生效可接通 C；
- 冷却期内 A/B 回拨 R01 只得 `ENDED`，不会被转接到新联系人 C；
- 冷却期结束后再拨为 `DENIED`。

## 验证范围

全部为本地合成数据，无网络访问、无凭据读取、无第三方依赖安装。

- `tests/test_allocator.py`（24 个）
  - 冲突规则（6 个）：区间不相交 / 无共享号码不冲突；重叠 + 共享号码
    冲突；冷却期延伸造成的冲突；被叫共享与主叫共享对称；半开端点相接。
  - 最小性（7 个）：3 团需 3 号、C5 奇环需 3 色、独立集 1 号、空批、
    14 条稠密批次与独立暴力参考色数逐一对照等。
  - 固定号（7 个）：公布号保留、冲突固定号拒绝、固定号强制计数、
    池外号拒绝、池容量不足拒绝、池标签顺序、分配缓存与确定性。
  - 输入校验（6 个）：超 14 条、重复 ID、空号码、主被叫相同、
    非法区间（终点 <= 起点）、负冷却。
- `tests/test_callback.py`（9 个）
  - 唯一接通及反向拨打、陌生主叫拒绝无身份泄露、冷却期 `ENDED` 且
    隐藏新联系人、半开边界（起点含/终点不含/冷却终点）、未开始拒绝、
    陌生主叫全时段拒绝、共号预约的正确路由、歧义路由抛错、结果字段。
- `tests/test_demo_integration.py`（7 个）
  - 演示分配与 10 条回拨场景的期望值（3 个）；25 组随机合成批次的
    冲突约束不变量；10 组随机批次与独立参考色数的最小性对照；
    时间轴扫描下的回拨安全不变量（陌生主叫恒拒绝、接通对端必属同一
    预约、ENDED 必在冷却窗内）；固定号端到端。
- `scripts/edge_checks.py`：无冷却窗口、端点相接共号、14 团/14 条上限、
  第 15 条被拒绝，支持 `python3 -I` 隔离运行。

实测结果：`python3 -m unittest discover -s tests` → **Ran 40 tests, OK**；
`python3 -I scripts/edge_checks.py` → `edge checks OK`。

## 目录结构

```
anon_relay/
  __init__.py    # 公开 API 导出（demo 子模块按需导入）
  models.py      # Reservation / Allocation / CallResult / CallStatus
  allocator.py   # 冲突判定、固定号校验、最小着色分配
  callback.py    # 回拨解析
  demo.py        # 题面双预约演示
tests/
  test_allocator.py
  test_callback.py
  test_demo_integration.py
scripts/
  edge_checks.py
```
