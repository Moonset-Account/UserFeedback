# 多播组成员管理模块（IGMPv3/MLDv2 风格的源过滤聚合）

同一接口上的多个接收者可以订阅同一个组播组，并各自携带完整的源过滤条件：

- **INCLUDE 模式**：只接收显式列出的源；
- **EXCLUDE 模式**：接收除所列源之外的全部源。

本模块把每个接收者的条件按 `(接口, 组)` 聚合成**一条**发往上游的订阅
（结果同样表达为 INCLUDE/EXCLUDE 模式），仅当模式或源集合**确实变化**时
才产生订阅更新；数据到达时再依据各接收者自己的条件，选出本地真正想接收
该流量的接收者。

## 目录结构

```
workspace/
├── multicast_membership/
│   └── __init__.py        # 全部实现，仅依赖 Python 标准库
├── tests/
│   ├── __init__.py
│   └── test_membership.py # 39 个 unittest 用例
└── README.md
```

## 公开 API

### 数据类型

| 名称 | 说明 |
| --- | --- |
| `FilterMode` | 枚举：`INCLUDE` / `EXCLUDE` |
| `Filter(mode, sources=frozenset(), expires_at=None, source_expiry={})` | 不可变的完整过滤条件；`wants(source)` 判定是否想收某源 |
| `SubscriptionUpdate(interface, group, mode, sources, withdrawn=False)` | 一条上游订阅更新；`withdrawn=True` 表示撤销（该接口/组已无接收者），此时 `mode/sources` 为撤销前最后状态 |
| `MembershipManager` | 成员关系管理器 |

### `MembershipManager` 方法

| 方法 | 作用 |
| --- | --- |
| `submit_filter(receiver, interface, group, mode, sources=None, expires_at=None, source_expiry=None)` | **整体替换**某接收者在某 `(接口,组)` 上的完整过滤条件（非增量补丁）；返回产生的 `SubscriptionUpdate`，无净变化返回 `None`。`sources` 省略时取 `source_expiry` 的键 |
| `leave(receiver, interface, group)` | 离开事件：删除该接收者的整条记录；接收者不存在或聚合未变时返回 `None` |
| `advance(now)` | 逻辑时钟推进到 `now`：**先统一移除**所有 `到期时刻 <= now` 的项，**再**为每个受影响的 `(接口,组)` 发布至多一条净变化；返回本次更新列表（可能为空） |
| `upstream_filter(interface, group)` | 当前已发布给上游的聚合 `Filter`；无接收者返回 `None` |
| `upstream_wants(interface, group, source)` | 上游订阅是否想收某源 |
| `receivers_for(interface, group, source)` | 数据到达时，本地真正想收该源的接收者元组（按 `str` 排序，无人想收为 `()`） |
| `record_filter(receiver, interface, group)` | 查询某接收者当前的完整条件（含定时器），不存在返回 `None` |
| `updates`（属性） / `pop_updates()` | 订阅更新日志的只读元组 / 取出并清空日志 |

时间参数使用抽象逻辑时钟（测试中为 int/float 虚拟时钟），只要求可比较，
模块不读取真实系统时间。接收者、接口、组、源均为任意可哈希对象。

## 聚合规则（与 IGMPv3/MLDv2 组记录合并等价）

对同一 `(接口, 组)`：

1. 没有任何接收者 → 无订阅（状态为 `None`，最后一人离开时发布撤销更新）；
2. 全部为 INCLUDE → `INCLUDE(各源集合之并)`；
3. 至少一个 EXCLUDE →
   `EXCLUDE(所有 EXCLUDE 源集合之交 − 所有 INCLUDE 源集合之并)`。

**题面示例**：r1 排除 `{a,b}`、r2 排除 `{b,c}`、r3 明确包含 `{b}`：

- EXCLUDE 交集为 `{b}`，减去 INCLUDE 并集 `{b}` → `EXCLUDE(∅)`，
  即**上游接收所有源**；
- 但本地投递按各接收者自己的条件独立判定：源 `b` 只派给 r3
  （r1、r2 都排除 b），源 `a` 只给 r2，源 `c` 只给 r1，未列出的源
  给 r1、r2。

## 到期语义（两阶段）

- **记录级定时器** `expires_at`：到期删除整条记录；
- **逐源定时器** `source_expiry={源: 时刻}`：到期只移除该源，记录保留
  （INCLUDE 可能因此变为空集合，空 INCLUDE 记录仍保留并照常发布）；
- `advance(now)` 先在一次扫描中完成全部移除，再重新聚合发布：
  同一时刻多个接收者/源到期时，每个 `(接口,组)` 对外**至多一条**净变化，
  不发布任何中间状态；净聚合结果未变则不产生更新。

## 复现步骤

要求 Python 3.11.6，无第三方依赖。在工作区根目录执行：

```bash
# 隔离模式（-I）下从根目录发现全部测试
python3 -I -m unittest discover -s tests -t . -v

# 非隔离模式亦可（根目录会自动进入 sys.path）
python3 -m unittest discover -s tests -t .
```

## 验证范围

`tests/test_membership.py` 共 39 个用例，均为真实执行的功能测试，
未使用任何预置输出或 mock 时钟，按场景分为：

- **正常场景**：题面三接收者聚合（上游全收、b 只派第三人）、纯 INCLUDE
  取并集、INCLUDE↔EXCLUDE 模式切换、混合模式下 INCLUDE 源从排除集中剔除、
  本地投递判定；
- **边界场景**：空 INCLUDE 的保留与发布、最后一人离开触发撤销、撤销后再
  加入、相同状态重复提交无更新（含仅定时器变化时聚合不变即无订阅更新）、
  聚合不变的离开无更新、离开不存在的接收者幂等、`source_expiry` 键校验、
  非法 mode 校验、不可哈希标识自然报错；
- **交错场景**：记录级/逐源两种到期、`expires_at == now` 边界、同一时刻
  多记录到期只发一条净变化、多个组同时到期各发一条、逐源到期使 INCLUDE
  收缩为空集但记录保留、EXCLUDE 源到期后接收面扩大、逐源定时器与记录级
  定时器交错触发、重复 `advance` 无副作用、多接口/多组状态隔离、
  更新日志累积与 `pop_updates` 清空。

最近一次本地执行结果：`Ran 39 tests ... OK`（Python 3.11.6，
`python3 -I -m unittest discover -s tests -t .`）。
