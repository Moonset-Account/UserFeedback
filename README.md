# 构建产物分流命令（公开下载区 / 内部符号库）

纯 Python 3 标准库实现，无第三方依赖。入口为 `app.py`，测试在 `tests/`，
本地核验入口为 `tools/local_verify.py`。

## 1. 解决的问题

每次构建提供两个架构的产物目录和一份 JSON 索引，索引声明构建编号、架构、
产物角色以及主程序和符号文件各自的调试标识（debug identifier）。本工具：

1. 生成**公开归档计划**——只列运行所需文件，永不包含调试符号；
2. 生成**内部归档计划**——按架构和调试标识组织符号，内容相同的符号只存
   一份并被多个构建合并引用，内容不同则报冲突；
3. 生成**拦截清单**——用文件清单表现整批被拦下的原因，以及其余批次还可以
   发布哪些文件。

## 2. 目录结构

```
app.py                    CLI 主入口（triage / list 两个子命令）
triage/
  errors.py               异常层级（致命错误 vs 批次级错误）
  safety.py               路径越界/符号链接检查、调试标识校验
  models.py               索引 schema 与解析
  engine.py               批次校验、符号合并/冲突判定、三份计划生成
tests/
  _helpers.py             合成离线夹具辅助
  test_engine.py          引擎级测试（TC-001 ~ TC-009）
  test_cli.py             端到端 CLI 测试（TC-010 ~ TC-016）
tools/
  local_verify.py         本地核验（discover 测试 + 样例断言，出报告）
  smoke_demo.py           合成三批次端到端演示（临时目录，离线）
verify/                   核验报告输出目录（已 gitignore）
```

## 3. 输入索引格式

每个构建批次一个 JSON 文件，路径相对于该批次的产物根目录（`--root`）：

```json
{
  "build_id": "B20261114-001",
  "architecture": "arm64",
  "artifacts": [
    {"role": "main_program",     "path": "bin/app",          "debug_id": "A1B2-..."},
    {"role": "runtime_library",  "path": "lib/libfoo.dylib"},
    {"role": "runtime_resource", "path": "share/config.json"},
    {"role": "debug_symbol",     "path": "symbols/app.dbg",  "debug_id": "A1B2-..."}
  ]
}
```

角色语义：

| role               | 去向   | debug_id 要求 |
|--------------------|--------|---------------|
| `main_program`     | 公开   | 必填          |
| `runtime_library`  | 公开   | 可选          |
| `runtime_resource` | 公开   | 可选          |
| `debug_symbol`     | 内部库 | 必填          |

未知角色、缺失字段、含路径分隔符的调试标识都会让**该批次**被拦下；
JSON 无法解析、缺少 `build_id`/`architecture`/`artifacts` 等结构性错误
属于致命错误，整个运行以退出码 2 结束，不产生半成品计划。

## 4. 输出语义

### 4.1 公开归档计划 `public.json`

- `archive: "public"`；按批次列出，只包含三种运行时角色；
- 每个文件带 `role`、`debug_id`（可空）、`size`、`sha256`；
- 任何 `debug_symbol` 都不会出现；被拦批次整体不出现。

### 4.2 内部归档计划 `internal.json`

- `archive: "internal"`；先按 `architecture` 分区，再按 `debug_id` 组织；
- 同一 `(architecture, debug_id)` 下：
  - 各贡献者符号 `sha256` 相同 → 一个条目，`merged: true`，
    `references` 列出所有引用它的批次，`sources` 保留每个来源以便审计；
  - `sha256` 不同 → 判定为 `symbol_content_conflict`，冲突条目不入库，
    所有贡献批次全部拦下；
- 冲突判定以架构为作用域：不同架构复用同一调试标识不算冲突。

### 4.3 拦截清单 `manifest.json`

- `summary`：总批次数、拦批数、可发布批次数、符号冲突数；
- `blocked_batches`：每个被拦批次的完整原因列表（稳定 reason code，
  含涉及的路径、调试标识、观测到的哈希等细节）；
- `symbol_conflicts`：冲突的架构/调试标识对；
- `publishable`：未受影响批次仍可发布的公开文件与符号文件清单
  （`all_public_files` / `all_symbol_files` 平铺视图 + 按批次视图）。

### 4.4 退出码

| 码 | 含义 |
|----|------|
| 0  | 全部批次可发布 |
| 2  | 致命输入错误（索引不可读/JSON 非法/CLI 参数错误），不产出计划 |
| 3  | 计划已产出，但存在被拦批次或符号冲突 |

## 5. 安全边界（不可信输入处理）

索引与产物目录均按不可信数据处理：

