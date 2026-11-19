# 握手 UTC 授时判定器

终端恢复供电后本地时钟不可信。本模块把每份**已认证授时回执**视为一个
UTC 闭区间 `[start, end]` 加一个时钟来源（source），据此判定握手是否可以
放行。全部计算只使用本地合成夹具，不探测任何真实时间服务。

## 规则

1. **同源同一时刻最多一票。** 同一 `source` 的多份回执（无论区间是否重叠）
   先取并集，再参与计数；因此重复副本无法凑数。
2. **保留获得足够不同来源支持的时刻。** 某时刻被至少 `min_sources` 个
   不同来源覆盖时进入保留集。结果可能是不连续的若干段，段与段之间的
   空白不会被补成证据。
3. **闭区间语义。** 回执端点、相邻段共享端点、证书有效期端点都算在内；
   相邻的两段在共享端点处合并为一段连续区间。
4. **有效性只看保留集。** 调用方给出 `valid_from` / `valid_to`：
   - 所有保留时刻都落在有效期内 → `RELEASE`（放行）；
   - 所有保留时刻都落在有效期外 → `REJECT`（明确拒绝）；
   - 保留时刻同时出现有效与无效，或根本没有保留时刻（证据不足）→ `HOLD`（暂缓）。
5. **空白段不参与有效性计算。** 两个可信段之间的间隔既不是有效证据，
   也不是无效证据；若有效期整体落在该空白内，结论是 `REJECT`（因为没有
   任何保留时刻有效），而不是按包络区间误判为混合。
6. 未认证回执（`authenticated=False`）不计票，只在结果中报告忽略数量。

## 入口

- 库入口：`handshake_time.adjudicate(receipts, min_sources=..., valid_from=..., valid_to=...)`，
  返回 `AdjudicationResult`（`verdict` / `retained` / `valid_segments` /
  `fully_valid` / `fully_invalid` / `reason`）。
- 只用证据的函数：`handshake_time.retained_segments(receipts, min_sources=...)`。
- 命令行入口：`python -m handshake_time`，从 stdin 读 JSON，向 stdout 写 JSON。

```sh
printf '%s' '{"min_sources":2,
  "valid_from":"2026-11-20T00:00:00Z","valid_to":"2026-11-20T01:00:00Z",
  "receipts":[
    {"start":"2026-11-20T00:10:00Z","end":"2026-11-20T00:20:00Z","source":"gps"},
    {"start":"2026-11-20T00:15:00Z","end":"2026-11-20T00:25:00Z","source":"ntp"}]}' \
  | .venv/bin/python -m handshake_time
```

输出字段：`verdict`（`release`/`reject`/`hold`）、`retained`（UTC 闭区间列表）、
`valid`（保留集与有效期之交）、`fully_valid`、`fully_invalid`、
`ignored_unauthenticated`、`reason`、`exit_code`。
进程退出码：`0` 放行、`2` 拒绝、`3` 暂缓、`1` 输入错误。

时间一律要求带时区（UTC 或等价偏移），裸 datetime、反向区间、空来源名
都会在构造 `TimeReceipt` / `UtcInterval` 时被拒绝。

## 运行测试

```sh
.venv/bin/python -m unittest discover -s tests -t . -v
```

当前状态：**40 个用例全部通过**（Python 3.11.6）。

## 验证范围

- `tests/test_retained_segments.py`：同源去重、跨源交集、多段分离、
  共享端点合并、孤立端点、未认证回执、阈值与输入校验。
- `tests/test_adjudicate.py`：
  - 放行：全部保留时刻落在有效期内；多段均有效；端点相接。
  - 拒绝：保留时刻全部越界；只剩端点之外的极小偏移。
  - 暂缓：证据不足（单一来源、同源多副本）、有效与无效混合、
    保留段被有效期单侧截断。
  - **交错场景**：有效期落在两个可信段之间的空白 → 拒绝（空白不进入计算）；
    同一有效期覆盖整段 → 放行；阶梯式三来源在阈值 2 下只保留中间段，
    再被有效期截断 → 暂缓。
  - 命令行入口的放行/拒绝/暂缓/坏输入四类退出路径。

测试使用 `tests/fixtures.py` 生成固定 UTC 夹具（起点
2026-11-20T00:00:00Z），不含随机性与外部依赖。
