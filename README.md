# 受限公共区间差分编码器

输入旧底包字节、**公共区间**（授权可被更新包引用的字节范围）和已批准公开的目标字节，
生成**总成本最低、可逐字节重建目标**的更新指令序列。

## 指令与成本模型

| 指令 | 含义 | 成本 |
| --- | --- | --- |
| `CopyOp(offset, length)` | 从旧底包 `offset` 起复制 `length` 字节 | 固定 `copy_cost`，与长度无关 |
| `LiteralOp(data)` | 直接写入目标字面量 `data` | `literal_head + len(data) * literal_byte` |

三个成本全部由调用方提供（`literal_byte` 默认 `1`），允许零值与小数。

## 授权约束（核心语义）

1. **单区间完整包含**：每条 copy 的源区间 `[offset, offset+length)` 必须完整落在
   **同一个**公共区间内；区间边界上的长匹配会被截短，或改由相邻 copy + literal 拆分。
2. **只引用公共实例**：同一内容同时出现在受限段与公共段时，只允许引用公共段的偏移；
   受限段独有的字节只能走 literal。
3. **重叠区间拒绝、相邻区间不合并**：重叠区间在输入校验时直接报错（合并会隐式放行
   跨边界 span）；相邻但不重叠的区间保持独立，边界仍然有效。
4. copy 只能引用**旧底包**，不能引用目标自身（无后向引用），属于经典单边 delta 编码。

## 算法

- 对目标位置 `i` 与底包偏移 `o` 用一次反向 DP 求最长公共前缀，时间 `O(n*m)`、
  额外空间 `O(m)`；LCP 按公共区间末端封顶，区间外偏移长度强制为 0，
  一次性落实约束 1、2。
- 在目标位置 `0..m` 上做最短路 DP：literal 转移用滑动最小值 `O(m)`；
  copy 成本与长度无关，每个位置只需最长匹配，区间松弛用最小堆 `O(m log m)`。
- 输出经回溯重建的**全局最优**指令序列（非贪心），等成本时确定性地偏向 literal。
- 总复杂度 `O(n*m + m log m)`，实测 2000×2000 字节约 0.13 秒（本机 Python 3.11.6）。

## 实际入口

### Python API

```python
from diffencoder import encode, decode

plan = encode(
    source=b"SECRETpublic",
    public_intervals=[(6, 12)],          # 半开区间 [start, end)
    target=b"public",
    copy_cost=3,
    literal_head=2,
    literal_byte=1,
)
assert decode(plan, source=b"SECRETpublic", public_intervals=[(6, 12)]) == b"public"
```

`encode(...)` 返回不可变 `Plan`（含 `ops`、`cost`、成本参数与 JSON 序列化方法）；
`decode(plan, source, public_intervals)` 会重新校验每条 copy 的授权合法性，
越界 / 跨区间 / 长度不符均抛 `ValueError`。

### 命令行

```bash
# 编码（--interval 可重复；区间写法 START:END，支持 0x 前缀）
python diffencoder/__main__.py encode \
    --source base.bin --target target.bin \
    --interval 0:128 --interval 256:512 \
    --copy-cost 5 --literal-head 2 --literal-byte 1 \
    --out plan.json

# 解码并校验
python diffencoder/__main__.py decode \
    --source base.bin --plan plan.json --out rebuilt.bin
```

包模式 `python -m diffencoder ...` 同样可用（需要正常 `sys.path` 包含仓库根目录）。

## 目录结构

```
diffencoder/
  __init__.py     # 公共 API 导出
  encoder.py      # 区间校验、LCP DP、最优解析、校验式解码
  cli.py          # argparse 命令行
  __main__.py     # 入口（兼容 -m 与直接脚本执行，含 -I 隔离场景）
tests/
  test_encoder.py            # 验收用例（含真实 CLI 子进程）
  test_fuzz_optimality.py    # 独立暴力 DP 随机对拍
```

## 验证范围与运行方式

```bash
python3 -I -m unittest discover -s . -p "test_*.py" -v
```

`-I` 隔离模式下从仓库根目录运行 discover 即可，无需安装任何第三方依赖。

### 交叉约束覆盖（每类均为真实断言）

- **可接受结果**：完整落在公共区间内的匹配输出为单条 copy，成本等于固定 copy 成本，
  解码逐字节还原（`test_acceptable_full_public_copy`）。
- **应被阻止的结果**：
  - 内容仅存在于受限段时，编码结果不允许出现任何 copy，只能 literal
    （`test_restricted_only_content_is_blocked_from_copy`）；
  - 手工构造的越权 copy 计划、越底包末端 copy、长度篡改计划，被校验式解码拒绝
    （`test_decoder_rejects_unauthorized_copy`、`test_decoder_detects_length_mismatch`）。
- **影响两者的边界 / 交错场景**：
  - 跨授权边界匹配被截短为 copy + literal（`test_match_crossing_boundary_is_shortened`）；
  - 相邻区间边界仍生效，copy 最长不超过单个区间（`test_adjacent_intervals_still_enforce_the_boundary`）；
  - 重复内容只取公共实例（`test_duplicate_content_uses_public_instance_only`）；
  - 多个不相交区间与 literal 交错，所有 copy 均限定在授权岛内
    （`test_interleaved_matches_across_multiple_intervals`）；
  - 成本档位切换改变最优解析（copy 便宜走 copy、literal 便宜走 literal），
    零成本 / 小数成本、空输入、非法区间与非法成本参数均有覆盖。
- **最优性对拍**：60 组固定随机种子夹具（底包最长 12、目标最长 14、0–3 个区间、
  多组成本参数），与独立编写的 `O(m²n)` 暴力 DP 逐例比较最小成本，
  并同时断言重建正确、copy 全部合法、报告成本与逐指令重算一致
  （`test_random_fixtures_match_brute_force`）。
