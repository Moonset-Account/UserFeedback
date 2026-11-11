# 崩溃报告离线归并工具

同一内存安全缺陷在不同构建中会产生大量崩溃报告，栈上原始地址随加载基址变化、
内联展开层次随编译选项增减，直接按地址或符号字符串匹配会把同一缺陷拆散。
本工具在**离线、纯标准库**条件下，依据

1. 崩溃线程中**可用的首个相关应用帧**（`is_app_frame=true` 的第一帧）；
2. **故障类别**（fault_category）；
3. 经构建映射解析出的**稳定源码归属**（最内层源码文件:行号 + 非内联归属函数）；

形成可解释分组，并把缺少映射或出现多义映射的记录标为证据不足、保留待核查。

## 目录结构

```
merge_reports.py            入口启动器（支持 python3 -I 安全运行）
src/crash_merge/
  models.py                 原因码、稳定归属、成员证据等数据结构
  mapping.py                构建映射索引、地址归一化、归属提取
  resolution.py             单份报告：首帧定位 + 映射解析 + 候选符号交叉验证
  clustering.py             分组聚合、代表记录选择、归并理由生成
  cli.py                    命令行入口
data/                       脱敏样例（报告 + 构建映射），与代码目录分离
tests/test_merge.py         14 项场景验证测试
out/                        运行输出（git 忽略）
```

## 运行

```bash
# 隔离模式：不读取 PYTHONPATH、不把 cwd 隐式加入 sys.path，防止数据目录中的同名模块劫持
python3 -I merge_reports.py \
  --reports data/sample_reports.json \
  --mapping data/sample_build_mapping.json \
  --output out/merge_result.json

# 测试
python3 -I -m unittest discover -s tests -v
```

## 输入格式

构建映射 `sample_build_mapping.json`：按 `module_build_id + module + address` 定位，
每个地址给出完整内联展开链（`inline_depth` 从 0 连续编号，0 为非内联归属函数）。

崩溃报告 `sample_reports.json`：每份报告含模块构建标识、故障类别、崩溃线程帧；
应用帧带候选符号与报告自带的内联层次，用于和映射链交叉印证。

## 归并键与判定规则

- 稳定归属三元组 = `(最内层帧 source_file, 非内联帧 symbol, 最内层帧 source_line)`；
  原始地址不参与归并键。
- 分组键 = `(fault_category, 稳定归属三元组)`。因此同一源码位置的不同故障类别
  （如 heap-buffer-overflow 与 stack-buffer-overflow）各自成组。
- 证据不足原因码：
  - `MISSING_KEY_FRAME`：崩溃线程无应用帧，缺少关键帧；
  - `MISSING_MAPPING`：构建标识未知 / 模块缺失 / 地址查无条目 / 候选符号与映射无交集；
  - `AMBIGUOUS_MAPPING`：同一地址存在多个互不一致的稳定归属；
  - `INVALID_REPORT`：报告结构不完整或 report_id 重复。
- 报告自带内联层次与映射链不一致或缺失时，保留映射解析结论，但置
  `report_corroborated=false` 并在归并理由中点名，不静默忽略。

## 输出

每个分组保留：确定性 `group_id`、稳定归属、成员及各自构建/地址/解析链证据、
按 report_id 选出的代表记录、可逐条复核的中文归并理由；待核查记录给出原因码与
具体原因。完整结果写入 `--output` 指定的 JSON，stdout 同时打印人类可读摘要。

## 样例覆盖的验证场景

| 报告 | 场景 | 结果 |
| --- | --- | --- |
| R01/R02/R03/R06 | 同一 use-after-free，跨 3 个构建，地址不同且 b102 中间内联帧符号变化 | 合为 1 组（R06 标注未印证） |
| R04/R05 | heap-buffer-overflow，frame_buffer.cc:88 | 独立 1 组 |
| R07/R08 | stack-buffer-overflow，同一源码位置、不同故障 | 独立 1 组 |
| R09 | 崩溃线程无应用帧 | 待核查 MISSING_KEY_FRAME |
| R10 | 构建标识在映射中不存在 | 待核查 MISSING_MAPPING |
| R11 | 同一地址两条互不一致的归属链 | 待核查 AMBIGUOUS_MAPPING |
| R12 | 构建已知但地址无映射条目 | 待核查 MISSING_MAPPING |
