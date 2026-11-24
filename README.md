# mac_access — 端点安全策略访问控制模块

用于端点安全策略验证的强制访问控制（MAC）参考监视器。模拟涉密终端上的
"进程 / 共享内存对象"标签模型，验证**读后升密、升密即收回、写回通道阻断**
这一组防御性策略。模块只做题面规定的策略判断，不包含任何利用或攻击载荷。

## 策略模型

标签 `Label(level, categories)`：

- `level`：非负整数等级；`categories`：类别集合。
- 标签 `a` **支配** `b`（`a.dominates(b)`）当且仅当
  `a.level >= b.level` 且 `a.categories ⊇ b.categories`。
- 合并 `a.merge(b)`：等级取大、类别取并（最小上界）。
- `Clearance` 是进程的许可上限；标签在上限之内当且仅当上限标签支配它。

参考监视器 `AccessMonitor` 执行的规则：

1. **读取判定**：读取对象后拟得到的新标签 = 进程标签 ∪ 对象标签。
   - 新标签超出进程 `Clearance` → **整次读取失败**：不返回内容、不修改
     进程标签、不触发任何收回（原子失败）。
   - 否则读取成功；仅当新标签确实高于旧标签时状态为
     `READ_OK_UPGRADED`，否则为 `READ_OK`。
2. **升密即收回**：每次真正升密时，收回该进程所有**可写共享映射**中
   目标标签不再支配新进程标签的映射。只读映射不收回。
3. **句柄联动**：同一映射 `duplicate_handle` 复制出的所有句柄共享映射
   状态，映射被收回时全部句柄同时失效。
4. **写回判定**：
   - 通过已收回（或目标标签已不能覆盖当前进程标签）的句柄写回 →
     拒绝，状态 `MAPPING_REVOKED`，目标内容保持原值不变。
   - 目标标签仍能覆盖新标签的高密目标 → 正常写入，状态 `WRITE_OK`。
5. **排队写回**：写回可先入队、后到达。交付时按 FIFO 顺序对**当前实时**
   标签 / 映射状态重新判定，因此"升密前排队、升密后才到达"的低密写回
   会被拒绝，而高密目标写回照常完成。

调用方通过结果对象上的 `AccessStatus` 显式区分：读取被拒
（`READ_DENIED_CLEARANCE`）、映射被收回（`MAPPING_REVOKED`）、
写回成功（`WRITE_OK`），另含读取成功的两种状态
（`READ_OK` / `READ_OK_UPGRADED`）。

## 目录结构

```
mac_access/
  __init__.py        # 公开 API 导出
  labels.py          # Label / Clearance
  monitor.py         # AccessMonitor 及实体、状态、结果类型
tests/
  test_labels.py       # 标签支配 / 合并 / 许可上限
  test_read_policy.py  # 读取接受、升密、超限整次拒绝
  test_revocation.py   # 可写映射收回与句柄联动
  test_interleaving.py # 排队写回与升密交错
  test_edge_cases.py   # 错误路径、多进程隔离、三态区分
  test_end_to_end.py   # 题面完整故事线端到端断言
```

## 公开 API

### `mac_access.labels`

| API | 说明 |
| --- | --- |
| `Label(level, categories=None)` | 不可变标签；支持 `dominates`、`merge`（`|`）、比较运算符 `>= <= > <`、`==` |
| `Clearance(bound)` / `Clearance(level, categories)` | 许可上限；`permits(label)` 或 `label in clearance` 判定标签是否在上限内 |

### `mac_access.monitor`（经 `mac_access` 包导出）

| API | 说明 |
| --- | --- |
| `AccessMonitor()` | 创建参考监视器 |
| `register_process(pid, label, clearance)` | 注册进程（初始标签必须在上限内） |
| `create_object(oid, label, content=b"")` | 创建带标签的共享内存对象 |
| `open_mapping(pid, oid, writable=...)` | 打开共享映射并返回句柄；目标标签须支配进程当前标签 |
| `duplicate_handle(handle_id)` | 复制句柄；副本与原句柄共享收回状态 |
| `read(pid, oid) -> ReadResult` | 读取：判定许可、必要时升密并收回映射 |
| `write_back(pid, handle_id, content) -> WriteResult` | 立即写回，按实时状态重新判定 |
| `queue_write_back(pid, handle_id, content) -> seq` | 排队写回，返回队列序号 |
| `flush_pending_writes() -> list[WriteResult]` | FIFO 交付全部排队写回 |
| `pending_writes()` | 查看尚未交付的排队请求 |
| `is_handle_revoked(handle_id)` / `mapping_status(handle_id)` | 查询句柄 / 映射状态 |
| `get_process` / `get_object` / `get_handle` | 实体查询 |

