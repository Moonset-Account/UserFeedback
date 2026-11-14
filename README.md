# SEC-VULN-018 报文长度整数范围审计器

报文解码器在**扩容**(alloc)和**读取**(read)两个阶段分别计算长度。本工具对
两版构建生成的中间表示(IR)做有符号/无符号感知的区间抽象解释,定位
「经过检查的长度在哪一步变成了未受保护的长度」,并输出补丁前后的判定差异。

## 构建与运行(本地可重跑入口)

```sh
# 一键核验: 重新编译 + 原生测试 + CLI 样例 + diff 样例(任何失败非零退出)
/usr/bin/python3 tools/local_verify.py

# 或手动:
/usr/bin/clang++ -std=c++17 -Wall -Wextra -O1 -o build/secvuln018_analyzer \
    src/ir.cpp src/analyze.cpp src/report.cpp src/main.cpp
/usr/bin/clang++ -std=c++17 -Wall -Wextra -O1 -o build/test_analyzer \
    src/ir.cpp src/analyze.cpp src/report.cpp tests/test_main.cpp

./build/secvuln018_analyzer analyze tests/cases/case2_one_branch_unprotected.ir
./build/secvuln018_analyzer diff tests/cases/case10_patch_before.ir \
                                 tests/cases/case10_patch_after.ir
./build/test_analyzer tests/cases
```

仅依赖 C++17 标准库与 Python 3 标准库;不下载、不安装任何第三方依赖。

## 输入格式(带源码位置的结构化 IR)

行式文本,`#` 起为注释。除 `label` 外每条指令必须以 `@ 行:列` 结尾。
类型为定宽整数 `u8/u16/u32/u64/s8/s16/s32/s64`,有符号与无符号语义全程保留。

```
policy max_len <非负整数>            # 可选, 有效长度区间 [0, max_len], 默认 65535

func <名称> @ L:C
  param <var> <type> @ L:C                       # 函数参数(初值=类型全域或实参区间)
  const <dst> <type> <整数> @ L:C                # 常量(支持十进制/0x/负号)
  move  <dst> <src> @ L:C
  binop <dst> <type> <add|sub|mul|shl> <a> <b> @ L:C
  trunc <dst> <type> <src> @ L:C                 # 截断(缩窄位宽)
  zext|sext <dst> <type> <src> @ L:C             # 零/符号扩展
  cmp   <dst> <a> <eq|ne|slt|sle|sgt|sge|ult|ule|ugt|uge> <b> @ L:C
  check <cond> @ L:C                             # 条件为假即拒绝报文(后续路径假设为真)
  br    <cond> <labelT> <labelF> @ L:C           # 分支, 两条出边分别按真/假精化
  jmp   <label> @ L:C                            # 跳转(回边即循环)
  label <名称>
  call  <dst|_> <type|_> <函数> [实参...] @ L:C
  alloc <dst> <size> @ L:C                       # 分配 size 字节
  read  <buf> <len> @ L:C                        # 从 buf 读取 len 字节
  ret   <var|_> @ L:C
end

summary <函数>                                   # 可选函数摘要
  requires <实参下标> <=|<|>=|>|== <整数>         # 调用方必须满足的前提
  ensures  ret <=|<|>=|>|== <整数>               # 返回值保证
end
```

## 分析语义

- **取值语义**: 区间端点用 128 位整数表示,同时容纳 u64/s64 全域;`trunc/zext/sext`
  与有符号/无符号比较严格按类型语义求值。
- **分支路径精化**: `br`/`check` 用定义条件的 `cmp` 对两条出边分别施加约束
  (含取反),不可行路径直接剪除。
