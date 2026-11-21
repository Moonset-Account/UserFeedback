# 外排序分页算子（external_rank）

一次性行迭代器 → 按分数降序稳定外排序 → 跳过 M 行后流出前 N 行，并额外保住与第 N 行同分的后续行。纯本地组件：不联网、不探测外部目标，只使用合成夹具；临时段由本进程创建并只由本进程读回。

## 入口

| 内容 | 路径 |
| --- | --- |
| 实现 | `external_rank.py`（`ExternalRanker` / `SortStats`） |
| 验收用例 | `tests/test_external_rank.py`（unittest，37 个用例） |

运行方式（在仓库根目录）：

```sh
python3 -m unittest discover -s tests -v
```

Python 3.11.6 实测通过：`Ran 37 tests ... OK`。

## 契约

```python
ExternalRanker(
    rows,                 # 任意一次性可迭代对象，只被迭代一次
    slots=...,            # 内存记录槽位数，>= 4
    merge_fan_in=...,     # 单次归并的输入段上限，2 <= merge_fan_in <= slots - 1
    offset=M,             # 跳过行数，>= 0
    limit=N,              # 取行数，>= 1
    score_key=...,        # 可选，默认取 row["score"] / row.score，缺省为 None
    is_empty_score=...,   # 可选，默认 None / NaN / "" 视为空分数
    temp_parent=...,      # 可选，临时目录父路径
    stats=...,            # 可选 SortStats，收集诊断
)
```

- 结果对象本身是迭代器，也是上下文管理器。消费方提前结束时应 `close()` 或退出 `with`，临时目录会被立即删除；正常耗尽、`close()`、`__exit__` 以及对象被回收时都会清理。
- 排序键：非空分数降序；相同分数按输入序号升序（稳定）。空分数整体排最后，彼此并列且保持输入顺序。
- 窗口：先跳过 `offset` 行，再取 `limit` 行；若取满且第 N 行有非空分数，则继续流出与其同分的后续行。若 `offset` 切进一个同分组，被跳过的行不会因末尾同分扩展而补回。
- 空输入产出空迭代器，`stats.merge_passes == 0`。

## 外排流程

1. 以 `slots` 为上限缓冲记录，缓冲满即排序并写成初始段；不在内存中保存全部行，也不预读输入。
2. 段数超过 `merge_fan_in` 时做多趟归并，每趟最多同时打开 `merge_fan_in` 个段，因此归并峰值记录数不超过 `merge_fan_in` 个（每段一个当前记录），整体不超过 `slots`。
3. 最后一趟归并直接从段迭代器流给消费方，不再落盘。

`SortStats` 提供 `initial_run_sizes`、`merge_passes`、`segments_written`、`peak_buffered_records`、`temp_dir`，便于验证“末位同分横跨多个临时段”以及归并趟数确实随配置变化。

## 验证范围（`tests/test_external_rank.py`）

全部为真实断言，覆盖题面交叉约束：

- **可接受结果**：`test_skip_and_take_then_extend_tie`、`test_window_larger_than_input_returns_everything`、`test_duplicate_extremes_stable`、`test_empty_scores_sort_last_and_keep_input_order` 等。
- **应被拒绝/阻止**：`RejectionTests` 全部用例——`slots < 4`、`merge_fan_in < 2`、`merge_fan_in >= slots`、`limit <= 0`、`offset < 0`、非整数参数、不可调用的钩子，均抛 `ValueError`/`TypeError`，不静默夹取。
- **边界与交错**：
  - `test_offset_cutting_into_tie_does_not_restore_skipped_rows`：M 切进同分组时被跳过行不补回。
  - `test_limit_cutting_into_tie_extends_but_not_to_next_score`：N 切进同分组时只扩展到同分末尾。
  - `test_limit_landing_on_empty_row_extends_across_all_empties`：空分数并列作为末尾跨越边界。
  - `test_trailing_tie_matches_in_memory_order_across_configs`：末位 17 行同分，槽数 4–8、归并上限 2–7 共 8 组配置下与充足内存参考顺序完全一致；并断言无单个初始段容纳整个同分组、峰值不超过槽数、归并趟数出现多种取值。
  - `test_offset_and_limit_both_land_inside_cross_segment_tie`、`test_many_rows_multiple_merge_passes`：跨段同分与多趟归并。
  - `test_deterministic_fuzz_matches_reference`：固定种子随机用例，多种槽数/归并上限对拍独立实现的参考窗口。
- **一次性输入**：`OneShotInputTests` 断言构造时不消费、迭代入口只调用一次、恰好读到末尾一次。
- **资源释放**：`TempSegmentLifecycleTests` 断言正常耗尽、`with` 中 `break`、手动 `close()`、丢弃引用后，临时目录及其中所有段均被删除；提前结束后迭代器为空。
- **内存上限**：外排用例断言 `peak_buffered_records <= slots` 且每个初始段大小 `<= slots`。

参考实现 `reference_window` 在测试内独立实现排序与窗口语义，不复用算子内部排序代码。

## 限制

- 非空分数必须彼此可比较；混合类型（如 `int` 与 `str`）应在 `score_key` 中归一化。实现不做隐式转换，也不吞掉比较错误。
- 段文件是进程私有临时文件，使用 Python `pickle` 作为内部编解码。不要把 `ExternalRanker` 指向外部或不受信任的段文件；输入行本身只被排序和透传，不会被执行。
- 仅验证本地合成夹具；不含任何真实目标、网络访问或攻击载荷。
