# SEC-VULN-013 函数级栈保护覆盖回归检查器

变更编译优化配置（例如 `-O0` 切到 `-O2`）后，部分原本独立的源码函数会被内联到调用者中，
函数级栈保护（stack protector / stack clash 等抽象保护标记）的承接关系随之变化。本工具读取
三类**离线、抽象**的本地记录，逐个构建配置判断风险清单中要求保护的函数是否仍有对应证据，
并给出每个源码项由哪一个（或哪几个）编译后函数承接的最小证据链。

纯 C++17 + 标准库实现，无第三方依赖，使用 `/usr/bin/clang++ -std=c++17` 编译。

## 1. 目录结构

```
src/
  main.cpp                     CLI 入口
  ssp_checker/
    Types.h / Types.cpp        枚举、记录类型与稳定 token
    Parser.h / Parser.cpp      三类输入文件的离线解析器
    Analysis.h / Analysis.cpp  归属连接、判定引擎、跨配置差异与复核单元汇总
    Report.h / Report.cpp      稳定文本报告渲染
tests/
  test_ssp_checker.cpp         原生 C++17 断言测试（15 个稳定 case_id）
  fixtures/                    合成离线输入（模拟构建工具导出，非真实系统数据）
    risk_list.txt              函数风险清单
    O0_attribution.txt / O0_marks.txt
    O2_attribution.txt / O2_marks.txt
tools/
  local_verify.py              本地编译/运行包装器（Python 3 标准库，-I 隔离模式）
```

## 2. 输入格式

三类文件使用同一行约定：`#` 起始为注释，空行忽略；字段以 `|` 分隔，字段两端空白会被裁剪；
非注释行字段数必须精确匹配，重复 ID、重复键、未知枚举都会带行号报错。

### 2.1 函数风险清单（6 字段）

```
risk_id | source_function | compilation_unit | risk_level | requirement | note
```

- `risk_level ∈ {low, medium, high}`
- `requirement ∈ {required, exempt_leaf}`
  - `required`：策略要求该源码函数具备栈保护证据
  - `exempt_leaf`：策略明确不要求保护的叶函数（直接判 `NOT_REQUIRED`，不需要任何构建记录）

### 2.2 优化后归属映射（6 字段）

```
config | source_function | compiled_function | compilation_unit | relation | attribution_id
```

- `relation ∈ {self, inlined}`
  - `self`：源码函数仍保留为同名编译后函数
  - `inlined`：源码函数被吸收进某个调用者，由该编译后函数承接
- 同一 `source_function` 在同一 `config` 下可有多行，表示多个内联归属（多个调用者）

### 2.3 构建工具导出的抽象保护标记（6 字段）

```
config | compiled_function | compilation_unit | mark | protector_kind | mark_id
```

- `mark ∈ {protected, unprotected}`；`protector_kind` 为自由文本（如 `ssp-strong`）
- 保护标记按 `(config, compiled_function, compilation_unit)` 唯一键连接归属记录；
  同一键出现冲突标记直接解析失败，不静默取舍

## 3. 判定语义

对每个 `(风险清单条目, 构建配置)` 组合，按以下顺序判定：

| 判定 | reason token | 触发条件 |
|---|---|---|
| `NOT_REQUIRED` | `exempt_leaf` | 策略标记为 `exempt_leaf` 的叶函数 |
| `UNKNOWN` | `missing_attribution` | 该配置下没有任何归属行（缺失构建记录） |
| `UNKNOWN` | `missing_protection_mark` | 存在归属，但承接的编译后函数没有导出保护标记 |
| `UNKNOWN` | `partial_caller_protection` | 多个归属中只有部分调用者有保护（归属不确定，不能视为已覆盖） |
| `CONFIRMED` | `self_protected` | 函数自身的编译后记录带 `protected` 标记 |
| `CONFIRMED` | `inlined_caller_protected` | 全部承接该函数的内联调用者记录均为 `protected` |
| `VIOLATION` | `unprotected_self` | 自身记录明确为 `unprotected` |
| `VIOLATION` | `unprotected_callers` | 全部内联调用者均为 `unprotected` |

关键规则：

1. **缺失即未知**：缺少归属或缺少保护标记时输出 `UNKNOWN`，绝不臆造保护结论。
2. **多归属从严**：多个调用者保护状态不一致时输出 `UNKNOWN`（`partial_caller_protection`），
   并把其中未保护的承接单元列入复核清单；全部受保护才可 `CONFIRMED`。
3. `exempt_leaf` 不需要任何证据即判 `NOT_REQUIRED`，与是否内联无关。
4. 最小证据链按 `attribution_id` 排序，逐跳给出
   `源码函数 -> 编译后函数 @ 编译单元 -> mark_id / mark=MISSING`，可直接定位复核对象。

## 4. CLI 用法与退出码

```bash
/usr/bin/clang++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -Isrc \
  src/ssp_checker/*.cpp src/main.cpp -o .build/ssp-cover-check

.build/ssp-cover-check \
  --risk-list tests/fixtures/risk_list.txt \
  --bundle O0=tests/fixtures/O0_attribution.txt,tests/fixtures/O0_marks.txt \
  --bundle O2=tests/fixtures/O2_attribution.txt,tests/fixtures/O2_marks.txt \
  --report .build/coverage_report.txt [--strict]
```

