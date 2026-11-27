# 整数卷积余数重建器（广义 CRT）

独立工作者分别在若干模数上计算卷积，内存不足以保存展开系数时，本重建器只合并
各工作者提交的**余数块**，按位置逐步恢复精确的整数卷积系数。模数之间**不要求
互素**：合并基于广义中国剩余定理（CRT），有效信息量按最小公倍数（lcm）和
素数幂归并计算，同一因子不会被重复计数。

## 数学规则

设两个输入序列长度均为 `n`，逐项绝对值界为 `|a_i| <= A`、`|b_j| <= B`。

1. **位置相关的系数界。** 输出位置 `k` 的系数是
   `c_k = sum_{i+j=k} a_i b_j`，参与求和的项数为
   `t(k) = #{(i,j): i+j=k, 0<=i,j<n}`（先增后减的三角形，边缘为 1，中心为 n），
   因此 `|c_k| <= B_k = t(k) * A * B`。
2. **广义 CRT 合并。** 新约束 `x ≡ r2 (mod m2)` 与已有约束
   `x ≡ r1 (mod m1)` 相容当且仅当 `r1 ≡ r2 (mod gcd(m1,m2))`；相容时合并为
   模 `lcm(m1,m2)` 的单一余数，不相容则该块整体拒绝。
3. **因子不重复计数。** 每个模数先做素因子分解，某位置的有效模数是各素因子
   **最高素数幂**的乘积（即 lcm），不是模数乘积。较弱或等强的重复素数幂在
   `BlockReport.duplicated_factor_positions` 中显式标出，不贡献新信息。
4. **唯一可发布条件。** 当且仅当有效模数 `M_k > 2 * B_k` 时，区间 `[-B_k, B_k]`
   内恰有一个整数满足全部同余约束，此时才能提前发布该位置。`M_k == 2*B_k`
   时 `B_k` 与 `-B_k` 同余，仍然不可发布（测试中专门验证）。
5. **冲突定位与原子性。** 迟到块先做全量 dry-run 校验，任何位置冲突都抛
   `CongruenceConflict`，其中每个 `ConflictRecord` 定位到具体模数、位置、
   新余数、旧余数与 gcd；冲突块不写入任何状态。

全部整数运算使用 Python 任意精度整数，系数超过 64 位时不会溢出。

## 公开 API

包 `convolution_reconstruct`（实现位于 `convolution_reconstruct/reconstruct.py`）：

### 重建器

- `ConvolutionReconstructor(n, abs_bound_a, abs_bound_b, output_length=None)`
  - `add_block(modulus, residues, positions=None) -> BlockReport`
    `residues` 可以是完整序列、`{位置: 余数}` 映射，或与 `positions` 等长的
    部分序列；余数接受 `[0, m)` 或带符号表示。
  - `get_value(k)` / `is_determined(k)` / `ready_positions()`
  - `publish_ready() -> {位置: 系数}`（发布后置标记，不重复发布）
  - `snapshot() -> {位置: 系数}`（只读，不改发布标记）
  - `state(k) -> PositionState`、`states()`、`bounds`、`effective_modulus(k)`
  - `accepted_moduli()`、`factor_report()`（每个素因子的最强供应模数）
- `reconstruct_convolution(n, A, B, blocks) -> list[int | None]`：一次性包装函数。

### 数据类型与异常

- `PositionState`：`position, bound, residue, effective_modulus, factors,
  factor_owners, source_moduli, published`，方法 `is_determined()`、`value()`。
- `BlockReport`：`modulus, applied_positions, new_factor_positions,
  duplicated_factor_positions, newly_determined_positions, conflicts`。
- `ConflictRecord`：`position, modulus, incoming_residue, existing_residue,
  existing_modulus, gcd, difference`。
- `CongruenceConflict(conflicts)`：异常，`.conflicts` 给出本块全部冲突位置。

### 纯函数

- `term_count(k, n)`、`coefficient_bound(k, n, A, B)`、`coefficient_bounds(n, A, B)`
- `direct_convolution(a, b)`：O(n²) 朴素整数卷积，测试中的真值参照
- `factorize(m)`、`prime_power_lcm(factors)`
- `merge_congruence(r1, m1, r2, m2) -> (r, lcm) | None`
- `signed_residue(r, m)`：把 `[0, m)` 余数提升到 `(-m/2, m/2]`

## 运行方法

```bash
# 标准命令（题目要求）
python3 -m unittest discover -s tests -v

# 隔离模式（-I 不把 cwd 加入 sys.path，需显式指定顶级目录）
python3 -I -m unittest discover -s tests -t . -v
```

无第三方依赖，仅使用 Python 3.11 标准库。

## 验证范围（45 个测试，全部实测通过）

`tests/test_primitives.py`
- 项数三角形与逐位置系数界（含零界、非法参数）
- 素因子分解与 lcm 往返
- 广义 CRT：互素、非互素相容、非互素冲突返回 `None`、整除关系、模数 1、
  多模数链式合并
- 带符号余数提升（含偶数模数的正半边界）

`tests/test_reconstruct_small.py`
- 小整数带负数卷积逐项核对，模数族 `6,10,15,7` 大量共享因子
- 有效模数是 lcm 而非乘积（`6,10` → 30 而非 60；`15` 零新增信息）
- 40 组随机长度/界/模数顺序的交叉核对，发布前每个值都与真值比较
- 带符号余数输入、部分块/迟到块、包装函数
- 冲突定位（模数+位置+gcd）、冲突块原子性、多冲突一次报全、共享因子相容不误报
- 发布顺序：边缘位置界最小先发布；`M=3` 只够边缘、`lcm=15` 后内部发布
- 模数 1 在零界下可发布、非零界下无信息；构造参数校验

`tests/test_bigint_and_bounds.py`
- `n=16`、元素界 `10^12` 的合成序列，最大系数 65 位
  （`19112635990802643646 > 2^63-1`，6 个位置超出有符号 64 位）
- 三个约 1e9 的互素大素数（lcm 约 90 位）精确重建全部 31 个系数，含负值
- 单模数/双模数信息量不足时全部不可发布，第三块后全部发布
- 声明界对实际系数成立且形状与项数三角形一致
- 边界条件 `M == 2B` 不可发布（`B` 与 `-B` 同余），`M = 2B+1` 才可发布
- 显式展示 `M <= 2B` 时区间内存在两个同余整数；负系数的带符号还原

`tests/test_interleaving.py`
- 每个模数块拆成任意两个位置子集、乱序交错到达后完整重建
- 重复相同块只产生重复因子、有效模数不变
- 已有进展后迟到块冲突 → 定位、原子拒绝 → 改正后重新提交成功
- 交错过程中 lcm 增长链 `12 → 36 → 180 → 180`，新素因子与重复素因子逐项核对
- 素因子 owner 随更强素数幂升级（因子 2 归 4、因子 3 归 9）
- 非法块形状/类型拒绝、截断输出长度

## 设计边界

- 面向**重建**问题：输入只有界与各模数余数，不实现 NTT/卷积计算本身。
- `factorize` 为朴素试除，适配测试中 <= ~1e9 的模数；工程上换用成熟分解库即可，
  合并与发布逻辑不依赖分解实现。
- 状态按输出位置保存，空间 O(输出长度)，不展开任何工作者的完整系数矩阵。
