# Mach-O 离线编译缓解检查器（SEC-VULN-011）

一个纯 C++17、零第三方依赖的 Mach-O / universal（fat）二进制**离线**检查器。
不加载、不执行被检查文件的任何代码；对每个架构切片报告文件类型、
位置无关可执行（PIE）标志、签名载荷记录（LC_CODE_SIGNATURE）是否存在，
以及其偏移/长度是否完全落在文件内部。

## 目录结构

    src/macho_inspect.h     公共数据结构与 API
    src/macho_inspect.cpp   Mach-O / fat 解析、端序处理、规则判定、报告渲染
    src/main.cpp            命令行前端
    tests/test_inspector.cpp 原生 C++17 测试（内含最小 Mach-O 合成夹具构建器）
    tools/local_verify.py   本地编译/运行包装器（Python3 标准库，argv 调用 clang++）
    build/                  临时编译产物与测试夹具（不入库）

## 构建（固定工具链）

    /usr/bin/clang++ -std=c++17 -Wall -Wextra -Werror \
        src/macho_inspect.cpp tests/test_inspector.cpp -o build/test_inspector
    /usr/bin/clang++ -std=c++17 -Wall -Wextra -Werror -c src/macho_inspect.cpp -o build/macho_inspect.o
    /usr/bin/clang++ -std=c++17 -Wall -Wextra -Werror -c src/main.cpp -o build/main.o
    /usr/bin/clang++ -std=c++17 build/main.o build/macho_inspect.o -o build/macho_inspect

或使用包装器（内部仅以 argv 数组调用 /usr/bin/clang++，重新编译后再运行，
不复用旧二进制，不使用 shell=True，不联网）：

    python3 tools/local_verify.py

## 输入

一个或多个文件路径。文件按字节整体读入内存，依据前 4 字节魔数判定：

- `0xfeedface / 0xcefaedfe`     32 位 Mach-O（含端序交换形式）
- `0xfeedfacf / 0xcffaedfe`     64 位 Mach-O（含端序交换形式）
- `0xcafebabe / 0xbebafeca`     32 位 fat 通用包（big-endian 描述符）
- `0xcafebabf / 0xbfbafeca`     64 位 fat 通用包
- 其他魔数                      记为无法判定（U003）

## 输出语义（稳定行格式）

    file: <路径>
    container: thin Mach-O | universal (fat)
    slices: <切片数>
    [容器级 finding ...]
    slice[i] arch=<架构> slice_off=<绝对偏移> slice_size=<长度>
      filetype=<类型> flags=0x<头部flags> pie=yes|no pie_policy=applies|n/a
      code_signature_load=present|absent payload_in_bounds=yes|no \
        payload_off=<绝对偏移> payload_size=<长度>
      <缩进 finding ...>

每条 finding 一行：

    <级别> <规则号> evidence_off=<证据绝对偏移> range=[<起始>,+<长度>) - <说明>

三类结果严格分开：

| 级别           | 含义                                       |
|----------------|--------------------------------------------|
| VIOLATION      | 策略规则未满足（应用/动态库策略）          |
| CORRUPTION     | Mach-O 或 fat 容器结构损坏                 |
| INDETERMINATE  | 无适用策略或容器无法识别，属性无法判定     |

## 策略与规则编号

应用（MH_EXECUTE）与动态库（MH_DYLIB）是策略适用对象；其余文件类型
（MH_OBJECT、MH_BUNDLE、MH_DSYM 等）不适用应用/动态库策略，记 U001。

