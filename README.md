# trajectory-publish — 保密时段感知的轨迹发布库

受管终端向维修方发布合成行程前，先经过本库消毒：凡是可能位置（采样点
± 方形误差半径）触及"当时生效"的保密矩形的采样或连线，一律隐藏或
切断，且绝不引入插值点、绝不把删除采样两侧重新连接。

## 运行环境

- Python 3.11.6（开发核验使用工作区内 `.venv`，同为 3.11.6）
- 仅使用标准库，无第三方依赖

## 实际入口

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| `Sample` / `SecretRegion` / `PublishResult` / `sanitize_trajectory` | `trajectory_publish/__init__.py`（实现于 `trajectory_publish/core.py`） | 库公开 API |
| 验收测试 | `tests/test_trajectory_publish.py` | `unittest`，27 个用例，全部真实断言 |

### 数据模型

- `Sample(t, x, y, radius)`：带时间戳的平面采样；`radius` 为方形误差
  半径，可能位置为 `[x-r, x+r] × [y-r, y+r]`。`radius < 0` 或 NaN 直接
  抛 `ValueError`。
- `SecretRegion(x_min, y_min, x_max, y_max, t_start, t_end)`：轴对齐保密
  矩形，仅在**闭区间** `[t_start, t_end]` 内生效。坐标或时间区间倒置
  抛 `ValueError`。
- 相邻采样之间，坐标与半径均按时间**线性插值**；采样时间必须严格
  递增，否则 `sanitize_trajectory` 抛 `ValueError`。

### 消毒规则（`sanitize_trajectory(samples, regions) -> PublishResult`）

1. 某采样时刻其误差方形触及任一当时生效的矩形（含边缘相切）→
   隐藏该采样，记入 `hidden_samples`。
2. 某连线的插值方形在生效时段内任一瞬时触及矩形 → 切断该连线。
3. 输出只含**原始输入采样**：不生成标出保密区边界的插值点；被删
   采样两侧不重新连线。
4. 返回 `PublishResult`：
   - `samples`：保留的原始采样（输入顺序）；
   - `segments`：可发布的相邻采样连线 `(a, b)`；
   - `tracks`：由安全连线组成的最长可展示轨迹段（单点轨迹段表示
     孤立保留点）。两端点安全、仅中途穿过保密区时，返回两段可展示
     轨迹。

### 最小示例

```python
from trajectory_publish import Sample, SecretRegion, sanitize_trajectory

region = SecretRegion(10, 10, 20, 20, 100, 200)  # 闭区间生效
samples = [
    Sample(0,   0.0, 15.0, 0.0),
    Sample(50,  5.0, 15.0, 0.0),
    Sample(250, 25.0, 15.0, 0.0),
    Sample(300, 30.0, 15.0, 0.0),
]
result = sanitize_trajectory(samples, region)
assert result.tracks == ((samples[0], samples[1]), (samples[2], samples[3]))
```

## 运行测试

```bash
cd <workspace>
.venv/bin/python -m unittest discover -s tests -v
```

## 验证范围

`tests/test_trajectory_publish.py` 按题面交叉约束组织：

- **可接受结果**：无保密区 / 矩形远离 / 采样落在矩形生效时段之外 /
  连线在空间上穿越但时段不相交 → 全部保留并连通；空输入安全返回。
- **应被阻止或拒绝的结果**：采样位于生效矩形内、误差方形触及矩形、
  连线在生效时段内穿越矩形 → 隐藏或切断；被删采样两侧不得重连。
- **边界与交错场景**：
  - 闭区间端点 `t_start` / `t_end` 上的采样被隐藏，区间外相邻时刻保留；
  - 误差方形与矩形边缘恰好相切（`x + r == x_min`）视为触及，差
    0.001 则放行；
  - 插值点仅在单一瞬时触及矩形角点 → 切断；y 方向少 0.1 错开时间窗
    → 放行；
  - 连线恰在保密窗口开启瞬时到达矩形 → 切断；窗口开启前结束 → 放行；
  - 端点误差方形均安全、仅中途因半径线性增大而触及 → 切断连线但
    保留两端点；
  - 两个生效时段交错的矩形：只在各自窗口内生效；
  - 两端点安全、中途穿越 → 恰好两段可展示轨迹。
- **输出完整性**：输出中的每个采样都是输入对象本身（无插值点），
  轨迹段只连接原本相邻的采样。
- **输入校验**：负半径、NaN、矩形/时间区间倒置、采样时间非严格
  递增均被拒绝。

## 限制说明

- 触及判定采用闭集语义（相切即触及），与题面"触及"一致。
- 浮点比较为精确比较，未引入容差；边界用例以精确可表示的数值构造。
- 仅本地交付与核验，不联网、不探测任何外部目标。
