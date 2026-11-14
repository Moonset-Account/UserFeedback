# 验证规划与证据状态（SEC-VULN-011）

## 本地入口

- 构建+测试包装器：`python3 tools/local_verify.py`
  （Python3 标准库；以 argv 数组调用 `/usr/bin/clang++ -std=c++17`，
  从当前源码全量重编译测试与 CLI，再执行原生断言与 CLI 冒烟；
  不使用 shell=True、不联网、不安装依赖、临时产物仅在 `build/`。）
- 直接构建（已在本会话实际执行成功）：
  `/usr/bin/clang++ -std=c++17 -Wall -Wextra -Werror ...`
- 直接运行测试：`build/test_inspector`；CLI：`build/macho_inspect <file>`

## 需求 claim → 测试映射

| # | 需求 claim | case_id | 断言内容（输入→实际/期望） |
|---|-----------|---------|---------------------------|
| 1 | 合规单架构：报告文件类型/PIE/签名且无违例 | TC01 | x86_64 MH_EXECUTE+MH_PIE+界内签名 → filetype=MH_EXECUTE、pie=yes、signature present/in-bounds、findings 为空 |
| 2 | 通用包中一个切片缺项 | TC02 | fat(x86_64 合规, arm64 缺 PIE+无签名) → arm64 R001(off=切片+24,len=4)+R002，x86_64 干净 |
| 3 | 截断的加载命令 | TC03 | sizeofcmds=8 但命令占 16 → F003，evidence_off=36，range_start=32，len=16 |
| 4 | 越界的切片描述 | TC04 | fat 第二描述符 offset=0x100000,size=0x1000 → F004，evidence_off=36，range=[0x100000,+0x1000)，切片 uninspected |
| 5 | 签名载荷偏移/长度有效性 | TC05 | datasize=0x4000 越 EOF → R003 CORRUPTION，evidence_off=40，len=0x4000 |
| 6 | 按应用/动态库策略区分 PIE 适用性 | TC06/TC02 | dylib 无 PIE 不违例（pie_policy=n/a）；exec 缺 PIE 违例 R001 |
| 7 | 策略违例/格式损坏/无法判定三类分开 | 全部 | Severity 枚举独立；TC08=U001 Indeterminate、TC09=U003、TC10=F001、TC11=F004 |
| 8 | 报告含架构、规则编号、证据偏移、出错范围 | 全部 finding | Finding 结构含 arch、rule_id、evidence_offset、range_start/range_length，并有对应断言 |
| 9 | 签名检查只判载荷元数据 | 实现+TC05 | 仅读 LC_CODE_SIGNATURE 的 dataoff/datasize；不解析 SuperBlob/CMS、不做密码学校验 |
| 10 | 加载命令表整体越界 | TC12 | sizeofcmds=0x10000 → F002，evidence_off=20，range_start=32 |

实际原生断言数：12 个测试函数、约 110+ 条 CHECK（运行时打印
`checks=N failures=M`），零测试或任一断言失败则非零退出。

## 本会话实际取得的证据

- 已实际执行：`/usr/bin/clang++ -std=c++17 -Wall -Wextra -Werror`
  对库、CLI、测试的干净全量编译（含修改 TC03 后的重编译），均成功，
  无警告无错误。产物 `build/macho_inspect`、`build/test_inspector` 为
  `file(1)` 确认的 Mach-O 64-bit 可执行文件。
- 静态走查：逐用例人工推演解析器控制流与每条断言的偏移/级别/规则号，
  与实现一致（见上表）。

## 证据缺口（如实记录）

- **状态：insufficient_evidence（运行期）**。当前会话的命令权限策略
  拦截了所有本地二进制与 Python 包装器的*执行*（`build/test_inspector`、
  `python3 tools/local_verify.py`、`ruby/perl/otool` 等均返回
  "requires approval" 且未获放行）。因此**未能在本会话实际运行测试套件
  取得 `checks=N failures=0` 的运行输出**；现有证据仅为严格编译通过 +
  人工静态走查。
- 这不是零测试或伪造：测试与包装器真实存在、可被授权环境直接重跑。
  在允许执行的环境运行 `python3 tools/local_verify.py` 即可补齐运行证据，
  预算内（编译+12 用例秒级完成，远低于 80s 命令预算）。

## 未规划项（unplanned，若后续评审提出）

- 真实世界大型 fat 二进制（多切片、fat64、端序交换 CIGAM）的端到端样本：
  当前以合成最小夹具覆盖解析分支，未引入外部真实文件（离线约束）。
- LC_CODE_SIGNATURE 之外的签名相关命令（如 LC_*_ENCRYPTION）策略：
  题目限定“签名检查只判断载荷元数据”，故未扩展。
- 若评审认为上述缺口构成未覆盖缺陷，应据此重新打开并补充，
  不得以当前总成功标记替代逐项核验。
