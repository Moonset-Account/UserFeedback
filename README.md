# 本地证书撤销状态库（revocation_store）

一个纯本地的 Python 3.11 状态库，用来接收证书校验器定期下发的**已验证撤销全量表**和
**增量表**，按查询时刻回答某证书是否被撤销、被临时挂起，或在证据不足时返回未知。

不联网、不解析真实 CRL/OCSP、不做目标探测；所有判断只基于本地已入库的表。

## 实际入口

| 用途 | 入口 |
| --- | --- |
| 库 API | `from revocation_store import RevocationStore, Table, TableEntry, Reason` |
| 模块 CLI | `.venv/bin/python -m revocation_store --db <path> <command>` |
| 验收测试 | `.venv/bin/python -m unittest discover -s tests -t tests -v`（在仓库根目录执行） |

已用解释器：`.venv/bin/python`，Python 3.11.6。仅使用标准库（`sqlite3`、`unittest`、
`argparse`、`json`），无需安装任何依赖。

### 库 API 示例

```python
from datetime import datetime, timedelta, timezone
from revocation_store import RevocationStore, Table, TableEntry, Reason

t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
H = lambda h: t0 + timedelta(hours=h)

with RevocationStore("state/revocations.sqlite3") as store:
    store.ingest(Table.full(
        issuer="Example CA", scope="root", number=1,
        this_update=H(0), next_update=H(48),
        entries=[TableEntry("A1", Reason.KEY_COMPROMISE, H(1))],
    ))
    store.ingest(Table.delta(
        issuer="Example CA", scope="root", base_number=1, number=2,
        this_update=H(4), next_update=H(48),
        entries=[TableEntry("A2", Reason.CERTIFICATE_HOLD, H(4))],
    ))

    store.lookup("Example CA", "root", "A1", H(6)).status  # Status.REVOKED
    store.lookup("Example CA", "root", "A2", H(6)).status  # Status.SUSPENDED
    store.lookup("Example CA", "root", "A9", H(6)).status  # Status.NOT_LISTED
    store.lookup("Example CA", "root", "A9", H(99)).status # Status.UNKNOWN（无有效基线）
```

所有时间必须是带时区的 `datetime`；naive 时间会被拒绝，避免查询时刻按本地时区漂移。

### CLI 示例

```bash
# 入库：JSON 文件可以是单个表对象，也可以是表数组
.venv/bin/python -m revocation_store --db state.sqlite3 ingest --file tables.json

# 查询
.venv/bin/python -m revocation_store --db state.sqlite3 \
    lookup --issuer "Example CA" --scope root --serial A1 --at 2026-01-01T06:00:00Z

# 盘点
.venv/bin/python -m revocation_store --db state.sqlite3 tables --issuer "Example CA"
.venv/bin/python -m revocation_store --db state.sqlite3 entries --table-id 1
```

CLI 输出 JSON 到 stdout。表结构非法时退出码 `1` 且输出 `{"error": "invalid_table"}`；
同一表身份被换了内容重发时退出码 `1` 且输出 `{"error": "conflict"}`；两者都**不写入**任何数据。

`DELTA` 表必须给出 `base_number`；`FULL` 表是自身基线，`base_number` 被忽略。

## 数据模型

每张表都记录：`issuer`（签发者）、`scope`（分发范围）、`base_number`（基线编号）、
`number`（本表编号）、`this_update` / `next_update`（生效 / 失效时刻）、以及条目
`(serial, reason, revocation_time)`。

- **FULL** 表是完整基线快照，`base_number == number`。
- **DELTA** 表只作用于 `base_number` 指明的完整基线，且是**相对该基线的完整变化集**
  ——所以查询只取“最新有效的那一张增量”，绝不把多张增量叠加起来。

## 已实现的语义

1. **按查询时刻选证据**：在同 `issuer` + `scope` 中，取 `this_update <= at < next_update`
   的编号最大基线，再取该基线名下编号最大且同样在有效期内的增量。入库先后顺序不影响结果。
