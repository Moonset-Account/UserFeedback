# 逐帧受保护窗口遮挡模块

一个仅使用 Python 标准库的逐帧遮挡参考实现。它根据显示器缩放/旋转、窗口几何、
叠放顺序以及帧携带的拓扑/窗口版本，计算每块屏幕实际需要遮住的像素区域，并处理
拖动帧先于对应窗口状态到达时的局部暂停发布。

## 运行方法

要求 Python 3.11.6（或兼容的 Python 3.11+），无需安装第三方依赖。

运行全部测试：

```bash
python3 -m unittest discover -s tests -v
```

运行本地合成像素网格演示：

```bash
python3 -m frame_occlusion.demo
# 等价地：python3 demo.py
```

演示全部使用内存中的合成字符像素，不访问网络、真实屏幕、凭据或外部服务。

## 公开 API

主要导出位于 `frame_occlusion` 包：

| API | 作用 |
| --- | --- |
| `Rect(x, y, width, height)` | 全局逻辑坐标矩形；右/下边界为 `x+width`、`y+height` |
| `PixelGrid.from_rows([...])` / `PixelGrid(...)` | 小型像素网格；`None` 表示该像素不得发布 |
| `Display(id, bounds, scale=1.0, rotation=0, scale_y=None, ...)` | 单块显示器的逻辑范围、缩放和旋转 |
| `DisplayTopology(version, displays)` | 带版本号的显示拓扑 |
| `Window(id, rect, z_index, protected, visible, opacity, opaque)` | 窗口几何、叠放层级、保护属性和不透明度 |
| `WindowSnapshot(version, windows)` | 某一窗口版本的完整不可变快照 |
| `FramePacket(frame_id, topology_version, window_version, images, ...)` | 一帧或同一时刻的一组显示器帧 |
| `OcclusionEngine()` | 保存版本状态并逐帧计算遮挡/暂停区域 |
| `FrameResult` / `DisplayResult` / `PixelPatch` | 处理结果、每屏结果和可发布矩形补丁 |
| `release_withheld_patches(ready, pending)` | 版本到达后，只提取之前暂停、现在已安全遮挡的补丁 |
| `compute_display_occlusion_rects(...)` | 不经过版本状态，直接计算某屏的遮挡矩形 |
| `protected_visible_logical_regions(...)` | 计算每个受保护窗口未被完全不透明窗口盖住的逻辑区域 |

最小调用顺序：

```python
from frame_occlusion import (
    Display, DisplayTopology, FramePacket, OcclusionEngine,
    PixelGrid, Rect, Window, WindowSnapshot,
)

topology = DisplayTopology(1, [Display("screen-0", Rect(0, 0, 8, 6), scale=2)])
windows = WindowSnapshot(1, [
    Window("secret", Rect(2, 1, 3, 2), z_index=10, protected=True),
])
frame = FramePacket.single_display(
    "frame-0001", "screen-0", PixelGrid(16, 12, "."),
    topology_version=1, window_version=1,
)

engine = OcclusionEngine(mask_char="#")
engine.submit_topology(topology)
engine.submit_windows(windows)
result = engine.process_frame(frame)

assert result.is_complete
for patch in result.patches:
    ...  # 发布 patch.rect 对应的 patch.image
```

## 坐标、缩放和旋转规则

1. 窗口 `rect` 和 `Display.bounds` 使用同一个全局逻辑桌面坐标系，允许负坐标。
2. 窗口矩形先与每块显示器的 `bounds` 求交，因此跨屏窗口会按各屏范围分别裁剪。
3. `scale`（或分别指定 `scale_y`）把逻辑边界映射到像素边界。
4. `rotation` 定义为**从逻辑显示矩形顺时针变换到原始采集帧缓冲区**的角度，只支持
   `0/90/180/270`。例如 `rotation=90` 且逻辑范围为 `5x3` 时，原始帧尺寸为 `3x5`。
   若平台文档定义的是反向旋转，传入 `(-reported) % 360` 即可。
5. 分数缩放产生非整数边界时，使用 `floor(起点)` / `ceil(终点)` 向外扩展，确保半覆盖
   的受保护像素也不会漏出。
6. 传入帧的尺寸必须与 `Display.raw_pixel_width/height` 一致，否则抛出
   `FrameDimensionError`，避免按错误分辨率遮挡。

## 实际可见像素与不透明遮盖

`z_index` 越大越靠上；相同 `z_index` 保持快照中的稳定顺序。

对于每个 `protected=True` 且确有可见像素的窗口：

- 从其矩形中减去所有更高 z 序、`visible=True`、`opacity >= 1.0` 且未显式标记
  `opaque=False` 的窗口矩形。
- 剩余区域才会填入遮挡字符（实际系统中可替换为纯黑/模糊/不透明马赛克）。
- 被完全不透明普通窗口盖住的部分不打码，因此合成帧中原本属于该普通窗口的像素会保留。
- 半透明窗口不会被视为完全遮盖；保守起见仍遮挡下方受保护区域。
- 如果遮盖者本身也是受保护窗口，它自己的可见区域仍会被遮挡，最终并集不会泄露内容。

## 版本交错与局部暂停

