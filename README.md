# fixcheck — 发版前修复符号来源核对工具

紧急修复合入后，增量构建可能仍打包旧对象文件。本工具读取四类本地构建记录，
按构建目标判断发布包中的修复符号是否来自正确的源码修订。

## 构建与测试

```sh
./tests/run_tests.sh        # 编译并跑黄金输出测试
```

## 用法

```sh
fixcheck <fix_manifest> <compile_units> <archive_members> <link_symbols>
```

退出码：0 = 全部修复符号来源正确；1 = 存在异常；2 = 输入格式错误。

## 输入记录格式（行式，空白分隔，`#` 为注释）

| 记录 | 格式 | 说明 |
|------|------|------|
| FIX | `FIX <id> <目标> <源文件> <修复后摘要> <符号1,符号2,...>` | 修复源文件摘要清单 |
| CU  | `CU <id> <目标> <对象文件> <源文件> <源摘要>` | 编译单元记录 |
| AR  | `AR <id> <目标> <归档> <成员名> <对象文件>` | 归档成员 |
| SYM | `SYM <id> <目标> <符号> <提供对象>` | 链接符号归属记录 |

## 结论分类

- `OK` — 提供对象的源摘要与修复修订一致
- `STALE_OBJECT` — 链接选中的对象编译自旧修订（含同名归档成员覆盖检测）
- `MISSING_UNIT` — 编译单元缺失：修复源未编译、未归档或未链接进目标
- `AMBIGUOUS` — 来源无法唯一确认：多条冲突归属、缺编译记录、或符号由其他源的对象提供

每条结论附 `依据记录`（输入记录编号），用于定位待清理的缓存对象与待重建目标。

## 判定流程（每个目标 × 每个修复符号）

1. 查 SYM：无记录 → `MISSING_UNIT`（再细分未编译/未归档/未链接）；
   多个不同提供对象 → `AMBIGUOUS`。
2. 由提供对象查 CU：无记录或摘要冲突 → `AMBIGUOUS`；
   编译源不是修复源文件 → `AMBIGUOUS`。
3. 比对 CU 源摘要与 FIX 修复摘要：一致 → `OK`；不一致 → `STALE_OBJECT`，
   并检查同一归档中同名成员是否指向新对象（旧对象覆盖新对象）。

## 测试场景覆盖（tests/data/）

- `app/fix_crypto_verify` — 正常修复生效
- `app/fix_net_reconnect` — 同名归档成员 `net.o` 的旧对象覆盖新对象（AR:A1/A2）
- `app/fix_json_parse, fix_json_parse_alias` — 多个符号别名指向同一对象
- `app/fix_util_clamp` — 优化后符号改由 `unity_util.o` 提供
- `app/fix_log_redact` — 两条冲突归属记录 → 来源无法唯一确认
- `firmware/fix_fs_journal` — 编译单元缺失
- `tools/fix_net_reconnect` — 不同目标中同名归档成员 `net.o` 解析到各自对象
