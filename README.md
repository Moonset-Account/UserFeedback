# logleak_checker —— 日志敏感信息泄露源码检查组件

面向账号服务源码评审：在评审阶段提前发现**完整邮箱、手机号未经脱敏流入日志**的写法。
基于 Python 标准库 `ast` 的模块内静态污点分析，无第三方依赖，运行时为 Python 3.11.6（`.venv`）。

## 分析能力

对同一模块内的每个函数（含模块顶层、类方法、嵌套函数）跟踪：

- **函数调用**：日志汇聚点、掩码函数、已知安全函数、范围外调用分别处理；
- **局部变量**：赋值/增广赋值/解包/属性与下标写入的污点传播；
- **分支**：`if/else`、`try/except`、循环按分支分别分析后汇合——
  一侧脱敏、另一侧保留原值时，汇合结果仍按原值（TAINTED）处理，
  并保留从敏感参数到日志行的完整证据链；
- **格式化字符串**：f-string、`%` 格式化、`str.format`、字符串拼接均传播污点。

## 判定规则

| 场景 | 结果 |
| --- | --- |
| 敏感参数原值（或经局部变量/格式化字符串间接）进入日志函数 | `sensitive_in_log` 违规，附证据链 |
| 只传入掩码函数返回值的日志调用 | 通过 |
| 与敏感数据无关的日志调用 | 通过 |
| 不在日志/掩码/已知安全配置内的调用 | 独立标为 `manual_review`（待人工确认），不影响违规判定 |
| 范围外调用接收敏感原值且返回值再进日志 | 保守按原值流出，报违规 |

## 实际入口

- **库接口**：
  ```python
  from logleak_checker import analyze_source, analyze_file, AnalyzerConfig, default_config
  findings = analyze_source(source_text)          # 或 analyze_file(path, config)
  ```
  返回 `Finding` 列表（`kind` / `line` / `message` / `evidence`）。
- **命令行**：
  ```bash
  .venv/bin/python -m logleak_checker <文件.py> [...] [--config config.json]
  ```
  存在 `sensitive_in_log` 时退出码为 1，否则为 0；`manual_review` 仅打印。
- **配置**：`AnalyzerConfig`（或 JSON 文件，键同名）标记
  `sensitive_params`（敏感参数名）、`log_functions`（日志函数，支持点分路径或裸名）、
  `mask_functions`（认可的掩码函数）、`known_safe_functions`（已知安全函数）。
  默认值见 `logleak_checker/config.py`。

## 验证范围

`tests/test_analyzer.py`（unittest，18 个用例，全部真实断言）：

- **可接受结果**：掩码值进日志（直接/内联/双分支均脱敏）、非敏感值进日志；
- **应被阻止结果**：原值直接进日志、f-string/`%`/`str.format` 携带原值、
  经局部变量链流入日志，均校验证据链包含参数名与日志行；
- **边界/交错场景**：脱敏分支与原值分支汇合后仍报违规且证据完整、
  缺省 else 的汇合、先赋原值后被掩码覆盖（通过）、
  范围外调用与掩码日志交错（人工确认与违规判定互不干扰）、
  范围外调用结果保守传播、同一调用点去重；
- **配置与范围**：自定义配置生效、未配置参数不污染、同模块多函数独立判定。

运行验证：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/fixtures/sample_violation.py` 为 CLI 冒烟夹具（脱敏/原值分支汇合 + 范围外调用）。

## 目录结构

```
logleak_checker/
  __init__.py     # 库接口导出
  config.py       # AnalyzerConfig 与默认配置、JSON 加载
  analyzer.py     # AST 污点分析核心
  __main__.py     # CLI 入口
tests/
  test_analyzer.py
  fixtures/sample_violation.py
```