| 规则  | 级别          | 判定 |
|-------|---------------|------|
| R001  | VIOLATION     | MH_EXECUTE 必须置 MH_PIE(0x200000)；动态库不要求 PIE |
| R002  | VIOLATION     | MH_EXECUTE/MH_DYLIB 必须存在 LC_CODE_SIGNATURE 加载命令 |
| R003  | CORRUPTION    | LC_CODE_SIGNATURE 的 dataoff/datasize 必须非零且完全在文件内 |
| F001  | CORRUPTION    | 切片魔数不可读或非法、Mach-O 头部被截断 |
| F002  | CORRUPTION    | sizeofcmds 声明的加载命令表越出文件末尾（截断的加载命令表） |
| F003  | CORRUPTION    | 单条加载命令 cmdsize<8 或超出命令表边界（截断的加载命令） |
| F004  | CORRUPTION    | fat 描述符表截断，或切片 offset/size 越界（越界的切片描述） |
| U001  | INDETERMINATE | 文件类型无应用/动态库策略 |
| U002  | INDETERMINATE | 空文件 |
| U003  | INDETERMINATE | 魔数无法识别 |

**签名检查边界**：只判断 LC_CODE_SIGNATURE 这一载荷*元数据*
（命令是否存在、dataoff/datasize 是否在界内），不解析也不密码学验证
SuperBlob/CMS 签名内容本身。

CLI 退出码为位域：`0` 干净/仅无法判定；`1` 存在 VIOLATION；
`2` 存在 CORRUPTION；`3` 两者皆有；参数错误为 `64`。

## 测试用例（tests/test_inspector.cpp，合成最小 Mach-O）

| case_id | 构造 | 关键断言 |
|---------|------|----------|
| TC01-thin-exec-compliant | 单架构 x86_64 MH_EXECUTE，MH_PIE + 界内签名载荷 | 无任何 finding，pie/yes、signature present、in-bounds |
| TC02-fat-one-slice-missing-pie | fat 双切片，x86_64 合规、arm64 缺 MH_PIE 且无签名命令 | arm64 报 R001(flags 绝对偏移=切片+24, 4字节) 与 R002；x86_64 干净 |
| TC03-truncated-load-command | 声明 16 字节命令但文件只保留 6 字节命令体 | F003 CORRUPTION，evidence_off=36，range 起始=32 |
| TC04-fat-slice-descriptor-out-of-bounds | 第二切片描述符指向 0x100000、长度 0x1000 的越界区 | 该切片 uninspected，F004，evidence_off=36，range=[0x100000,+0x1000) |
| TC05-signature-payload-out-of-bounds | dylib 的签名 datasize=0x4000 超出 EOF | R003 CORRUPTION，evidence_off=40，range_length=0x4000 |
| TC06-dylib-no-pie-valid-signature | arm64 MH_DYLIB 无 PIE 但签名界内 | 无 finding；PIE 策略 n/a |
| TC07-thin-exec-missing-pie-and-signature | 薄 MH_EXECUTE 无 PIE 无签名 | R001 + R002 |
| TC08-object-file-no-policy | MH_OBJECT | U001 INDETERMINATE，无 violation/corruption |
| TC09-unrecognised-magic | 魔数 0xdeadbeef | 容器级 U003 |
| TC10-truncated-macho-header | 64 位头部截到 20 字节 | F001，切片 uninspected |
| TC11-fat-descriptor-table-truncated | nfat=3 但只有 1 个描述符 | 容器级 F004，evidence_off=4 |
| TC12-sizeofcmds-extends-past-eof | sizeofcmds=0x10000 越界 | F002，evidence_off=20，range 起始=32 |

所有偏移断言均使用文件内**绝对偏移**；fat 用例的切片基址由容器实际
布局计算（8 字节头 + 每描述符 20 字节）。断言数在运行结束打印
`checks=N failures=M`，任一失败则进程非零退出。

## 限制

- 仅静态解析，不执行、不加载目标；不校验签名密码学有效性。
- 策略聚焦 MH_EXECUTE/MH_DYLIB；其他类型按“无法判定/不适用”处理。
- 加载命令表一旦判定越界或截断即停止该切片后续策略评估，避免基于
  残缺结构产生不可靠结论（相应缺口以 CORRUPTION finding 体现）。
