# 可电话口述一次性短语：生成与格式核验库

身份恢复服务需要可电话口述的一次性短语。本库在给定**编号词表、同音组、
禁止相邻词对、校验模数与位置权重**下，对长度至多 8 词的短语提供：

- 合法短语的**精确总数**（动态规划计数，不随机拼整句碰运气）；
- 按词编号字典序的**排名**与**反向取词**（rank / unrank 互逆）；
- **等概率**随机抽样（每条合法短语概率严格为 `1/T`）；
- 随机源只通过“按需索取独立随机位”的协议接入；
- 完整的**格式核验**（长度、词编号、同音组、禁连、校验和）；
- 零候选与恰好一条候选均可处理。

仅使用 Python 3.11 标准库，无第三方依赖。

## 目录结构

```
phrase_code/
  __init__.py     公开 API 汇总
  bits.py         独立随机位协议、默认 CSPRNG 源、区间均匀抽样原语
  scheme.py       PhraseScheme 方案定义与格式核验
  enumerator.py   后缀 DP 计数、rank/unrank、均匀抽样
tests/
  test_bits.py        随机位与拒绝采样（含按需取位断言）
  test_scheme.py      方案参数校验与格式核验
  test_enumerator.py  DP vs 独立暴力穷举、rank/unrank、零/单候选、16 词稀疏场景
  test_sampling.py    等概率卡方检验、真实随机源、按需取位
  test_fuzz.py        25 组随机方案的 fuzz 交叉验证
```

## 约束语义（题面落实方式）

| 约束 | 语义 |
| --- | --- |
| 词表 | 1–16 个唯一非空字符串，词编号为 0 起的列表下标 |
| 短语长度 | 1–8（`max_length` 可下调），枚举器按固定长度构造 |
| 同音组 | 每个词至多属于一个组；合法短语中同一组至多出现一次。不在任何组中的词不受此限（可重复） |
| 禁连 | 词对 `(a, b)` 按**有向**邻接解释：连续出现 `a,b` 即非法；反向不连带禁止，如需双向请同时给出 |
| 校验和 | 对长度 L 的短语，前 L-1 个词按 1 起位置计算 `c = (Σ w_pos · id_pos) mod m`；末词必须满足 `id_L mod m == c`。L=1 时前缀为空，`c=0` |
| 权重 | 构造方案时传入，长度需覆盖位置 1..L-1；允许任意整数（负值按模 m 归约） |
| 模数 m | 正整数；`m=1` 时校验约束恒真（`x mod 1 == 0`） |

## 公开 API

### `PhraseScheme`（`phrase_code.scheme`）

```python
PhraseScheme(
    words,                    # Sequence[str]，1..16 个唯一词
    homophone_groups=(),      # Sequence[Sequence[int]]，每组 >=2 个词编号
    forbidden_pairs=(),       # Sequence[tuple[int, int]]，有向禁连
    modulus=10,               # 正整数
    weights=(),               # Sequence[int]，至少 max_length-1 个
    max_length=8,             # 1..8
)
```

方法/属性：

- `scheme.is_valid(ids) -> bool`
- `scheme.violation(ids) -> str`（首个违规原因，合法返回 `""`）
- `scheme.validate(ids) -> None`（非法抛 `PhraseError`）
- `scheme.checksum_of(ids) -> int`
- `scheme.to_words(ids) -> list[str]`
- `scheme.vocab_size`、`scheme.group_of`、`scheme.forbidden`
- `describe_validation(scheme, ids) -> dict`（结构化核验结果）

### `PhraseEnumerator`（`phrase_code.enumerator`）

固定方案与固定长度的枚举器，核心为记忆化后缀 DP，状态是
`(剩余位置, 上一个词, 同音组位掩码, 前缀加权和 mod m)`：

- `enumerator.count() -> int`：合法总数，零候选返回 0；
- `enumerator.enumerate_all() -> list[tuple[int, ...]]`：字典序全部短语（小词表穷举用）；
- `enumerator.rank(ids) -> int`：字典序排名（0 起），非法短语抛 `PhraseError`；
- `enumerator.unrank(rank) -> tuple[int, ...]`：按排名取词，与 `rank` 互逆；
- `enumerator.sample(bit_source) -> tuple[int, ...]`：等概率抽样；
- `enumerator.sample_words(bit_source) -> list[str]`：抽样后直接转词；
- 零候选时 `count()==0`、`enumerate_all()==[]`，抽样/unrank 抛 `NoCandidateError`；
- 恰好一条候选时 `sample` 不消耗任何随机位。

### 随机位（`phrase_code.bits`）