- 拒绝绝对路径、含 `..` 的路径、空路径；
- 逐组件解析符号链接，链接目标逃逸出 `--root` 即拒绝；
- 最终解析结果必须仍位于根目录之内；
- 调试标识只接受不含空白与路径分隔符的可打印 token；
- 建议用隔离模式运行：`/usr/local/bin/python3 -I app.py ...`。

## 6. 使用方法

```bash
# 一个共享根目录，或每个 --index 配一个同序 --root
/usr/local/bin/python3 -I app.py triage \
  --index builds/b100/index.json --root builds/b100 \
  --index builds/b101/index.json --root builds/b101 \
  --public-plan out/public.json \
  --internal-plan out/internal.json \
  --manifest  out/manifest.json \
  [--json-summary out/summary.json]

# 列出某份归档计划的内容（text 或 json）
/usr/local/bin/python3 -I app.py list out/public.json
/usr/local/bin/python3 -I app.py list out/internal.json --format json
```

合成端到端演示（自动建临时夹具，覆盖合并 + 拦批 + list）：

```bash
/usr/local/bin/python3 -I tools/smoke_demo.py
```

## 7. 本地核验

```bash
/usr/local/bin/python3 tools/local_verify.py
```

该脚本：

1. 用 `unittest.defaultTestLoader.discover("tests")` 发现套件，并断言
   `countTestCases() >= 1`；发现 0 个用例直接非零退出；
2. 实际运行套件，统计真实执行且未跳过的用例数；全部跳过、任何失败或
   错误均非零退出（不接受 unittest 零测试退出 0）；
3. 额外执行 5 条交付样例/边界断言（S-001 ~ S-005），逐条保留
   `case_id`、输入、期望值与实际值；
4. 报告写入 `verify/local_verify_report.json` 与
   `verify/local_verify_report.txt`。

## 8. 需求条款 → 测试/样例映射

| 需求要点 | 稳定 case_id | 断言内容（输入 → 期望/实际） |
|---|---|---|
| 公开计划只列运行所需文件 | TC-001、TC-010；S-001 | 公开计划角色集合恰为 main_program/runtime_library/runtime_resource，无 debug_symbol |
| 内部计划按架构+调试标识组织 | TC-002、TC-006、TC-010 | internal.architectures 按架构分区，条目键为 debug_id |
| 同标识同内容符号合并引用 | TC-001；S-002 | 两批次同符号字节 → `merged=true`、`reference_count=2`、references 含两个批次 |
| 同标识不同内容报冲突 | TC-005；S-003 | 两个不同 sha256 → 两个贡献批次均带 `symbol_content_conflict`，冲突符号不入库 |
| 冲突按架构隔离 | TC-006 | 不同架构复用同一 debug_id 不产生冲突，`conflict_count=0` |
| 标识不一致只拦受影响批次 | TC-003、TC-011、TC-014；S-004 | 仅坏批次带 `debug_id_mismatch`，好批次仍在 publishable 与两份计划中 |
| 文件清单说明拦批原因 | TC-003、TC-011 | manifest.blocked_batches 含 reason code + message + 细节字段 |
| 清单列出其余批次可发布文件 | TC-014；S-004 | manifest.publishable 含好批次的 public_files/symbol_files，坏批次不出现在其中 |
| 缺失产物拦批次且不牵连他人 | TC-004 | 缺文件批次带 `missing_file`，兄弟批次正常发布 |
| 路径越界拦截 | TC-007、TC-009；S-005 | `../`、绝对路径 → `unsafe_path`，批次拦下 |
| 符号链接逃逸拦截 | TC-008 | 指向根目录外的 symlink → `unsafe_path`，符号条目不入库 |
| 非法调试标识拦截 | TC-015 | 含 `/` 的 debug_id → `invalid_artifact_entry`，退出码 3 |
| 结构性坏索引致命失败 | TC-012 | 非法 JSON → 退出码 2，不写任何计划文件 |
| CLI 参数配对校验 | TC-013 | 2 个 index 给 3 个 root → 退出码 2 |
| 无子命令不可静默成功 | TC-016 | argparse 拒绝空参数，SystemExit |
| 列出两份归档内容 | TC-010 | `list` 子命令分别可读回公开/内部计划，公开列表不含符号路径 |

## 9. 限制与约束

- 仅使用 Python 3 标准库；不联网、不访问任何外部系统；
- 所有夹具均为测试内合成的离线数据，监听端口等行为本工具不需要；
- 哈希统一为 SHA-256，文件大小为字节数；
- 计划 JSON 的键顺序固定（有序字典），便于文本 diff 与评审；
- 本工具产出的是**归档计划**（带哈希与引用关系的清单），不实际打包或
  上传，安装包入公开下载区、符号入内部库的落地由下游消费计划完成。
