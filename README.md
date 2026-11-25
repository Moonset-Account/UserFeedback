# DECIMAL/BCD 到定宽有符号整数的批量转换内核

本包只用 Python 标准库实现数据库页列搬运中的一类转换：

- 源列：带符号的压缩十进制 / packed BCD（IBM COMP-3 风格布局）。
- 源元数据：`precision`（BCD 数字总数）和 `scale`（小数位）。
- NULL：由独立位图表示，不依赖载荷。
- 目标列：定宽、二进制补码有符号整数，按目标 `scale` 解释。
- 舍入：仅用整数运算直接完成最近偶数舍入（round half to even）。
- 错误策略：严格模式整批拒绝；宽松模式错误行置 NULL 并记录行/半字节位置。

运行环境已核对为 Python 3.11.6；不需要安装第三方依赖。

## 运行测试

在工作区根目录执行题面要求的命令：

```bash
python3 -m unittest discover -s tests -v
```

最近一次实际执行结果为 `Ran 36 tests ... OK`。

## 公开 API

主要入口在 `decimal_kernel` 包顶层导出：

```python
from decimal_kernel import (
    SourceColumn,
    TargetColumn,
    PackedDecimalBatchConverter,
    convert_packed_batch,
    convert_packed_page,
    NullBitmap,
    BatchResult,
    RowError,
    ErrorCode,
    ErrorMode,
    BatchConversionError,
)
```

### `convert_packed_batch(...)`

```python
result = convert_packed_batch(
    payloads,                 # Sequence[bytes-like]，每行一个定长 packed BCD 槽
    precision=18,             # 源 DECIMAL 总数字位数
    source_scale=4,           # 源小数位
    target_scale=2,           # 目标整数的隐含小数位
    target_width=8,            # 目标字节宽度，默认 8
    target_precision=18,       # 可选：目标 DECIMAL 总精度，用于十进制精度溢出判断
    byte_order="big",          # "big" 或 "little"
    null_bitmap=bitmap_bytes,  # bytes/bytearray/memoryview、NullBitmap、bool 序列或 None
    bitmap_bit_order="msb",    # 原始位图字节中的位序："msb" 或 "lsb"
    on_error="lenient",        # "strict" 或 "lenient"
    accept_unsigned_sign=False,# 为 True 时把符号半字节 F 也视为正数
)
```

返回的 `BatchResult` 包含：

- `values: tuple[int | None, ...]`：转换后的 Python 整数；NULL/错误行为 `None`。
- `slots: tuple[bytes | None, ...]`：定宽补码字节；NULL/错误行为 `None`。
- `errors: tuple[RowError, ...]`：宽松模式下的逐行错误。
- `null_bitmap: NullBitmap`：结果位图；宽松模式会把错误行也置为 NULL。
- `contiguous_target_bytes(null_fill=None)`：拼接所有目标槽，NULL 槽填零或指定字节。

严格模式只要存在数据错误，就收集全部行错误并抛出
`BatchConversionError`；异常对象有 `errors`、`rows` 和 `first_error`，不会返回
部分 `BatchResult`。

也可以使用绑定列定义的有状态内核：

```python
kernel = PackedDecimalBatchConverter(
    precision=4,
    source_scale=3,
    target_scale=0,
    target_width=2,
    target_precision=1,
)
result = kernel.convert(payloads, null_bitmap=bitmap, mode="lenient")
```

如果源数据是连续页缓冲、每行按 packed BCD 宽度定长排列，可使用
`convert_packed_page(page, row_count=..., ...)`。它内部仅在访问非 NULL 行时切片。

## BCD 与位图约定

### Packed DECIMAL

宽度为：

```text
packed_width = precision // 2 + 1
```

半字节顺序为从高到低：

- 最后一个半字节是符号：`C` = 正，`D` = 负。
- 数字半字节必须为 `0..9`。
- 当 `precision` 为偶数时，第一个半字节是前导填充，规范值为 `0`。
- `F` 符号默认按非法符号拒绝；设置 `accept_unsigned_sign=True` 时按正数接受。
- `D` 加全零数字，或负值舍入后为零时，统一归一为整数 `0`，目标字节为全零。

例如 `DECIMAL(4,3)` 的 `+1.499` 是：

```text
0 1 4 9 9 C  ->  01 49 9C
```

### NULL 位图

- 默认 `bitmap_bit_order="msb"`：第一个 NULL 位是首字节的最高位。
- 也支持 `"lsb"`：第一个 NULL 位是首字节的最低位。
- 可直接传 `NullBitmap.from_bytes(...)` 或 `NullBitmap.from_bools(...)`。
- 原始位图只需包含 `ceil(row_count / 8)` 字节；最后一个字节未使用的位会被忽略，
  重新序列化时写零。

转换循环先判断位图并立即 `continue`，之后才执行 `payloads[row]`、`len(payload)`
或任何 BCD 半字节读取。测试用“访问 NULL 行就抛 `AssertionError`”的序列和填充
`EE/FF` 垃圾字节的连续页双重核对了这一点。

## 精确舍入与溢出判断

源尾数 `M` 表示的值是 `M * 10**(-source_scale)`，目标整数 `Q` 表示
`Q * 10**(-target_scale)`。

- 若 `target_scale >= source_scale`：直接乘 `10**(target_scale-source_scale)`，
  不做舍入。
- 若 `target_scale < source_scale`：令
  `factor = 10**(source_scale-target_scale)`，执行一次：

```text
kept, remainder = divmod(abs(M), factor)
2 * remainder > factor       -> 进位
2 * remainder == factor      -> 仅当 kept 为奇数时进位（最近偶数）
```