- `--bundle <config>=<归属文件>,<标记文件>` 可重复，配置按命令行顺序参与跨配置比较
- 报告同时写到 stdout 与 `--report` 指定文件；行前缀 `V |`、`S |`、`D |`、`U |`、`W |`
  分别表示函数判定、配置汇总、跨配置差异、待复核编译单元、数据质量告警，可稳定解析

退出码：

| 码 | 含义 |
|---|---|
| 0 | 分析完成（`--strict` 下还表示无 `VIOLATION` 且无 `UNKNOWN`） |
| 1 | 用法错误、文件无法打开或输入解析失败（错误带文件名与行号） |
| 3 | `--strict` 模式下至少存在一条 `VIOLATION` |
| 4 | `--strict` 模式下无违规但至少存在一条 `UNKNOWN` |

## 5. 一键本地验证

```bash
python3 -I tools/local_verify.py
```

包装器仅使用 Python 3 标准库、以 argv 数组（不使用 `shell=True`）调用 `/usr/bin/clang++`，
每次先删除旧二进制再从当前源码重新编译，随后执行：

1. 原生 C++17 断言套件（15 个 case，实际断言计数打印在总结行）
2. CLI 对 O0/O2 合成夹具的端到端断言（TC-CLI-001 ~ TC-CLI-012）
3. 缺失保护标记的临时合成场景（TC-CLI-013/014）
4. 负向用例：畸形输入行号报错、缺失文件退出 1、`--strict` 退出码 0/3（TC-CLI-015 ~ 018）

任何编译失败、零测试、断言失败都会非零退出；临时产物只落在工作区 `.build/` 内。

> 本地执行状态说明：交付工作区中 `/usr/bin/clang++ -std=c++17 -Wall -Wextra -Wpedantic
> -Werror` 对全部翻译单元（含测试与 CLI）的编译与 `-fsyntax-only` 严格检查已通过；
> 若当前会话的沙箱策略拦截二进制/Python 执行（表现为命令返回 approval gate），
> 在允许执行的终端中直接运行上面的 `python3 -I tools/local_verify.py` 即可从源码
> 重新编译并获得完整测试输出，包装器不依赖任何缓存产物。

## 6. 原生测试 case 与需求映射

| case_id | 断言内容 |
|---|---|
| TC-PARSE-001 | 风险清单解析为强类型记录（枚举、注释、空行、字段裁剪） |
| TC-PARSE-002 | 字段数错误与未知枚举带行号报错，合法行仍保留 |
| TC-PARSE-003 | 重复 risk_id 与冲突保护标记被拒绝 |
| TC-SELF-001 | 函数自身有保护记录 → `CONFIRMED/self_protected`，证据链含 attribution/mark id |
| TC-INLINE-001 | 内联后唯一受保护调用者承接 → `CONFIRMED/inlined_caller_protected` |
| TC-PARTIAL-001 | 多调用者仅部分受保护 → `UNKNOWN/partial_caller_protection`，仅未保护单元入复核清单 |
| TC-MISSING-ATTR-001 | 缺失构建归属 → `UNKNOWN/missing_attribution` |
| TC-MISSING-MARK-001 | 有归属无标记 → `UNKNOWN/missing_protection_mark`，证据链渲染 `mark=MISSING` |
| TC-VIOL-SELF-001 | 自身未保护 → `VIOLATION/unprotected_self` |
| TC-VIOL-CALLER-001 | 全部内联调用者未保护 → `VIOLATION/unprotected_callers` |
| TC-INLINE-002 | 多个调用者全部受保护 → `CONFIRMED` |
| TC-EXEMPT-001 | 策略不要求保护的叶函数 → `NOT_REQUIRED/exempt_leaf`，无记录也不复核 |
| TC-DIFF-001 | 跨配置回归：判定变化与仅 reason 变化均进差异区，复核单元按配置/单元聚合 |
| TC-REPORT-001 | 报告稳定前缀与最小证据链文本可被下游解析 |
| TC-WARN-001 | 未声明配置的记录被忽略并产生告警，不污染目标配置结论 |

## 7. 合成夹具预期结论速览

`tests/fixtures/` 模拟一个报文解析库的两次构建：

- **O0（无内联基线）**：7 个 required 函数全部 `CONFIRMED/self_protected`，R005 为
  `NOT_REQUIRED`；汇总 `confirmed=7 violation=0 unknown=0 not_required=1`
- **O2（优化后）**：
  - R001 自身保留且受保护（`self_protected`）
  - R002 内联进受保护的 `parse_packet_header`（`inlined_caller_protected`）
  - R003/R004 自身保留但保护丢失（`unprotected_self`，`VIOLATION`）
  - R005 策略豁免（`NOT_REQUIRED`）
  - R006 自身保留且受保护（`self_protected`）
  - R007 内联进两个调用者，仅 `parse_packet_header` 受保护、`handle_udp_segment`
    未保护（`partial_caller_protection`，`UNKNOWN`），`net/udp.c` 进入复核清单
  - R008 自身保留且受保护（`self_protected`）
  - 汇总 `confirmed=3 violation=2 unknown=2 not_required=1`

## 8. 限制与边界

- 全部输入为本地抽象记录，工具不访问真实构建系统、云服务或网络；报告只反映记录证据，
  不替代二进制反汇编核验
- 风险清单按 `source_function` 名称匹配归属行；同名函数跨编译单元的场景应在输入侧使用
  唯一全局限定名
- 只对命令行声明过的 `config` 出报告；输入中引用未声明配置会产生 `W |` 告警并忽略