- **三类缺陷模式**:
  1. *运算后才检查范围* — `binop` 数学结果超出目标类型范围即判定回绕
     (风险 `OVERFLOW`),之后对结果值的检查只作用于回绕后的值,不构成保护;
  2. *先缩窄位宽再校验* — 对被 `trunc` 截断的值施加的检查不保护原始宽值
     (风险 `NARROW`),宽值流向 alloc/read 时定位到截断指令;
  3. *调用者约束在调用中丢失* — 被调函数无定义且无 `summary` 时
     (风险 `CALL_NO_SUMMARY`),调用点及其下游长度操作判「待确认」;
     有 `summary` 时校验 `requires` 并应用 `ensures`;被调函数有定义时
     按调用点做上下文敏感分析(不同实参独立判定)。
- **收敛规则(明确且有限)**:
  - 循环: 每个基本块精确迭代 4 次后对仍在增长的变量做 widening(区间跳到
    类型边界,风险 `WIDENED`),每块绝对上限 32 次;
  - 调用/递归: 深度上限 8;递归环或超限且无摘要时返回值按类型全域处理
    (风险 `RECURSION`),相关结论输出「待确认」。
- **判定**: alloc 长度须落入 `[0, max_len]` 且数据流链上无风险;read 另须
  不超过缓冲区(读取长度与分配大小为同一变量且定义唯一时必然不越界)。
  可证明有效=**安全**;可证明必然无效=**违规**;其余(含无法证明的路径)=**待确认**。

## 输出语义

`analyze` 报告包含:每条 alloc/read/call 的判定与依据(区间、约束路径、风险)、
**首个无法保证长度有效的指令**(数据流链上最早引入风险的指令;无风险时取链上
最早输出越出有效区间的指令)、受影响操作清单、全局首处与汇总、收敛规则触发记录。
`diff` 按 (函数, 操作类别, 同类序号) 配对两版 findings,输出
`[变化]/[相同]/[新增]/[移除]` 及汇总,容忍补丁引起的行号漂移。
退出码: 0=分析完成; 2=用法/读取/解析错误(判定为违规不影响退出码,结论以报告为准)。

## 验收用例映射(tests/cases/)

| 文件 | 覆盖点 | 期望结论 |
|---|---|---|
| case1_both_branches_safe.ir | 两条分支均满足范围约束 | 4 个操作全部安全 |
| case2_one_branch_unprotected.ir | 仅 else 分支缺少范围保证 | else 路径 alloc/read 待确认,首处=参数@2:1 |
| case3_callsite_independent.ir | 不同调用参数受独立约束 | 同一被调函数两个调用点分别判安全/待确认 |
| case4_missing_summary.ir | 缺少函数摘要 | 调用与下游 alloc/read 待确认,首处=调用@6:1 |
| case5_overflow_check_after_arith.ir | 运算后才检查范围 | 待确认,首处=加法@9:1(OVERFLOW) |
| case6_narrow_before_check.ir | 先缩窄位宽再校验 | 待确认,首处=截断@3:1(NARROW) |
| case7_loop_widening.ir | 循环收敛规则 | widening 记录 + 待确认 |
| case8_recursion.ir | 递归收敛规则 | 递归记录 + 待确认 |
| case9_read_violation.ir | 读取越界 | read 违规 |
| case10_patch_before/after.ir | 补丁前后判定差异 | diff 报告「待确认 -> 安全」 |
| case11_summary_ok.ir | 摘要前提满足的正面对照 | 无待确认/违规 |

以上每个结论均由 `tests/test_main.cpp` 中的原生断言逐条核验(判定、首处指令
位置、依据关键词、收敛记录),并由 `tools/local_verify.py` 驱动 CLI 复核。

## 限制

- IR 为抽象指令集,不解析真实机器码/源码;变量按名匹配,建议 SSA 风格命名。
- 区间域不跟踪变量间关系(如 `a <= b` 的关联),`shl` 大移位、变量间比较
  的精化按保守处理,可能输出「待确认」而非精确结论。
- 跨函数分析以调用点为上下文、深度 8 为限;函数指针/间接调用不在指令集内。
