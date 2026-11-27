# 净化导出器（Sanitizing Exporter）

面向内存块设备的只读净化导出。场景：测试磁盘映像交付供应商前，已删文件的旧内容仍散落在空闲扇区，卷快照之间共享物理块。导出器按每个获准视图的分配图与有效字节数生成净化副本——视图读到原先的已分配有效字节，其余一律读零。

## 设计保证

- **按视图净化**：每个 `VolumeView` 声明自己的 `valid_bytes`。同一物理块在两个视图中可有不同的有效尾界，各自得到独立净化副本；有效前缀保留，块内历史尾部（非零残留）被清零。
- **按净化后实际字节复用**：净化结果按内容寻址去重——内容相同的净化块（含全部零块：空洞、稀疏区、有效界之后的块）共享同一个导出物理块。
- **原子发布**：导出先在暂存区构建完整的新块存储与全套新块表，全部完成后以单次赋值切换发布态。中断（`ExportInterrupted`）时旧发布态原样保留；重新开始只能发布一整套一致的新块表，绝不发布半套。
- **输入校验**：重叠/越界的 extent 与稀疏区、超出容量的 `valid_bytes`、重复视图名均在暂存前拒绝，不影响已发布快照。

## 公开 API（`sanitized_export.py`）

| 名称 | 说明 |
| --- | --- |
| `InMemoryBlockDevice(block_size)` | 内存块设备；`write_block(id, data)` / `read_block(id)`（未写块读零）/ `block_ids()` |
| `Extent(logical_start, physical_block, block_count)` | 已分配区段：逻辑块区间映射到连续物理块 |
| `SparseRegion(logical_start, block_count)` | 稀疏区：读零、无后备存储 |
| `VolumeView(name, block_count, valid_bytes, extents, sparse)` | 一个获准导出的视图 |
| `SanitizingExporter(device)` | 导出器；`export(views) -> ExportSnapshot`；`snapshot` 属性为当前发布态；`interrupt_after` 为中断演练钩子 |
| `ExportSnapshot` | 不可变发布结果：`read(view, offset, length)`、`block_table(view)`、`reachable_blocks(view=None)`、`stored_blocks()`、`read_block(id)`、`view_names()`、`view_size(view)` |
| `ExportInterrupted` | 导出中断异常；暂存状态被丢弃，发布态不变 |

## 运行方法

```sh
python3 -m unittest discover -s tests -v
```

仅使用标准库，Python 3.11.6，无需安装依赖。

## 验证范围（tests/test_sanitized_export.py，16 个用例，全部实测通过）

- **正常场景**：已分配有效字节原样可读；跨块读取、偏移/长度、越界裁剪。
- **净化**：含非零历史尾部的合成块，尾部在导出块中被实际清零（直接断言存储块内容，而非仅读路径）；稀疏区与未分配空洞读零。
- **共享块**：同一物理块在两个视图中不同有效尾界 → 两个不同净化块；相同尾界 → 复用同一块。
- **内容复用**：相同净化内容合并；所有零块收敛为单一零块；存储块集合与可达块集合精确相等（无夹带孤儿块、无缺失）。
- **边界**：`valid_bytes = 0`；`valid_bytes` 恰在块边界；extent/稀疏/空洞交错布局。
- **原子性**：注入中断后发布态保持旧快照（同一对象）；重试后发布完整全套新块表且内容正确；第二次导出失败不破坏第一次的快照。
- **校验**：重叠 extent、extent 与稀疏区重叠、越界区段、非法 `valid_bytes`、重复视图名均被拒绝且不污染发布态。

未覆盖：真实磁盘/文件系统接入、并发导出、持久化崩溃恢复（当前为单线程内存模型，原子性由单次赋值保证）。
