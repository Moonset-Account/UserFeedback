# fontsubset — 字体资源裁剪模块

档案馆场景：对外提供一小段已脱敏文字，并随附能排出原样的精简字形包。
本模块输入**字符映射（cmap）**、**组合字形引用（composite components）**、
**按相邻字形触发的替换规则**以及**可公开文本清单**，产出一个紧凑重编号的
子集字体，并用排版回放验证每段公开文字的字形序列与位置都能还原。

运行环境：Python 3.11.6（仅标准库，无第三方依赖）。

## 数据模型（简化）

- **Font**：字形表（gid 即索引，gid 0 为 `.notdef`）+ `cmap`（码点 → gid）
  + 有序替换规则表。
- **Glyph**：简单字形携带不透明的 `outline` 数据；组合字形携带
  `components`（被引用 gid + dx/dy 偏移）。
- **Rule**：`input`（相邻字形 gid 序列）→ `output`（替换序列）。
  连字（`f i → fi`）与语境替换（`a b → a b.alt`）都可表达。
  规则按声明顺序逐条扫描应用，从左到右、不重叠，替换后继续向后扫描。

## 公开 API

```python
from fontsubset import (
    build_font,        # 由名字规格的 glyph/cmap/rule 构建 Font
    Font, Glyph, Component, Rule,
    shape,             # shape(font, text) -> [PositionedGlyph(gid, name, x, y)]
    subset_font,       # subset_font(font, public_texts, deduplicate=True) -> SubsetResult
    verify_subset,     # verify_subset(original, subset, texts) -> VerificationReport
    # 异常：
    FontSubsetError, MissingMappingError, CompositeCycleError,
    SubsetVerificationError, UnknownGlyphError, DuplicateGlyphError,
)
```

`subset_font` 的流程与保证：

1. **生效替换所需部件全部保留**：公开文本经 cmap + 规则排版后的输出字形、
   公开码点的基础字形（cmap 查回所需），以及组合字形引用的**传递闭包**
   全部进入保留集。
2. **内部部件不取得外部映射**：子集 cmap 恰好只含公开文本出现过的码点；
   仅作为部件存在的字形（如 `dotlessi`、连字 `fi`）不会有任何 cmap 条目。
3. **删除字符不可查回**：被删码点即使与公开字符共用同一字形
   （如全角 Ａ → `A`）或共用相同轮廓（不同字形、相同 outline 数据，
   去重合并后）也不会出现在子集 cmap 中；轮廓去重只合并字形，
   绝不新增码点映射。
4. **紧凑重编号**：保留字形压缩为连续 gid（`.notdef` 固定为 0），
   `SubsetResult.old_to_new` 给出原 gid → 新 gid 映射。
5. **引用成环时报出涉及部件**：闭包扩展中检测组合引用环，抛出
   `CompositeCycleError`，其 `cycle` 属性按引用顺序列出涉及的字形名
   （首尾相同闭合）；不可达的环不影响裁剪。
6. **排版回放验证**：每段公开文字分别用原字体与子集字体排版，
   逐字形比较 `(名称, x, y)`；不一致则抛出 `SubsetVerificationError`
   （携带 `VerificationReport`）。`verify_subset` 可单独用于复核
   任何子集字体。

`SubsetResult` 字段：`font`、`public_codepoints`、`kept_glyph_names`、
`old_to_new`、`dropped_glyph_names`、`report`。

## 运行方法

```bash
python3 -m unittest discover -s tests -v
```

## 验证范围（tests/，全部为本地合成数据）

- `test_shaping.py`：cmap 映射、相邻触发连字/语境替换、位置按 advance
  累计、规则顺序敏感的交错情形（`ffi`/`fff`/`fifi` 两种规则序）、
  未映射字符报错、空文本。
- `test_subset.py`：闭包保留与多余字形丢弃、紧凑重编号、规则过滤与
  重映射、子集 cmap 恰好等于公开码点集、共用同一字形/共用相同轮廓的
  删除字符不可查回、内部部件无映射、去重合并方向（部件副本并入公开
  字形）、关闭去重时副本保留但仍无映射、公开文本回放一致（含交错
  连字字体）、空公开清单、三节点环/自环报出涉及部件、不可达环不阻塞、
  公开文本含未映射字符时报错。
- `test_verify.py`：验证器自身有效性——子集被删规则或篡改 advance
  后回放必检出失败，`SubsetVerificationError` 携带报告。

## 已知限制（简化模型）

- 位置仅由 advance 累计（x 方向），不含字偶距/GPOS 类调整；
  部件 dx/dy 只影响组合字形内部，不参与笔位计算。
- 替换规则为定长序列→序列，按声明顺序应用，无回溯/优先级冲突解决。
- 轮廓为不透明可哈希数据，去重按 `(advance, outline, components)`
  完全相等合并。
