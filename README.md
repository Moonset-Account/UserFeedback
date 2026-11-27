# 取证坐标溯源模块（Forensic Coordinate Provenance）

扫描档案的一张纸上可能拼了多个原始页面，之后又经历旋转、裁切、重新拼版，
局部还可能被后画的空白覆盖。本模块记录每一步版面操作，并对**成品中的任意
矩形选区**做逆向溯源，拆出实际可见部分各自来自哪一页、哪一片原始坐标。

纯 Python 3.11 标准库实现，无第三方依赖。

## 坐标约定

* 所有矩形都是**半开像素区间** `Rect(x0, y0, x1, y1)`，即 `[x0, x1) × [y0, y1)`；
  宽度 `x1 - x0`、高度 `y1 - y0`，要求严格非空（`x0 < x1` 且 `y0 < y1`）。
* 坐标必须是整数（`bool` 与浮点会被拒绝）。
* 仅边界相接的两个矩形不算相交，边界像素归属于区间闭的一侧，天然不重不漏。

## 变换语义

每一步（`Step`）只做一种几何变换，配合目标位置 `(target_x, target_y)`：

| `Transform`   | 含义                                   | 脚印尺寸           |
| ------------- | -------------------------------------- | ------------------ |
| `IDENTITY`    | 纯平移拼版，不缩放不旋转               | `w × h`            |
| `SCALE`       | 整数缩放，因子 `factor`                | 见下               |
| `ROTATE_90`   | 顺时针 90°（四分之一转）               | `h × w`（交换宽高）|
| `ROTATE_180`  | 顺时针 180°（半转）                    | `w × h`            |
| `ROTATE_270`  | 顺时针 270°（等价逆时针 90°）          | `h × w`（交换宽高）|

缩放因子 `factor`：

* `factor >= 2`：放大，每个源像素覆盖 `factor × factor` 个目标像素格；
* `factor <= -2`：缩小，`|factor| × |factor|` 个源像素聚合进一个目标格。
  要求来源宽高能被 `|factor|` 整除（构造期校验），避免边缘分数格歧义。

**缩小逆向的关键规则**：一个目标像素格可能对应多个源像素，溯源结果返回
覆盖这些源像素的**完整来源范围**（按 `k` 倍数网格取下/上界），而不是只
倒推该格中心的单个像素。

## 公开 API

全部位于 `provenance.py`：

### 数据类型

* `Rect(x0, y0, x1, y1)` — 半开像素矩形（namedtuple，可解包/下标访问）。
* `Transform` — 变换枚举：`IDENTITY` / `SCALE` / `ROTATE_90` /
  `ROTATE_180` / `ROTATE_270`。
* `PageSource(page_id, width, height)` — 原始扫描页（溯源叶子）。
* `CanvasSource(canvas_id, width, height)` — 中间/成品画布，可被继续拼版。
* `BlankSource(width, height, blank_id="blank")` — 有尺寸的空白表面；
  空白步骤按绘制次序遮盖更早内容，但不产生任何溯源片段。
* `Step(step_id, target, source, source_rect, transform=IDENTITY,
  factor=None, target_x=0, target_y=0, draw_order=0)` — 一次绘制操作，
  记录来源矩形、变换、目标位置与绘制次序。空白步骤 `source_rect=None`，
  且只允许 `IDENTITY`。
* `Fragment(page_id, page_rect, target_rect, step_path)` — 一条可见溯源
  结果：来源页、来源页坐标系中的半开矩形、成品坐标系中的可见矩形、以及
  从成品回溯到原始页的步骤 ID 路径（靠近成品的步骤在前）。

### 模型

* `ProvenanceModel()`
  * `add_page(page)` / `add_canvas(canvas)` — 注册表面，ID 全局唯一。
  * `add_step(step)` — 登记步骤并做结构校验（来源/目标已注册、来源矩形
    不越界、脚印与画布有交集、同一画布 `draw_order` 构成全序、画布引用
    无环、缩放因子与整除性合法等）。
  * `query(canvas_id, selection)` → `list[Fragment]` — 核心溯源入口。
    选区允许越界（越界部分安静裁掉），结果按目标位置排序；被后画内容
    （含空白）覆盖的区域不出现；同一页重复印刷的不同出现位置分别保留。
  * `steps_on(canvas_id)` — 画布上按绘制次序升序的步骤元组。
  * `footprint_of(step_id)` — 某步骤在目标画布上的结果矩形（含可能被
    画布裁掉的越界部分）。

