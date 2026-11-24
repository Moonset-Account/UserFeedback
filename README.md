# 量化校准选择模块（quant_calibration）

端侧模型中若干**连续通道**可以共用一份量化尺度，从而减少每个组的尺度头
（scale header）加载开销。本模块输入每个通道的小型激活值频数表、设备支持的
有符号位宽与尺度、轴上允许的分组长度、每通道元素数、组头字节数与对齐粒度，
按**最近值量化 + 饱和截断**计算各通道的频数加权重建平方误差，选出**各通道
误差均达标、占用字节最少**的连续分组与每组尺度配置。

纯标准库实现，无第三方依赖；全部计算在本地完成，不读取凭据、不联网、不生成
或执行任何载荷。

## 量化与误差模型

对有符号位宽 `b`、尺度 `s`（`QuantSpec(bits=b, scale=s)`）：

- 量化级别区间：`qmin = -2^(b-1)`，`qmax = 2^(b-1)-1`
- 最近值量化（半值远离零，half-away-from-zero）后饱和截断：
  `q = clamp(round(x/s), qmin, qmax)`
- 重建：`x_hat = q * s`
- 单通道频数加权重建平方误差：
  `sse = Σ count(v) * (x_hat(v) - v)^2`
- 达标判据用每通道均方误差：`mse = sse / element_count`，要求
  `mse <= tolerance_mse`（边界相等视为达标）
- 同时返回饱和元素数 `saturated_count` 与最大绝对误差 `max_abs_error` 作为依据

一个连续通道组共用一份 `QuantSpec`；组仅当**所有成员通道** MSE 都达标时才
合法。每组占用字节：

```
align_up(header_bytes + Σ(channel_elements) * bits/8, alignment)
```

## 公开 API

```python
from quant_calibration import (
    QuantSpec,            # (bits: int, scale: float)，含 qmin/qmax
    ChannelError,         # 单通道误差：sse/mse/saturated_count/max_abs_error ...
    GroupSelection,       # 选中的组：channels/bits/scale/payload_bytes/total_bytes ...
    CalibrationResult,    # 总结果：feasible/groups/channel_errors/total_bytes/
                          # violating_channels/max_mse/considered_specs
    evaluate_channel,     # 直接评估单通道在某规格下的误差
    quantize_dequantize,  # 对一组值做量化-反量化，便于审计单点重建
    select_calibration,   # 主入口：分组 + 尺度选择
)
```

### `select_calibration(...)` 参数

| 参数 | 说明 |
| --- | --- |
| `histograms` | 每通道一个频数表：`{激活值: 频数}` 或 `[(值, 频数), ...]`；频数之和必须等于对应的 `element_counts` |
| `element_counts` | 每通道元素数（正整数） |
| `supported_bits` | 设备支持的有符号位宽，仅接受字节对齐宽度（如 `[8, 16]`） |
| `supported_scales` | 设备支持的正尺度列表，与位宽做笛卡尔积并去重 |
| `tolerance_mse` | 每通道可接受的最大重建 MSE |
| `header_bytes` | 每组尺度/元数据头字节数 |
| `alignment` | 每组存储对齐粒度（字节） |
| `groupable_ranges` | 允许的连续组长度（通道数），如 `[1, 2, 4]`；省略时取 `1..max_group_size` |
| `max_group_size` | 省略 `groupable_ranges` 时的便捷上限，默认整条轴 |

### 返回与不可行情况

- 存在全达标布局时：`CalibrationResult.feasible=True`，在所有合法连续划分 ×
  规格组合中返回总字节最少者（字节相同则组数更少、MSE 更低者优先，结果确定）。
- 没有任何布局全达标时：`feasible=False`，仍返回**违约最小**的尽力布局
  （先最小化最差通道 MSE 违约量，再比字节），并通过 `violating_channels`
  与每通道 `ChannelError` 给出完整误差依据，供调用方决定放宽容差或增加位宽。
- 允许的组长度无法铺满整条轴（如 3 通道只允许长度 2）时抛出 `ValueError`。

### 极端值场景

当某通道的极端激活值在共享尺度下饱和、把邻近通道组的误差顶出容差时，优化器
会自动改分组：把极端通道隔离到自己的组（必要时使用更宽位宽），普通通道仍
保留廉价的窄位宽组，而不是把整条轴都加宽。`tests/test_grouping_scenarios.py`
中的 `test_extreme_channel_is_isolated_with_a_wider_width` 与交错场景
`test_alternating_extreme_channels_each_get_isolated` 对此有实测断言。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

环境：Python 3.11.6，仅标准库，无需安装任何依赖。

## 验证范围（本地合成数据）

共 38 个 unittest 用例，全部通过。所有输入均为测试内构造的合成频数表：

- `tests/test_quantize.py`（11 个）
  - 最近值量化与半值远离零（含负数、精确半值、非银行家舍入）
  - int8/int16 有符号上下限的饱和截断
  - 频数加权 SSE/MSE 的手算数值对照、饱和计数与最大绝对误差
  - MSE 对元素数归一化、浮点键频数表、频数不匹配/负频数拒绝
- `tests/test_byte_accounting.py`（7 个）
  - 组字节 = 头 + 载荷再向上对齐的手算对照、每组独立计填充
  - 共享尺度节省组头、8/16 位宽成本选择、允许组长度约束下的划分形状
  - 重复规格去重、`channel_group_map` 辅助方法
- `tests/test_grouping_scenarios.py`（15 个）
  - 极端值共享尺度超差（手算 SSE=1332.25、MSE=13.3225、饱和 1 个元素）
  - 隔离极端通道的改分组结果（448B）与整体加宽 16 位（624B）的成本对比
  - 容差边界相等达标 / 边界下方立即拆分
  - 两个极端通道交错出现的隔离布局、强制成对分组的更贵布局对照
  - 单通道轴、全零频数表、重复调用确定性
  - 非字节对齐位宽、非正尺度、非法对齐/组头、空轴、元素数不匹配、
    超长 span、负容差等参数校验
- `tests/test_exhaustive_optimality.py`（5 个）
  - 独立实现的暴力枚举（所有连续划分 × 每组所有规格）与动态规划结果在
    总字节、组数、最差 MSE、可行标志上逐项一致，覆盖可行与不可行轴、
    受限 span、零容差精确数据等情形

### 已知边界与限制

- 仅支持字节对齐位宽（8/16/…）；子字节打包不在范围内，传入即报错。
- 频数表必须完整覆盖 `element_count`，不做隐式重加权。
- 组间独立对齐；不实现整轴级别的统一对齐填充。
- 误差判据为每通道 MSE（与通道大小无关）；原始 SSE 同时在 `ChannelError`
  中返回，调用方可自行换算其他判据。
