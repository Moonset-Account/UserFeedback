# dissect.regf

A Dissect module implementing a parser for Windows registry file format, used to store application and OS configuration
on Windows operating systems. For more information, please see [the
documentation](https://docs.dissect.tools/en/latest/projects/dissect.regf/index.html).

## Requirements

This project is part of the Dissect framework and requires Python.

Information on the supported Python versions can be found in the Getting Started section of [the documentation](https://docs.dissect.tools/en/latest/index.html#getting-started).

## Installation

`dissect.regf` is available on [PyPI](https://pypi.org/project/dissect.regf/).

```bash
pip install dissect.regf
```

This module is also automatically installed if you install the `dissect` package.

## Build and test instructions

This project uses `tox` to build source and wheel distributions. Run the following command from the root folder to build
these:

```bash
tox -e build
```

The build artifacts can be found in the `dist/` directory.

`tox` is also used to run linting and unit tests in a self-contained environment. To run both linting and unit tests
using the default installed Python version, run:

```bash
tox
```

For a more elaborate explanation on how to build and test the project, please see [the
documentation](https://docs.dissect.tools/en/latest/contributing/tooling.html).

## BigData 解析质量核验（本地）

本仓库附带针对 `dissect/regf/regf.py` 中 `KeyValue.data`、`KeyValue.is_big_value` 与 `BigData.data`
的质量保障用例（仅新增测试、夹具与重放入口，不修改生产实现）。

### 实际 API

- `RegistryHive(fh)`：从文件对象解析 hive，`hive.version` 取自 HBASE_BLOCK 的 Minor 版本。
- `KeyValue.data`：按 `DataLength` 选择存储分支——高位 `0x80000000` 置位时取 `Data` 字段内联 dword
  （按声明长度截断）；否则 `is_big_value` 为真（hive Minor 版本 > 4 且 `DataLength > 0x3FD8`）时经
  `db` cell 走 BigData 分段读取；其余按普通 cell 读取并截断到声明长度。
- `BigData.data`：按段列表 cell 中的偏移顺序拼接，每段截取 16344 字节（`CM_KEY_VALUE_BIG`）。

### 命令

```bash
.venv/bin/python tools/local_verify.py --list          # 列出用例
.venv/bin/python tools/local_verify.py --case C01      # C01/C02/C03/C04，缺省运行全部
.venv/bin/python -m pytest tests/test_regf_bigdata.py  # 同一批断言的 pytest 入口
```

`tools/local_verify.py` 每组输出一条 JSON：case、实际断言数、passed/failed/skipped、种子、
样本偏移（角色、HCELL、文件偏移、长度、SHA-256 摘要）及失败断言的期望/观察摘要。
任一断言失败、用例缺失或零断言时退出码非零。

### 可重放样本

- 所有 hive 由 `tests/bigdata_hive.py` 用 `c_regf` 结构在内存中构造，不依赖外部样本文件。
- 固定种子：C01 `0xC01C01`、C02 `0xC02C02`、C04 `0xC04C06`（5 段、乱序物理布局、混合间隙与
  cell 内填充）；同一种子字节级重放，C04 对此有专门断言。
- C04 的缩减样例（2 段：16344 + 1 字节）保留原 BigData 存储分支与每段取证来源偏移。

### 未覆盖边界（解析能力边界，C03 单独记录）

- 截断的分段列表不会被校验：解析器把 cell 内 `0xCC` 填充当作额外段偏移解引用，在 IO 层抛出
  `ValueError`（负 seek），而非结构化的解析错误。
- `Data` 指向非 `db` cell：已知签名抛 `dissect.regf.exceptions.Error`，未知签名抛
  `NotImplementedError`。
- hive Minor 版本 ≤ 4 的大值按单个普通 cell 读取（无 BigData 分支）；内联存储仅覆盖 ≤ 4 字节。
- `RegistryHive.walk()`、脏 hive / 事务恢复不在本组用例范围内。

## Contributing

The Dissect project encourages any contribution to the codebase. To make your contribution fit into the project, please
refer to [the development guide](https://docs.dissect.tools/en/latest/contributing/developing.html).

## Copyright and license

Dissect is released as open source by Fox-IT (<https://www.fox-it.com>) part of NCC Group Plc
(<https://www.nccgroup.com>).

Developed by the Dissect Team (<dissect@fox-it.com>) and made available at <https://github.com/fox-it/dissect>.

License terms: AGPL3 (<https://www.gnu.org/licenses/agpl-3.0.html>). For more information, see the LICENSE file.