引擎分别保存拓扑和窗口快照的精确版本。

### 精确版本已到达

当帧中的 `topology_version` 和 `window_version` 都能在引擎中找到时，状态为
`complete`，输出整屏补丁，其中受保护可见像素已替换为掩码。

### 窗口状态未到达，但有旧版本基线

拖动帧携带较新的 `window_version`，而该快照尚未到达时：

- 自动选择已保存的、版本号小于帧版本的最高旧快照作为基线；也可用
  `fallback_window_version` 显式指定。
- 用基线继续遮挡其他未受影响区域中的已知受保护窗口。
- `FramePacket.affected_logical_rects` 或 `affected_pixel_rects` 给出拖动/damage/
  保守影响范围。该范围经跨屏裁剪/旋转后进入 `paused_rects`。
- 暂停区域在 `safe_grid` 中标记为 `None`，不会出现在任何 `PixelPatch` 中；其他区域
  被切成可发布矩形补丁，照常采集/发送。
- 预览时可用 `preview_rows(hold_char="?")` 把暂停格显示为 `?`；`?` 只是可视化标记，
  不在发布补丁内。
- 对应窗口快照到达后，用同一个 `FramePacket` 重新调用 `process_frame`，再用
  `release_withheld_patches(ready, pending)` 只提取并补发之前暂停的区域。

如果两个 affected 参数都为 `None`，模块无法知道新几何可能影响哪里，因此**故障安全地
暂停整块显示器**。调用方若显式传入空影响范围，则表示自行保证没有分类变化，模块不会
暂停区域，但结果状态仍标记为 `window_version_pending`。

### 没有旧窗口基线或拓扑版本缺失

- 没有任何更旧窗口快照时，无法识别已知受保护窗口，整块屏幕暂停，不产生补丁。
- 精确拓扑版本缺失时无法进行坐标变换，同样暂停该帧中的全部显示器。
- 状态消息之后到达时，可重新处理同一帧；测试覆盖了“帧先到 → 拓扑到 → 旧窗口状态到
  → 新窗口状态到”的完整交错顺序。
- 已存在的更新版本（例如 v3）不会被隐式当作较旧帧 v2 的几何基线，因为新几何可能已经
  移走；需要时可显式指定 `fallback_window_version=3`。

## 像素网格演示摘要

演示构造了两块屏幕：左屏 `5x3`、无旋转；右屏逻辑范围 `5x3`、`rotation=90`，原始网格
为 `3x5`。跨屏受保护窗口的逻辑矩形是 `Rect(4, 1, 3, 1)`，其中右屏部分又被
`Rect(6, 1, 1, 1)` 的完全不透明普通窗口盖住。

处理结果：

```text
left published:          right published:
.....                    .#.
....#                    .O.
.....                    ...
                         ...
                         ...
```

左屏遮挡原始像素格 `(4,1)`；右屏旋转后本应遮挡 `(1,0)-(1,1)`，但 `(1,1)` 被不透明
`O` 窗口覆盖，所以只遮挡 `(1,0)`，并保留正常画面像素 `O`。

拖动交错演示中，v2 帧先到时，合并的虚拟桌面网格如下（`|` 是显示器边界）：

```text
.#..|...N
..??|??..
N...|..N.
```

每块 `4x3` 屏幕只暂停 2 个像素格，共发布 `20/24` 个像素；静态受保护窗口仍显示为
`#`，普通 `N` 像素照常进入补丁。v2 窗口状态到达后，跨到右屏的新位置输出：

```text
.#..|...N
....|##..
N...|..N.
```

并且只补发之前暂停的两个 `2x1` 补丁。

## 验证范围

当前实测命令：

```bash
python3 -m unittest discover -s tests -v
```

实测结果：`Ran 31 tests ... OK`。

测试使用本地合成数据，覆盖：

- 矩形相交、边界接触、区域相减和负尺寸校验；
- DPI 缩放、负全局坐标、分数缩放向外覆盖、90/180/270 度旋转、缩放+旋转；
- 跨屏矩形按显示器边界裁剪，以及屏外/仅边界相交；
- 受保护窗口遮挡、完全不透明普通窗口覆盖后保留正常像素；
- 半透明窗口、显式非不透明窗口、z 序和受保护遮盖者的并集行为；
- 早期拖动帧的局部暂停、补丁与暂停矩形不相交、补丁中不存在未处理的合成受保护像素；
- 影响范围的逻辑坐标/原始像素坐标输入与裁剪；
- 缺少 affected 范围或旧基线时的全屏 fail-closed；
- 拓扑和窗口状态乱序/迟到、精确版本到达后的重处理与只补发暂停区域；
- 演示程序和关键网格输出。

## 设计边界

本实现处理的是题面要求的防御性遮挡判断和发布区域控制。窗口以轴对齐矩形表示；若平台
提供异形窗口 alpha 蒙版、圆角裁剪或每像素透明度，需要把矩形区域计算替换为对应蒙版
运算。对于早到且缺少精确窗口状态的帧，细粒度局部发布依赖采集侧提供保守的
damage/drag/affected 区域；没有该信息时模块选择暂停整块屏幕，而不是冒险外发可能泄露
的像素。