### 矩形代数与合并

* `rect_intersect(a, b)` — 半开交集，不相交返回 `None`。
* `rect_subtract(a, b)` — 半开差集，返回最多 4 个互不相交矩形。
* `merge_fragments(fragments)` — 合并同一 `(page_id, step_path)` 下、
  在目标坐标系与来源坐标系中都相邻的分片，避免同一来源的连续可见区域
  被覆盖切割后无谓拆开；合并保持面积守恒且来源/目标矩形仍严格对应。

## 使用示例

```python
from provenance import (
    ProvenanceModel, PageSource, CanvasSource, BlankSource,
    Step, Transform, Rect,
)

model = ProvenanceModel()
model.add_page(PageSource("p1", 16, 16))
model.add_canvas(CanvasSource("final", 40, 40))

# 16x16 页面缩小 2 倍（factor=-2）放到成品 (0,0)，脚印 8x8
model.add_step(Step(
    "shrink", "final", model.pages["p1"], Rect(0, 0, 16, 16),
    transform=Transform.SCALE, factor=-2, draw_order=0,
))
# 后画一张 4x4 白纸盖住 (2,2)
model.add_step(Step(
    "erase", "final", BlankSource(4, 4), None,
    target_x=2, target_y=2, draw_order=1,
))

# 查目标单格 (0,0)-[1,1)：返回完整的 2x2 源范围，而非中心单像素
model.query("final", Rect(0, 0, 1, 1))
# [Fragment(page_id='p1', page_rect=Rect(0,0,2,2),
#           target_rect=Rect(0,0,1,1), step_path=('shrink',))]

# 白纸区域查不到任何来源
model.query("final", Rect(2, 2, 6, 6))   # []
```

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

隔离模式（忽略环境变量与用户站点包，推荐用于复核）：

```bash
python3 -I -m unittest discover -s tests -v
```

## 验证范围

测试全部基于本地合成数据，不联网、不读取外部素材、不安装依赖。

| 测试文件                | 覆盖内容                                                                 |
| ----------------------- | ------------------------------------------------------------------------ |
| `tests/test_provenance.py` | 矩形代数与半开边界；正常拼版与选区裁切；绘制次序、交错覆盖、空白覆盖（含跨画布与越界白纸）；重复印刷保留不同出现位置；放大/缩小逆向（缩小完整来源范围、非零来源原点、整除校验）；90/180/270° 脚印与角点映射、整圈往返；递归画布拼版与中间层覆盖；面积守恒与不重不漏；全部错误路径校验（重复 ID、越界、环、次序冲突、缩放因子、空白约束等）。 |
| `tests/test_end_to_end.py` | 一条完整合成流水线：原始页 → 缩小/旋转/白纸盖角的中间画布 → 重复印刷画布 → 成品 180° 重拼 + 再覆盖；端到端验证缩小完整范围穿透多层变换、重复印刷位置区分、半开边界、空白穿透多层后仍阻断溯源、独立对照模型下的面积守恒、越界选区裁切。 |
| `tests/test_properties.py` | 固定种子随机性质测试（仅标准库 `random`）：每种变换数百组随机矩形的正向投影↔逆向映射互逆；缩小逆映射宽高恒为目标格的 `k` 倍且落在网格上；300 组随机交错覆盖场景下，查询分片两两不重叠、均在选区内、可见面积与独立覆盖模拟完全一致。 |

当前共 58 个测试用例，全部通过（含普通模式与 `python3 -I` 隔离模式）。

## 设计边界（明确不做的事）

* 只实现题面规定的防御性坐标几何与溯源判断；不生成、不执行任何利用/攻击载荷。
* 每步仅支持单一变换（缩放或一种四分之一转），复合变换通过多步/多画布串联表达。
* 缩小要求来源宽高可被聚合因子整除；非整除场景在 `add_step` 阶段直接拒绝，
  而不是静默猜测边缘像素归属。
* 空白只支持平移贴入，不支持旋转/缩放；空白是可见的遮盖物，不产生溯源片段。
