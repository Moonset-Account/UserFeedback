# 页面级崩溃恢复模块（本地基础设施）

纯 Python 标准库实现的页面存储恢复组件：数据库重开时，依据**旧的完整页备份、
当前页文件和追加日志**把页面恢复到恢复点，并报告实际读取的备份页号与实际改写
的页号。不依赖任何第三方库，不联网。

## 目录结构

```
db_recovery/
  __init__.py     公共导出
  page.py         固定页格式：页头（提交号 + 载荷长度 + CRC32）与序列化/解析
  journal.py      追加日志帧格式、JournalWriter、read_journal（欠字节/损坏定位）
  store.py        PageStore 抽象：MemoryPageStore（测试）与 DirectoryPageStore（目录）
  recovery.py     恢复主流程 recover(...)
tests/
  test_recovery.py  本地合成数据单元测试（unittest）
```

## 磁盘格式

### 页格式（`db_recovery/page.py`）

大端编码，页头 16 字节：

| 偏移 | 长度 | 字段 | 说明 |
| --- | --- | --- | --- |
| 0 | 8 | `commit_seq` | 该页内容最后一次落盘所属的已提交事务号，初始为 0 |
| 8 | 4 | `payload_len` | 载荷字节数 |
| 12 | 4 | `crc32` | 对 `commit_seq + payload_len + payload` 计算的 CRC32（不含 crc 字段本身） |
| 16 | 变长 | `payload` | 页内容，长度不超过 `page_size - 16` |

解析时长度不符或 CRC 不匹配即判定该页损坏。

### 追加日志帧格式（`db_recovery/journal.py`）

大端编码，固定头 17 字节、尾 CRC 4 字节：

| 偏移 | 长度 | 字段 |
| --- | --- | --- |
| 0 | 4 | magic `JRnL` |
| 4 | 1 | 记录类型：`WRITE=1` / `COMMIT=2` |
| 5 | 4 | body 长度 |
| 9 | 8 | seq：WRITE 帧为事务号；COMMIT 帧为提交号 |
| 17 | 变长 | body |
| 末尾 | 4 | 对 `type + length + seq + body` 计算的 CRC32 |

* WRITE body：`page_id(uint64) offset(uint64) replacement(替换字节)`。
* COMMIT body：`transaction_id(uint64)`；帧头 seq 即该事务的提交号。

## 恢复算法（`db_recovery.recovery.recover`）

1. **完整扫描日志**：写记录按事务归组并保持日志原次序；提交记录给出提交号，
   提交号必须严格递增，否则抛 `RecoveryError`。
2. **确定恢复点**：显式传入 `recovery_point` 时使用之；否则取日志最后一个提交号
   （无提交时为 0）。只有「已提交且 `commit_seq <= 恢复点`」的事务合格。
   **未提交事务的写按事务整体排除**——一个事务跨多少页都不会被部分应用。
3. **逐页选择基线**（候选页 = 当前页 ∪ 备份页 ∪ 合格补丁涉及页）：
   * 当前页**校验正确且提交号未越过恢复点** → 以当前页为基线，从其自身提交号
     续做，**不读取备份**；
   * 当前页**损坏、缺失或越过恢复点** → 从备份页重建，备份页号计入
     `backup_pages_read`；备份同样损坏/缺失/越点则恢复失败。
4. **内存打补丁**：同一事务内严格按日志原次序应用；写区间超出页载荷边界即判
   损坏，抛 `RecoveryError`。页的新提交号取本页最后一条补丁所属事务的提交号，
   不允许相对基线回退。
5. **统一发布**：所有页都在内存中规划成功后才写回当前存储；仅当新字节与现状
   不同才写，故 `rewritten_pages` 只包含真正变化的页。任何错误都发生在发布之前，
   不会留下半写状态。

### 日志欠字节与中段损坏

* **末条欠字节**：日志尾部读不满一条完整帧（头部欠字节或 body/CRC 欠字节）时，
  视为截断，`RecoveryResult.truncated` 给出截断字节位置 `position` 与尚缺字节数
  `missing`（头部即欠字节、无法确定帧长时为 -1）；欠字节之前的完整记录照常恢复。
