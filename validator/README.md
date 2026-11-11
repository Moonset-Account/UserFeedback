# Handle-Authorization Regression Verifier

回归验证文件服务补丁后的核心安全结论：**被校验的对象，就是提交写入的对象**，
即使日志来自多个线程、事件到达时存在乱序。

验证器只依据两类顺序证据重建 happens-before 关系，绝不读取事件在输入流中的
到达位置：

1. **线程内顺序**：同一 `thread` 上按 `seq` 排序；
2. **显式同步边**：
   - `SYNC` 的 `signal`/`wait` 同步点（wait 排在对应 signal 之后）；
   - 事件的 `depends_on` 直接前驱列表。

无顺序证据时结论保持 **UNDECIDABLE（无法判定）**，不做任何猜测。

## 三值结论

| 结论 | 含义 | 退出码 |
| --- | --- | --- |
| `PASS` | 每次提交都能证明：同任务、同句柄上存在先于提交且仍然有效的授权，且对象号+世代一致 | 0 |
| `VIOLATION` | 图与内存模型证明约束被破坏（如提交旧世代、取消后未再校验、跨任务借用句柄） | 1 |
| `UNDECIDABLE` | 缺少同步记录或存在互不排序的竞争事件，证据不足以得出确定结论 | 2 |
| 输入/schema 错误 | 记录格式非法、event_id 重复等 | 3 |

当同一报告中同时存在已证实的 VIOLATION 与无关的 UNDECIDABLE 记录时，
整体判定为 VIOLATION——已证实的违规不被证据缺口降级。

## 事件 Schema

```json
{
  "event_id": "e7",
  "type": "WRITE_COMMIT",
  "thread": "T2",
  "seq": 5,
  "task": "task-A",
  "handle": "H1",
  "object_no": "O1",
  "object_gen": 2,
  "sync_id": "point-1",
  "sync_op": "signal | wait",
  "depends_on": ["e3"]
}
```

事件类型：

- `HANDLE_CREATE`：句柄生命周期开始（携带 handle）；
- `BIND`：把句柄稳定绑定到 `object_no@object_gen`；
- `VALIDATE_OK`：某任务对该句柄当前对象身份校验通过；
- `WRITE_COMMIT`：提交写入；
- `CANCEL`：取消句柄上的授权；
- `RETRY`：重试，语义上必须跟随 CANCEL 且之后需要重新校验；
- `SYNC`：同步点，`sync_op` 为 `signal` 或 `wait`。

## 规则一览（findings 中的 rule_id）

**结构类**

- `hb_cycle` (VIOLATION)：顺序边成环，不存在一致执行序；
- `seq_conflict` (UNDECIDABLE)：同线程出现重复 seq；
- `missing_sync_record` (UNDECIDABLE)：`depends_on`/wait 指向日志中不存在的事件；
- `unknown_handle` (VIOLATION)：引用了从未 HANDLE_CREATE 的句柄。

**绑定与校验**

- `bind_before_create` / `validate_before_bind` (VIOLATION)：生命周期倒置；
- `bind_create_unordered` / `validate_bind_unordered` (UNDECIDABLE)：无顺序边；
- `validate_identity_mismatch` (VIOLATION)：校验身份与所有先前绑定不符；
- `validate_stale_generation` (VIOLATION)：句柄已重绑到新身份后仍校验旧世代。

**提交（核心）**

- `commit_without_validation` (VIOLATION)：任务从未在该句柄上校验；
- `commit_validation_unordered` (UNDECIDABLE)：有匹配校验但无法证明先于提交；
- `commit_validation_not_before` (VIOLATION)：所有匹配校验都在提交之后；
- `commit_wrong_generation` (VIOLATION)：任务只校验过其他世代/对象；
- `commit_after_cancel` / `commit_stale_rebind` /
  `commit_after_retry_without_revalidation` (VIOLATION)：校验与提交之间授权
  被取消、重绑或重试打断且未重新校验；
- `commit_invalidation_unordered` (UNDECIDABLE)：失效事件证明先于提交，
  但与校验互不排序，无法确认它是否使该授权失效；
- `commit_binding_ambiguous` (UNDECIDABLE)：两个互不排序的 BIND 竞争句柄的
  当前绑定身份；
- `commit_unbound_identity` / `commit_unbound_handle` (VIOLATION)：提交身份
  从未被任何先于提交的绑定命名。

## 用法

```bash
# 文本报告（退出码即结论）
python3 -I run_audit.py examples/01_parallel_isolation.json

# 机器可读报告
python3 -I run_audit.py examples/04_missing_sync.json --json

# 乱序不变性回归：同一历史打乱 N 次，报告必须逐字节一致
python3 -I check_order_invariance.py examples/03_stale_generation.json \
    --iterations 100

# 完整测试套件（33 个用例）
python3 -I tests/test_verifier.py
```

所有 Python 调用均使用 `-I` 隔离模式，防止输入目录中被植入的 `json.py` 等
模块影响解释器加载。

## 验收场景（examples/）

| 文件 | 场景 | 预期 |
| --- | --- | --- |
| `01_parallel_isolation.json` | 两个并行任务、独立句柄，经 signal/wait 跨线程同步 | PASS |
| `02_cancel_then_revalidate.json` | 取消后新任务在同一句柄上重新校验再提交 | PASS |
| `03_stale_generation.json` | 同一任务在重绑到 gen2 后仍提交 gen1 | VIOLATION |
| `04_missing_sync.json` | 提交的 depends_on 指向缺失的同步记录 | UNDECIDABLE |

## 模块结构

```
handle_audit/
  events.py    事件 schema 与日志加载
  order.py     happens-before 图（线程边+同步边、BFS 证据路径、确定性拓扑序）
  model.py     内存文件模型（句柄表、对象世代、任务授权状态）
  verifier.py  规则引擎，三值结论
  findings.py  Finding/AuditReport 数据类型
  report.py    文本与 JSON 渲染
  cli.py       命令行入口
run_audit.py                -I 隔离模式包装入口
check_order_invariance.py   乱序不变性检查器
tests/test_verifier.py      33 个回归用例
```

## 设计保证

- **到达顺序无关**：所有判定只查询图可达性；finding 列表按确定性键排序，
  JSON 输出 `sort_keys=True`。相同历史任意打乱，报告逐字节一致（由
  `check_order_invariance.py` 与测试中的全排列/随机洗牌用例守护）。
- **证据可追溯**：每条 VIOLATION/UNDECIDABLE 都附带最短 happens-before
  路径（带边标签 `thread:.. seq` / `sync:..` / `depends_on`），报告列出
  违反约束的事件对与所依据的顺序证据。
- **回放确定**：内存模型按拓扑序回放，平局以 (thread, seq, event_id)
  打破，不依赖输入流顺序。
