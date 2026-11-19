# tokenmatch — 作用域感知、容忍改名的词元比对库

面向再发行源码的来源复核。输入两个模块的**有序词元流**（词元已标注作用域
进入/退出、名称绑定、名称引用、字面量及输入侧绑定编号），输出所有达到最短
长度门槛的**极大连续匹配**，并为每处匹配给出两侧的原始区间与局部绑定的一对
一改名对照。

## 目录结构

```
tokenmatch/
  __init__.py     # 公开入口：Token、构造助手、Boilerplate、Match、compare_modules
  tokens.py       # Token 数据模型与 scope_enter/name_bind/name_ref/literal 助手
  compare.py      # 比对算法（反对角线扫描 + 双射一致性 + 样板断连）
tests/
  test_tokenmatch.py  # unittest 验收用例（21 个）
```

## 实际入口

```python
from tokenmatch import (
    Token,
    SCOPE_ENTER, SCOPE_EXIT, NAME_BIND, NAME_REF, LITERAL,
    scope_enter, scope_exit, name_bind, name_ref, literal,
    Boilerplate, Match, compare_modules, compatible,
)

matches = compare_modules(left_tokens, right_tokens,
                          min_length=3,
                          boilerplate=[Boilerplate("left", 8, 10)])
```

- `Token` 为不可变值对象。局部名称（`NAME_BIND` / `NAME_REF`）携带输入侧
  `binding` 编号；外部名称用 `external=True` 标注且不带局部绑定。
- `compare_modules(left, right, min_length, boilerplate)` 返回
  `list[Match]`，按左侧起点、再按右侧起点排序。
- `Match` 字段：
  - `left_start/left_end`、`right_start/right_end`：两侧半开原区间；
  - `length`：匹配词元对数（也是门槛计数单位）；
  - `bindings`：`{左侧绑定编号: 右侧绑定编号}` 的双射，按首次出现顺序建立；
  - `pairs()`：逐位置的 `(left_index, right_index)` 配对。
- `Boilerplate(side, start, end)`：某一侧的已批准公共样板半开区间。

## 匹配规则（题面约束的落地）

1. **作用域词元**：`scope_enter/scope_exit` 仅在 `scope_kind` 相同时匹配。
2. **局部名称**：绑定与引用按输入绑定编号核对，两侧编号在单个匹配内必须构成
   一致的一对一（双射）关系。同一左侧绑定改名后每次使用都必须指向同一右侧
   声明；两个局部绑定不得映射到同一右侧绑定（反之亦然）。
3. **声明在候选片段外**：片段内的引用仍按输入绑定编号参与双射核对，无需声明
   词元出现在匹配区间内（见
   `test_declaration_outside_fragment_verified_by_input_binding`）。
4. **外部名称**：两侧均为 `external` 且原文拼写相同才匹配，不进入绑定对照。
5. **字面量**：按 `value` 相等匹配（表面拼写不同但值相同仍匹配）。
6. **公共样板划断**：任一侧落入样板区间的词元对都不能属于匹配，样板两侧的
   真实片段无法借样板凑到 `min_length`；单侧标注样板同样断连。
7. **极大性与门槛**：每条反对角线上维护当前连续游程，遇到不兼容、样板或双射
   冲突即闭合；双射冲突时先发射冲突前的极大前缀，再从最近冲突绑定之后重启。
   只保留 `length >= min_length` 的匹配；不同反对角线产生的极大匹配允许重叠，
   全部保留。

## 算法要点

连续匹配必落在同一条反对角线（`i - j` 为常数）。对每条对角线线性扫描，用
`l_to_r` / `r_to_l` 两个字典维护当前游程的双射，用队列记录绑定对的引入位置；
冲突时按“最近一次涉及冲突绑定的位置 + 1”截断游程，并从保留队列重建双射，
保证重启后的前缀无冲突。样板标记用差分数组预计算。时间复杂度
`O(n_left * n_right)`，额外空间 `O(n_left + n_right)`。

## 验证范围与运行方式

测试全部为真实断言，无桩实现。运行（隔离模式，忽略环境注入路径）：

```bash
python3 -I -m unittest discover -s tests -v
# 或
python3 -I tests/test_tokenmatch.py
```

21 个用例覆盖题面全部交叉约束：

- **可接受结果**：完整改名片段产生单个匹配且双射正确（`AcceptedMatchTests`）。
- **应被阻止/拒绝**：同一绑定不一致改名、两个局部绑定映射到同一目标、外部
  名称拼写不符、局部/外部混用、字面量值不符、作用域种类不符
  （`BlockedMatchTests`）。
- **边界与交错**：`min_length` 含等于边界（8 过、9 拒）、样板划断使两段无法
  凑门槛、单侧样板即断连、片段外声明按绑定编号核对、冲突重启后的极大尾段及其
  绑定对照、多反对角线独立极大匹配（`BoundaryAndInterleavingTests`）。
- **随机不变量**：12 个固定种子的合成流，逐匹配复核门槛、等长区间、样板零
  参与、从实际配对重建的双射与报告一致、以及含双射约束的双向不可延展性；
  另含样板不可跨接保证（`RandomizedInvariantTests`）。
- **输入校验**：非法 `min_length`、空流、非法词元种类/绑定、非法样板区间、
  不可变值对象语义（`InputValidationTests`）。

仅使用本地合成夹具，不包含任何真实目标探测、攻击或漏洞利用内容。
