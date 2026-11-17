# wasmcap — Wasm 插件宿主能力分析

读取构建器导出的 JSON 摘要，为每个导出入口计算该入口可能触达的宿主（import）函数及其能力，并给出证据路径与排除原因。用于上线前核对 WebAssembly 插件会触达哪些宿主能力。

## 输入摘要格式（version 1）

```json
{
  "version": 1,
  "types":        {"<type_id>": {"params": ["i32"], "results": []}},
  "functions":    {"<fn>": {"type": "<type_id>, "kind": "local"}
                  |{"type":<type_id>, "kind": "import", "module": "env", "name": "host_fn"}},
  "capabilities": {"<import fn>": ["<capability>"]},
  "direct_calls":   [{"caller": "f", "callee": "g", "site": "f@12"}],
  "indirect_calls": [{"caller": "f", "table": "tab", "expected_type": "t0", "site": "f@20"}],
  "tables":         {"tab": {"externally_writable": false, "initial": ["g", null]}},
  "element_updates":[{"table": "tab", "index": 1, "function": "h", "at": "f@30"}],
  "entries":        [{"name": "main", "function": "f", "allowed_capabilities": ["log"]}]
}
```

- 程序点写作 `func@offset`；校验时会检查 caller/写入点与函数一致。
- `capabilities` 是能力登记表：每个宿主导入函数 → 能力集合。登记项必须指向 import 函数。
- `element_updates` 表示表写入（`table.set` / 主动元素段），是 *may-write*：只要写入点所在函数从该入口可达，写入的函数就纳入该槽位的可能目标（不做写入排序，多次写同一槽位全部保留）。

## 分析语义

- **直接调用**：沿调用图可达。
- **间接调用**：只匹配与 `call_indirect` 期望类型**结构等价**（params/results 相同）的表元素；类型别名 id 不同但结构相同视为兼容。
- **表元素来源**：初始元素 + 写入点可达的元素更新。
- **外部可写表**：入口可达的间接调用若经由 `externally_writable` 表，报告 `unknown: true` 并给出原因——未知目标**不会**被当成无能力。
- **未登记的 import**：若被可达，同样置 `unknown`。
- **逐入口隔离**：每个入口独立做单调不动点迭代，结论分别保留；共享函数不会串扰。调用环与反复表更新在有限域上单调收敛（报告含 `converged`/`iterations`）。
- **排除解释**：`excluded_elements` 说明某表项为何不影响该入口——`type_mismatch`（签名不兼容，含期望/实际类型）或 `unreachable_update`（写入路径不可达，含写入程序点）。
- **越权**：`violations` = 实际能力 − 入口 `allowed_capabilities`，逐项点名。

## API

```python
from wasmcap import load_summary, parse_summary, analyze, analyze_entry

summary = load_summary("samples/c01_direct_indirect.json")   # 校验并构建模型
report  = analyze(summary)            # {"entries": [<每个入口的报告>]}
single  = analyze_entry(summary, summary.entries[0])
```

每个入口报告字段：`entry`、`root`、`reachable_functions`、`reachable_host_functions`、`capabilities`、`allowed_capabilities`、`violations`、`unknown`、`unknown_reasons`、`evidence`（每个宿主函数一条从入口出发的路径，步骤含 direct/indirect 及表、槽位、来源）、`excluded_elements`、`converged`、`iterations`。

## 命令

```bash
# 分析任意摘要（可重放样例见 samples/）
.venv/bin/python -m wasmcap analyze samples/c04_overprivilege_exclusions.json
.venv/bin/python -m wasmcap analyze samples/c03_entries_convergence.json --entry e1

# 本地核验
.venv/bin/python tools/local_verify.py --list
.venv/bin/python tools/local_verify.py --case C01   # C02/C03/C04/ALL
```

核验器对每组用例输出 JSON：`case`、`assertions`（实际断言数）、`passed`/`failed`/`skipped`、每条断言的 expected/observed 摘要及所用样本。任一断言失败、用例未知、零断言均非零退出。样例全部为合成数据，不含真实凭据或私有账户内容。

## 可重放样例（samples/）

| 样本 | 覆盖点 |
|---|---|
| `c01_direct_indirect.json` | 直接调用 + 结构兼容的间接调用命中宿主能力；类型不匹配表项被排除 |
| `c02_updates_unknown.json` | 可达表更新 / 不可达更新 / 类型不匹配 / 外部可写表未知目标 |
| `c03_entries_convergence.json` | 两入口共享函数、调用环、同一槽位反复更新；入口隔离与收敛 |
| `c04_overprivilege_exclusions.json` | 越权能力点名；敏感表项因类型不匹配/写入不可达被排除 |

## 未覆盖边界（审计时注意）

- **流不敏感**：元素更新是 may-write，不建模写入顺序与覆盖关系；同一槽位的旧值与新值都保留为可能目标（保守方向是过近似，不会漏报可达目标）。
- **无函数体语义**：不建模条件分支、参数值、返回值；可达性以构建器给出的调用边为准。
- **表目标仅限声明的表**：不建模多表间复制、内存中的函数指针或 `ref.func` 逃逸到线性内存的情形。
- **外部可写表只置 unknown**：不枚举宿主可能写入的具体函数；`unknown: true` 的报告必须人工复核，不能视为通过。
- **类型兼容仅按结构等价**：不建模子类型（如 GC 类型层级）。
- **登记表可信**：能力登记表本身作为输入信任；登记缺失只以 `unknown` 标出，不推断能力。
- 核验包装器是运行入口而非独立合格证明；审计仍应阅读 `wasmcap/` 源码、真实分析输出，并对不能证实的内容保留未知。
