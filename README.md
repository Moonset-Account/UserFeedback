# SEC-VULN-017 — 深度受限的事件求值器（C++17）

原生策略服务把每份配置文档预解析为扁平事件流（容器进入 `EnterContainer`、
字段读取 `FieldRead`、容器退出 `ExitContainer`）。原递归求值路径在深层嵌套
输入下可能耗尽调用栈。本项目用**迭代式、带深度预算**的求值器替代递归路径。

## 安全语义

- **每份文档独立计数**：深度计数与事件编号在 `begin_document` 时重置，文档间互不影响。
- **进入前检查**：`EnterContainer` 若会使深度超过预算，求值器在**进入该容器之前**
  使文档失败，深度与容器栈保持不变；错误记录携带该事件在本文档事件流中的
  0 基事件号（`EvalError::event_no`）。
- **无部分发布**：策略只在 `end_document()` 成功时整体进入 `published()` 结果记录；
  失败或取消的文档不发布任何内容，且其状态被完全丢弃，不会污染下一份文档。
- **分批 / 暂停 / 继续**：`feed(batch)` 可多次调用，状态跨批次保持（进入与退出
  事件可分属不同批次）；`pause()` 后 `feed` 返回 `Paused` 且不消费事件，
  `resume()` 后从断点继续。
- **取消**：`cancel()` 丢弃当前文档的全部状态，下一份文档从零开始独立计数。

## 目录结构

- `src/event_evaluator.h` / `src/event_evaluator.cpp` — 核心求值器（仅依赖标准库）
- `src/main.cpp` — CLI 适配器（stdin 事件脚本 → stdout 结果记录）
- `tests/test_event_evaluator.cpp` — 原生 C++17 断言测试
- `tools/local_verify.py` — 本地编译/运行包装器（Python 3 标准库，argv 数组调用
  `/usr/bin/clang++`，不使用 shell；临时构建产物位于本工作区内）

## 本地可重跑入口

```sh
python3 tools/local_verify.py
```

包装器会：从当前源码重新编译测试与 CLI（`-std=c++17 -Wall -Wextra -Werror`）、
运行原生测试、再用合成离线样例驱动 CLI 并校验结果记录。任何编译失败、
零测试执行或断言失败都会以非零退出。也可手动：

```sh
/usr/bin/clang++ -std=c++17 -Wall -Wextra -O1 \
  src/event_evaluator.cpp tests/test_event_evaluator.cpp -o build/tests && ./build/tests
/usr/bin/clang++ -std=c++17 -Wall -Wextra -O1 \
  src/event_evaluator.cpp src/main.cpp -o build/cli && ./cli < sample.txt
```

## CLI 输入格式（每行一条命令，空白分隔）

```
maxdepth <n>        设置深度预算（必须在首个 begin 之前）
begin <doc_id>      打开新文档
enter <name>        容器进入事件
field <key> <value> 字段读取事件
exit                容器退出事件
pause / resume      暂停 / 继续批处理
end                 结束当前文档（成功才发布）
cancel              取消当前文档
# ...               注释
```

## 输出语义

- `PUBLISH <doc_id> fields=<n>` + `FIELD <path> = <value>` — 成功发布的策略记录
- `ERROR <doc_id> event=<no> <message>` — 失败记录（含文档内 0 基事件号）
- `FEED_STATUS <Ok|Paused|NoDocument|Failed>`、`END_OK` / `END_FAILED`、
  `CANCELLED`、`SUMMARY published=<n> errors=<n>`
- 进程退出码：脚本被完整处理即为 0；单份文档的失败以 `ERROR` 记录表达。

## 需求 → 测试映射

| 需求 | 用例（tests/，稳定 case_id） | 关键断言 |
|---|---|---|
| 最大合法深度 | `T1_max_legal_depth` | 深度恰等于预算时成功发布，字段路径正确 |
| 超过上限一层 | `T2_exceed_by_one_level` | 第 4 层进入前失败；`event_no==3`；深度停在 3；后续输入被拒；无发布 |
| 进入/退出分属不同批次 + 暂停继续 | `T3_enter_exit_split_batches` | 暂停时 `feed` 返回 `Paused` 且不消费；恢复后跨批次完成并发布 |
| 取消后打开新文档 | `T4_cancel_then_new_document` | 取消后无发布；新文档深度/事件号从零独立计数 |
| 结果记录验证隔离性 | `T5_result_records_isolation` | 超限文档无部分发布；`published()` 仅含下一份文档；错误记录归属超限文档 |
| 根处多余退出 | `T6_unbalanced_exit_rejected` | 以事件号报错，不发布 |
| 容器未闭合即结束 | `T7_unclosed_containers_fail` | `end_document` 失败、不发布，下一份文档不受影响 |

CLI 样例（`tools/local_verify.py` 内置）：超限文档 `deep` 产生
`ERROR deep event=3 ...` 且无 `PUBLISH deep`；随后的 `shallow` 正常发布，
`SUMMARY published=1 errors=1`。

## 限制

- 深度预算为构造参数（CLI 默认 32，可用 `--max-depth` 或 `maxdepth` 命令调整）；
  预算为 0 时任何容器进入都会超限。
- 事件号按文档内**已消费**事件从 0 开始编号；失败事件本身不计入（错误记录
  中的 `event_no` 即该失败事件的编号）。
- 求值器为单线程设计；外部系统（策略存储等）以内存结果记录模拟，无网络依赖。