完整余数一次参与比较，因此不会先舍入到中间小数位再第二次舍入，也不经过二进制
`float`。负数按绝对值完成相同决策后恢复符号；结果为零时返回正零。

溢出在舍入/缩放完成之后判断：

1. 若提供 `target_precision`，检查 `abs(Q) <= 10**target_precision - 1`；失败为
   `ErrorCode.PRECISION_OVERFLOW`，`stage="after_rounding"` 或
   `"after_scale_shift"`。
2. 再检查目标字节宽度的二进制补码范围；失败为
   `ErrorCode.FIXED_WIDTH_OVERFLOW`。

错误码还区分：

| `ErrorCode` | 含义 |
| --- | --- |
| `INVALID_PAYLOAD_LENGTH` | packed BCD 槽长度不符合 precision |
| `INVALID_PAYLOAD` | 槽不可读取或不可按字节索引 |
| `INVALID_DIGIT_NIBBLE` | 数字/填充半字节不是 `0..9` |
| `INVALID_SIGN_NIBBLE` | 符号半字节不是允许的 `C/D`（或启用后的 `F`） |
| `PRECISION_OVERFLOW` | 源前导填充已有额外数字，或目标精度在舍入进位/缩放后溢出 |
| `FIXED_WIDTH_OVERFLOW` | 整数超出目标字节宽度的补码范围 |

`RowError` 记录 `row`，并尽可能附带 `byte_offset`、`nibble_index`、
`bad_nibble`、`rounded_value`、`limit` 和 `stage`。

## 中点两侧正负样本与错误行

测试 `tests/test_midpoint_sample.py` 使用以下列定义：

- 源：`DECIMAL(4,3)`，packed width = 3。
- 目标：2 字节有符号大端整数，`target_scale=0`，`target_precision=1`。
- 位图：MSB 优先，原始字节 `80 20`，表示第 0、10 行为 NULL。

源 scale 为 3，因此整数 1 和 2 的中点是精确的 `1.500`；该精度下紧邻中点两侧的
可表示值是 `1.499` 和 `1.501`。

| 行 | 源字节 | 源值 | 目标整数 | 目标字节 | 核对点 |
| ---: | --- | --- | ---: | --- | --- |
| 0 | 不读取 | NULL | `None` | `None` | 载荷间谍确认未索引 |
| 1 | `01 49 9C` | `+1.499` | `1` | `00 01` | 中点下方，不进位 |
| 2 | `01 50 0C` | `+1.500` | `2` | `00 02` | 精确中点，1 为奇数，进到偶数 2 |
| 3 | `01 50 1C` | `+1.501` | `2` | `00 02` | 中点上方，进位 |
| 4 | `01 49 9D` | `-1.499` | `-1` | `FF FF` | 负样本中点下方 |
| 5 | `01 50 0D` | `-1.500` | `-2` | `FF FE` | 负精确中点，归到偶数 -2 |
| 6 | `01 50 1D` | `-1.501` | `-2` | `FF FE` | 负样本中点上方 |
| 7 | `01 A0 0C` | — | `None` | `None` | `INVALID_DIGIT_NIBBLE`；`byte_offset=1`, `nibble_index=2`, `bad_nibble=0xA` |
| 8 | `01 50 0E` | — | `None` | `None` | `INVALID_SIGN_NIBBLE`；`byte_offset=2`, `nibble_index=5`, `bad_nibble=0xE` |
| 9 | `09 99 9C` | `+9.999` | 舍入为 `10` | `None` | `PRECISION_OVERFLOW`；`rounded_value=10`, `limit=9` |
| 10 | 不读取 | NULL | `None` | `None` | 载荷间谍确认未索引 |

宽松模式实测：

```text
values      = (None, 1, 2, 2, -1, -2, -2, None, None, None, None)
error rows  = [7, 8, 9]
error codes = [invalid_digit_nibble, invalid_sign_nibble, precision_overflow]
target map  = 81 E0   # 源 NULL 行 0/10，加上错误行 7/8/9
```

严格模式对同一批次抛出 `BatchConversionError`，其 `rows == (7, 8, 9)`，整批不返回
结果对象。

另有一个避免二次舍入的行为样本：`1.499` 直接舍入到整数应为 `1`；若先舍入到两位
小数会变成 `1.50`，再次最近偶数舍入则会错误地变成 `2`。测试同时断言了这条错误
的两阶段路径，以证明本实现没有采用它。

## 验证范围

`tests/` 中的 36 个用例实际覆盖：

- MSB/LSB 位图、跨字节交错位、截断/长度不匹配和结果位图。
- 奇/偶 precision 的 packed BCD 布局、正负号、可选 `F` 符号。
- 非法数字半字节、非法符号、载荷长度错误及精确位置字段。
- 精确中点、中点两侧、长余数一次决策、负数和负零归一。
- 避免浮点和二次舍入的行为断言，以及大于 `2**53` 的整数不丢失。
- 目标 scale 扩展、大小端、1 字节有符号整数边界 `-128/127` 和越界 `-129/128`。
- 舍入后进位导致的目标 precision 溢出，包括 `99.50 -> 100` 的 tie/carry。
- 严格模式整批拒绝、宽松模式逐行 NULL 和错误行集合。
- NULL 与有效/错误行交错；通过访问间谍和连续页垃圾载荷确认不读 NULL 槽。
- 非 NULL 槽不可读取时返回 `INVALID_PAYLOAD` 行错误，而不是绕过错误策略。
- 空批次和连续数据库页适配器。

所有数据均为本地合成字节，没有联网、访问真实数据库目标、读取凭据或安装依赖。
未运行覆盖率/性能工具，因此 README 不声称具体行覆盖率或性能基准。
