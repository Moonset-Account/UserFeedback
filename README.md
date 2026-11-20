# mime_locator — 只读 MIME 证物定位库

复核人员在转发邮件附件里选中一段**最终解码后**的字节时，本库返回这段选区在
**原始邮件源字节**中对应的编码单元区间，以及从根到该附件的层级路径。库本身
只读：不修改输入、不写文件、不发起任何网络访问。

## 解决的问题（对应需求约束）

- **有明确边界的多段邮件**：自行实现偏移保留的 MIME 扫描器，标准库
  `email` 不保留原始绝对偏移；所有返回区间都是相对原始源字节的半开区间
  `[start, end)`。
- **嵌套 `message/rfc822`**：递归解析，层级路径中以 `rfc822[0]` 记录嵌套跳转。
- **base64 / quoted-printable 传输编码**：选区按**最终解码字节**计数，再映射
  回原始编码单元；另支持 `7bit`/`8bit`/`binary` 及未知编码的逐字节直通。
- **base64 单字节归组**：一个解码字节落在某个四字符组内时，选区自动扩展为
  该**完整四字符组**（含其 `=` 填充字符）。
- **软换行与边界行不是正文证据**：base64/QP 的软换行（`=\r\n`、编码行之间的
  CRLF）与 multipart 边界分隔行永远不会出现在返回区间内；区间在必要时拆成多段，
  调用方拼接后即可独立用标准库重新解码校验。
- **重复内容按附件实例区分**：即使两个附件字节完全相同，也拥有不同的
  `leaf_index`、层级路径与物理原始区间，不会被合并。
- **损坏隔离**：单个附件编码损坏（非法 base64 字符、截断组、非法填充、畸形
  QP 转义等）时仅该部件标记为 `corrupted` 并拒绝定位，兄弟附件仍可独立定位。

## 包结构与实际入口

```
mime_locator/
├── __init__.py     # 公共 API 导出
├── model.py        # PartNode / PathEntry / Unit / Location / 异常 / 区间合并
├── encodings.py    # base64、quoted-printable、identity 三种编码映射器
├── parser.py       # 偏移保留的只读 MIME 解析器（multipart、rfc822、头解析）
├── archive.py      # MimeArchive 门面：locate() / locate_path() / find_parts()
├── cli.py          # inspect / locate 命令行入口
└── __main__.py     # 支持 python -m mime_locator
```

### 编程入口

```python
from mime_locator import MimeArchive

archive = MimeArchive(raw_message_bytes)

leaf = archive.leaves()[1]                 # 深度优先、稳定编号的可选中叶节点
location = archive.locate(leaf.leaf_index, start=10, end=11)

location.path_string()      # 'root > part[1]'
location.encoding           # 'base64'
location.covered_start/end  # 组对齐后实际覆盖的解码区间
location.raw_ranges         # ((绝对起点, 绝对终点), ...)，已排序、已合并
location.extract(archive.source)  # 只读回读原始编码字节（软换行已剔除）
```

- `MimeArchive.locate(leaf_index, start, end=None)`：半开解码字节选区；
  `end` 缺省等于 `start`，零宽选区合法且返回空区间。
- `MimeArchive.locate_path(path, start, end)`：按 `PathEntry` 层级路径定位。
- `MimeArchive.find_parts(filename=..., content_type=...)`：重复附件逐个返回。
- 异常：`MimeParseError`（结构非法）、`InvalidSelectionError`（越界/反序/
  非整数）、`CorruptedPartError`（继承自 `InvalidSelectionError`，部件编码损坏）、
  `NotSelectableError`（对容器节点定位）。

### 命令行入口（只读）

```
python -m mime_locator inspect message.eml
python -m mime_locator locate message.eml --leaf 1 --start 10 --end 11
```

定位非法时 CLI 以退出码 `2` 返回并向 stderr 输出原因。

## 验证范围与运行方式

测试位于 `tests/`，全部用 `unittest` 编写，夹具在 `tests/fixtures.py` 本地
合成（无网络、无外部样本）。运行（隔离模式，避免从 CWD 加载意外模块）：

```
python3 -I -m unittest discover -s tests -t .
```

共 **44 个**测试用例，覆盖题面交叉约束的三类场景：

1. **可接受结果**
   - base64 单字节选区扩展为完整四字符组，并可用标准库独立重解码；
   - 多组选区、跨软换行选区拼接后与原始载荷字节一致；
   - QP 十六进制转义映射到 3 个原始字节 / 1 个解码字节；
   - 8bit 正文逐字节 1:1 映射；
   - 嵌套 `message/rfc822` 的层级路径与按路径定位；
   - 重复附件返回不同实例、不同物理区间；
   - 端到端用例 `test_cross_constraints.py` 一次性串联全部约束。
2. **应被阻止 / 拒绝的结果**
   - 选区越界、负偏移、起止反序、非整数（含 `bool`）、未知叶索引；
   - base64 非法字符、截断组、非法/非末尾填充 → 部件标记损坏并抛
     `CorruptedPartError`；
   - 畸形 QP 转义、非法控制字节 → 同样隔离；
   - 空源、无头/体分隔、multipart 缺 boundary、multipart 未闭合 →
     `MimeParseError`；
   - 损坏部件的兄弟附件仍正常定位（隔离性断言）。
3. **边界 / 交错场景**
   - 选区起点/终点恰好落在 base64 组边界；两端各落在组内一个字节时双侧扩展；
   - 零宽选区（含 EOF 游标）返回空区间但保留路径；
   - 跨 QP 软换行选区剔除 `=\r\n`，硬换行 CRLF 正常映射；
   - 全部返回区间断言落在部件 body 内、排序合并、半开且不包含任何边界行；
   - 源字节在解析前后保持不变（只读保证）；
   - CLI `inspect`/`locate` 成功路径与失败退出码。

## 设计取舍与限制

- 只处理 CRLF（兼容裸 LF）的明确边界 multipart；前置序言（preamble）与结束
  分隔符之后的尾声（epilogue）不作为证据。
- 边界分隔行及其前导 CRLF 属于传输帧，不计入部件 body，因此不可能出现在
  定位区间中。
- 未实现 RFC 2231 续行参数等少见头编码；文件名支持普通 quoted-string 与
  RFC 2047 编码字。
- 不做真实邮件抓取、目标探测或任何网络行为；所有输入均为调用方提供的字节。