2. **增量只作用于指明的基线**：基线未入库的增量被保留但视为不可解释（inert），不会生效。
3. **临时挂起可恢复**：`certificateHold` 报 `suspended`；`removeFromCRL` 在更新有效的增量里
   出现时恢复为 `not_listed`。`removeFromCRL` 只能解除挂起，**不能**解除永久撤销，也无法在
   FULL 表中出现（入库即拒绝）。
4. **永久撤销在同一证书生命周期内保持**：除 `certificateHold` / `removeFromCRL` 外的原因
   一旦在可解释的表中生效，就成为证书生命周期（`issuer` + `serial`）内的单调事实——更晚的
   基线、增量省略该证书、增量过期、表整体失效、迟到旧表都**不能**回退它。
5. **迟到旧表只补历史**：晚到的低编号表入库时标记 `backfilled=True`；它可为更早的查询时刻
   提供依据，但当前时刻仍由最新有效表决定，不能回退新状态。
6. **“未列入”需要完整有效证据**：只有确实选中了同签发者、同范围、当时有效的完整基线时
   才返回 `not_listed`（`definitive_non_revocation = true`）；否则返回 `unknown`。

### 需要留意的取舍

“永久撤销在生命周期内保持”与“新基线是新的权威快照”在题面里是两条独立约束，本实现把
**永久撤销当作单调覆盖层**：即便更新的 FULL 基线省略了某永久撤销证书，查询仍返回
`revoked`，因为题面明确要求永久撤销“在同一证书生命周期内保持”。临时状态（挂起）则完全
跟随所选基线 + 最新增量，新基线省略即视为挂起结束。若上游确实需要在同一 serial 上开启
全新生命周期，应改用新的 serial，而不是靠新基线覆盖。

## 验证范围

`tests/` 下四个文件、共 53 个 `unittest` 用例，全部为真实断言（无跳过、无期望为空的占位）：

- `tests/test_ingest.py` — 表结构校验的接受与拒绝、幂等入库、同身份异内容冲突、
  迟到表标记、条目往返。
- `tests/test_revocation_rules.py` — 基线选择、增量只作用指定基线且不叠加、增量生效时刻、
  增量过期回退、挂起与 `removeFromCRL` 恢复、永久撤销在增量省略/增量过期/新基线省略/表失效
  四种情况下均保持、迟到表只补历史、到达顺序无关、跨范围/跨签发者/无有效基线一律 `unknown`。
- `tests/test_jsonio.py` — JSON 往返保真、`DELTA` 必须给出 `base_number`、原因缺省值、
  随附示例夹具 `examples/tables.json` 可被解析。
- `tests/test_persistence_cli.py` — SQLite 重开后状态与冲突检测仍在；CLI 入库→查询往返、
  无证据返回 `unknown`、非法表与冲突表被拒且不落盘、`tables` / `entries` 盘点。

每个交叉约束都同时覆盖了“可接受结果”“应被阻止/拒绝的结果”和两者之间的边界（例如
`this_update` 与 `next_update` 半开区间的端点、增量恰好生效/恰好过期的时刻、迟到表在
新旧两个查询时刻的相反结论）。

运行：

```bash
cd /Users/xingyaolei/Desktop/feedback-studio/runs/feishu-nightly-20261120/UID-13982/round-1/attempt-2/workspace
.venv/bin/python -m unittest discover -s tests -t tests -v
```

当前结果：`Ran 53 tests ... OK`（Python 3.11.6）。

## 示例夹具

`examples/tables.json` 是一组最小合成数据（1 张 FULL + 1 张 DELTA），用于冒烟演示，也被
`tests/test_jsonio.py` 解析校验：

```bash
.venv/bin/python -m revocation_store --db /tmp/rv.sqlite3 ingest --file examples/tables.json
.venv/bin/python -m revocation_store --db /tmp/rv.sqlite3 \
    lookup --issuer "Example CA" --scope root --serial A1 --at 2026-01-01T06:00:00Z
```

## 未做 / 边界

- 不校验表上的密码学签名——题面假设表已“验证”后才进入本库。
- 不实现网络拉取、调度或与真实 CRL/OCSP 的对接。
- 没有删除或压缩历史表的 API；表按追加方式保存，便于审计与历史回放。