- `BitSource`：协议类型，只需实现 `getbit() -> bool`，每次返回一个独立无偏位；
- `SystemRandomBitSource`：基于 `os.urandom` 的默认安全源，逐位吐出、按需申请；
- `SequenceBitSource(bits)` / `ZeroBitSource` / `LcgBitSource`：测试用确定源；
- `uniform_inside(n, source) -> int`：仅靠独立随机位在 `[0, n)` 上均匀取整。
  取 `k = ceil(log2 n)` 位（高位先取）组成候选，越界则**整组拒绝重抽**，
  接受概率严格均匀；`n=1` 时不取位直接返回 0。

抽样流程：`r = uniform_inside(T, source)` 后 `unrank(r)`。因此每条合法短语
概率恒为 `1/T`，且所有随机位都是 unrank 之前按需逐位索取，不存在预取或浪费。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

无网络、无第三方依赖；生产随机源为 `os.urandom`（CSPRNG）。

在 Python 隔离模式（`-I`，不读环境变量/用户 site 包）下，cwd 不会自动进入
`sys.path`，需显式指定路径后再发现测试：

```bash
python3 -I -c "import sys, unittest; sys.path.insert(0, '.'); \
suite = unittest.TestLoader().discover('tests'); \
raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)"
```

两种模式下均为 51 个测试全部通过。

## 最小示例

```python
from phrase_code import PhraseScheme, PhraseEnumerator, SystemRandomBitSource

scheme = PhraseScheme(
    words=["alpha", "bravo", "charlie", "delta"],
    homophone_groups=((0, 2),),
    forbidden_pairs=((1, 3),),
    modulus=5,
    weights=(3, 1, 4, 1, 5, 9, 2),
)
en = PhraseEnumerator(scheme, length=4)
print(en.count())                 # 精确合法总数（DP）
ids = en.sample(SystemRandomBitSource())
assert scheme.is_valid(ids)       # 抽样结果必过核验
print(scheme.to_words(ids))
assert en.unrank(en.rank(ids)) == ids
```

## 验证范围（实测证据）

`python3 -m unittest discover -s tests -v` 共 51 个测试，全部通过，覆盖：

1. **DP 与独立暴力实现逐词一致**：6 词方案在长度 1..8 全长度上，
   `count` / `enumerate_all` / `rank` / `unrank` 与独立 `itertools.product`
   暴力穷举（独立手写同音组、禁连、校验逻辑）完全一致；
2. **fuzz**：25 组随机词表/同音组/禁连/模数/权重方案，全长度交叉验证；
3. **rank/unrank 互逆**：穷举空间内每个排名与短语双向映射，并校验非法输入异常；
4. **等概率抽样**：长度 1/2/3 各抽 3000 次做卡方拟合优度检验
   （显著性水平 0.001 的临界值），并检查每条短语频率偏离 ≤25%、全覆盖；
5. **按需独立随机位**：`SequenceBitSource` 精确断言消耗量——
   `T` 为 2 的幂时恰好 `log2(T)` 位；拒绝路径整组重抽消耗两组位；
   `T=1` 零位消耗；零候选抽样在触碰随机源之前即抛错；
6. **零候选 / 单候选**：禁连封死（计数 0）与唯一短语（计数 1）两类构造，
   验证 `count`、`enumerate_all`、异常类型与零位抽样；
7. **极稀疏大词表**：16 词、长度 8，封死为仅两条长度-8 链的场景，
   DP 直接给出精确计数 2 / 1 / 0，无需生成任何候选；另在无约束最坏规模下
   实测 8ms 内得到 330,382,100 条的精确总数；
8. **边界**：L=1 的空前缀校验语义、`m=1` 校验恒真、权重参数化、
   词表 1/16 边界、长度 0/9 拒绝、词编号越界、同词属多组拒绝、
   禁连有向性（反向不连带禁止）、自邻接禁止。

## 设计说明与取舍

- **为什么不“随机拼整句再检验”**：稀疏候选下接受率可能趋近于 0，
  拒绝采样整句无法保证终止时间。本库先用后缀 DP 求精确总数与各分支大小，
  抽样只在排名空间 `[0, T)` 上做拒绝采样，拒绝概率上界恒定（< 1/2），
  与短语空间稀疏度无关。
- **同音组而非全词去重**：题面只要求同音组不重复；非同音词允许重复出现，
  重复是否合法仍受禁连（如自邻接 `(x,x)`）约束。
- **禁连为有向词对**：便于精确表达“a 后面不能跟 b”这类口述混淆规则；
  双向限制通过传入两个词对实现，并有测试锁定该语义。
- **排名按词编号字典序**：编号即词表下标，排序确定、可复现，适合小词表穷举复核。