结果对象：

- `ReadResult`：`status`、`accepted`、`denied`、`upgraded`、`content`
  （被拒时为 `None`）、`old_label`、`new_label`、`revoked_handles`、`reason`。
- `WriteResult`：`status`、`applied`、`refused`、`object_id`、
  `handle_id`、`content`、`reason`。

异常（API 误用，与策略拒绝不同；策略拒绝走状态码而非异常）：
`AccessControlError`、`UnknownProcessError`、`UnknownObjectError`、
`UnknownHandleError`、`ForeignHandleError`。

## 使用示例

```python
from mac_access import AccessMonitor, AccessStatus, Clearance, Label

m = AccessMonitor()
m.register_process("p", Label(1), Clearance(Label(4, {"a", "b"})))
m.create_object("low", Label(1), b"original")
m.create_object("vault", Label(4, {"a", "b"}), b"vault")
m.create_object("dossier", Label(3, {"a"}), b"secret-text")

low = m.open_mapping("p", "low", writable=True)
vault = m.open_mapping("p", "vault", writable=True)

# 升密前排队两个写回，均尚未交付
m.queue_write_back("p", low.handle_id, b"tamper")
m.queue_write_back("p", vault.handle_id, b"vault-new")

# 读取高密材料 -> L1 升到 L3{a}，低密可写映射被收回
r = m.read("p", "dossier")
assert r.status is AccessStatus.READ_OK_UPGRADED
assert low.revoked and not vault.revoked

# 排队请求此时才到达：低密写回被拒且原值不变，高密写回完成
for w in m.flush_pending_writes():
    print(w.status)
# AccessStatus.MAPPING_REVOKED
# AccessStatus.WRITE_OK
assert m.get_object("low").content == b"original"
assert m.get_object("vault").content == b"vault-new"
```

## 运行方法

要求 Python 3.11（开发与验证环境为 3.11.6），仅使用标准库，无需安装依赖。

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围（合成数据，全部本地执行）

共 47 个测试，全部通过，覆盖：

- **标签基础**：等级偏序、类别包含、不可比较标签、最小上界合并、
  许可上限边界（恰好等于上限允许、超等级 / 缺类别拒绝）、不可变性与非法入参。
- **读取策略**：同标签读取不升密、等级 / 类别升密、读到上限边界接受、
  超上限整次拒绝（无内容、标签不变、无收回）、连续读取的单调升密、
  被拒读取无任何副作用。
- **收回语义**：升密仅收回不再被目标标签支配的可写映射、高密目标映射
  保留且可写、复制句柄联动收回、只读映射不收回、多次升级分批收回、
  已收回映射不会复活、纯类别升级触发的收回。
- **操作交错**：升密前排队 / 升密后到达的低密写回被拒且目标保持原值、
  同批高密目标写回成功、升级前已交付的写回不受追溯影响、复制句柄排队
  写回同样被拒、跨升级 FIFO 顺序、直接写回与排队写回语义一致、
  被拒读取不影响后续排队写回。
- **边界与错误**：未知进程 / 对象 / 句柄的具名异常、跨进程使用句柄
  （`ForeignHandleError`）、打开映射时的标签前置校验、多进程收回隔离、
  只读句柄写入按 API 误用拒绝、字节数组入参归一化、重复注册拒绝、
  五种状态码的三态可区分性。

## 设计说明与边界

- 策略拒绝（读取超许可、写回遇收回）通过 `AccessStatus` 返回，便于调用方
  分支处理；API 误用（未知实体、外进程句柄、只读句柄写入）才抛异常。
- 排队写回在入队时只做属主 / 可写性校验，**交付时**才做实时策略判定，
  这是"排队早于升密、到达晚于升密"场景得到正确结果的关键。
- 模型为进程内引用监视器模拟，不涉及真实共享内存系统调用、IPC 或网络；
  内容以 `bytes` 保存，被拒写回保证不触碰目标内容。
- 仅实现题面规定的防御性访问判定，不生成、不执行任何利用 / 入侵 / 规避载荷。
