# PNG 解码内存预算校验

针对图片上传安全评审结论：**仅核对压缩文件大小无法约束 PNG 解码服务的峰值内存**
（压缩包很小的图片同样可能解压出超大位图，即 decompression bomb）。

本模块只信任 IHDR 中已解析出的元数据，在任何像素缓冲分配**之前**完成预算判定，
并用记录式分配器（不真实持有像素内存，只做容量记账）验证双层约束。

## 判定模型

输入为本地 JSON 表示的已解析元数据：

```json
{
  "width": 16,
  "height": 16,
  "bitDepth": 8,
  "colorType": 6,
  "interlaceMethod": 0,
  "stage": "header"
}
```

`stage` 为当前解码阶段：`header / inflate / scanline / unfilter / convert / output`。

三种判定结果：

| decision | 含义 |
| --- | --- |
| `allow` | 元数据充分且峰值在单图上限内，任务获得预留 |
| `deny` + `limit: "per_image"` | 超出**单图**峰值上限（含准入阶段与分配阶段），附 `overBytes` |
| `deny` + `limit: "global"` | 单图合规但并发任务总和超出**全局总预算**，附 `overBytes` |
| `insufficient_metadata` | 字段缺失/非法/尺寸乘积溢出，信息不足一律不放行 |

## 缓冲与峰值推导（`src/pngMemoryBudget.js`）

- 打包扫描行：`1 字节 filter-type + ceil(width × bpp / 8)`，覆盖 1/2/4 位子字节像素；
- 展开扫描行：`1 字节 filter-type + width × channels × max(1, bitDepth/8)`；
- Adam7：按规范 §8.6 计算七趟子帧宽高（空趟跳过），按趟拼接的打包尺寸取
  `max(整图估算, 七趟之和)` 作为保守上界，行缓冲取最宽趟（最后一趟为全宽）；
- 每个阶段显式列出**同时存活的全部缓冲**，峰值取各阶段总和的最大值：
  - `inflate`：zlib 输出缓冲；
  - `scanline / unfilter`：inflate 输出 + 打包图像 + 当前打包行 + 当前展开行；
  - `convert`：打包图像 + 展开后全尺寸图像；
  - `output`：展开图像；
- 可选 `perAllocationOverheadBytes` 对每次分配额外记账（分配器元数据开销）。

压缩文件大小（`compressedBytes`）不参与任何计算——测试中两份 IHDR 相同、
压缩大小相差数千万倍的元数据推导出的峰值完全一致。

## 双层约束与归还

1. **单图上限**：`admit()` 时按峰值预算预留；`allocate()` 时再次校验该任务已用 + 申请 ≤ 预留。
2. **并发总预算**：记录式分配器对所有任务的存活缓冲实时求和，超限拒绝且不改变已用量。
3. **取消 / 提前失败**：`release(taskId, reason)` 批量归还该任务全部存活句柄，
   并发邻居的预算不受影响；归还后预算可立即被新任务复用。

所有允许与拒绝（含未到达分配器的单图层拒绝）都写入只追加的审计记录，
分配器另有底层记录，可通过 `stats().decisionRecords` / `allocatorRecords` 检查。

## 运行测试

```bash
npm test
```

24 项回归测试覆盖：行缓冲/Adam7 计算、压缩大小无关性、合法小图全流水线、
恰好达限通过、超 1 字节拒绝（区分 per_image / global）、元数据不足、
任务取消与提前失败后的预算归还、分配开销与审计记录。

## 目录

```
src/recordingAllocator.js   记录式分配器（容量记账 + 审计日志 + 按任务归还）
src/pngMemoryBudget.js      元数据校验、缓冲计划、PngMemoryBudget 双层预算管理器
test/fixtures/*.json        本地已解析元数据（含解压炸弹与非法样本）
test/pngMemoryBudget.test.js 回归测试
```