* **中段完整记录校验失败**：magic 错误、长度非法或 CRC 不匹配时，
  `read_journal` 抛 `JournalCorrupt`，其中 `position` 为坏记录的字节偏移；
  `recover` 不捕获该异常，直接失败且**不发布任何页**。

### 幂等性（再开一次两张清单皆空）

恢复发布的页是「基线 + 合格补丁」的确定性字节结果。对恢复结果再次执行
`recover`：当前页全部校验正确且不越点 → 不读备份（`backup_pages_read == []`）；
重放补丁重建出的字节与现状逐字节相同 → 不写回（`rewritten_pages == []`）。
测试 `test_committed_transactions_applied_in_commit_order_and_idempotent` 与
`test_multi_page_recovery_lists_and_idempotency` 覆盖该性质。

## 用法示例

```python
from db_recovery import (
    DirectoryPageStore, JournalWriter, build_page, recover,
)

PAGE_SIZE = 4096
current = DirectoryPageStore("/data/db/current")   # 当前页文件
backup = DirectoryPageStore("/data/db/backup")     # 旧完整页备份

# 正常运行期写日志（恢复侧只需要读日志；这里展示格式如何产生）
with open("/data/db/journal.log", "ab") as raw:
    jw = JournalWriter(raw)
    jw.write(transaction_id=1, page_id=7, offset=0, replacement=b"hello")
    jw.commit(transaction_id=1, commit_seq=1)

# 重开数据库时恢复；recovery_point=None 表示恢复到最后一个提交
result = recover(current, backup, "/data/db/journal.log")
print(result.recovery_point)        # 实际恢复点
print(result.applied_commit_seqs)   # 应用的提交号（提交先后次序）
print(result.backup_pages_read)     # 实际读取的备份页号
print(result.rewritten_pages)       # 实际改写的页号
print(result.truncated)             # 末条欠字节信息，无则 None
```

## 测试说明（重要：未运行）

`tests/test_recovery.py` 为 **unittest 本地合成数据测试**，全部使用内存页存储与
`io.BytesIO`/临时目录，不含任何外部服务、凭据或网络访问。覆盖场景：

1. 多事务按提交次序应用 + 再开一次两张清单为空
2. 未提交事务的跨页写整体排除
3. 当前页从自身已落盘提交号续做、不读备份
4. 当前页损坏时从备份重建并计入读取清单
5. 恢复点过滤后续提交
6. 当前页越过恢复点时从备份重建
7. 日志末条欠字节截断、前部记录照常应用
8. 中段完整记录 CRC 失败返回坏位置且零发布
9. 多页改写清单与二次恢复幂等
10. 重建所需备份缺失 → 失败且零发布
11. 补丁越界写 → 失败且零发布
12. 同事务内写次序保持、后写覆盖先写
13. 日志读取器：body 欠字节计数、bad magic 定位、空日志、提交号非严格递增

**按本次任务约束，本会话只编写源码与测试源码，未执行任何代码、命令或测试，
因此这些测试均未运行，不宣称验证通过。** 后续如需运行，可在工作目录执行：

```
python -m unittest discover -s tests -v
```

## 设计备注与边界

* 目录存储写页采用「临时文件 + `os.replace` 原子替换」，避免发布中断留下半页。
* 损坏的备份页不会被静默跳过：需要从备份重建而备份不可用时恢复明确失败。
* 恢复流程不截断、不删除原日志文件；欠字节位置只通过结果上报，由上层决定是否
  物化截断，避免恢复组件擅自改写输入。
* 64 MiB 的日志帧 body 上限仅为防御损坏长度字段的内存保护，不限制正常页大小
  （页容量在 `page_size - 16` 之内即可）。
* 本组件只实现本地页面恢复基础设施，不含事务协调、并发控制、垃圾回收或任何
  网络协议；也不包含任何漏洞利用、目标探测类代码。
