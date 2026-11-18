# 服务低权限基线引擎

对主机管控端导出的**服务定义、覆盖层、平台默认值和进程实测身份**进行分层
解析，核对需要低权限运行的服务是否满足：

1. **启动身份**：配置要求的 `user` / `group` 与进程实测身份一致；
2. **写入范围**：进程实际写入路径必须落在配置允许的写入路径白名单内；
3. **权限继承**：配置身份与进程身份均不得命中禁止的高权限身份
   （平台声明列表 + 内置兜底 `root` / `Administrator` / `SYSTEM`）。

## 目录结构

```
baseline_engine/
  __init__.py      # 公共 API 导出
  models.py        # 数据模型、ListOp、Finding/Report 与状态常量
  merge.py         # 分层合并：标量覆盖 + 列表 merge/replace/remove
  checks.py        # 三项核对与路径包含判定
  engine.py        # 别名解析、覆盖定向、快照匹配、分桶与来源判定
  cli.py           # 命令行入口
tests/
  test_baseline.py # unittest 验收用例（18 个）
examples/
  baseline_input.json    # 合成示例输入
  baseline_report.json   # 示例运行产物
run_baseline.py    # 支持 python -I 隔离运行的本地入口
```

## 实际入口

库 API（`baseline_engine/engine.py`）：

```python
from baseline_engine import evaluate

report = evaluate(json_like_dict)
report.summary            # 各状态桶计数
report.to_dict()          # 可序列化报告
```

命令行：

```bash
# 常规方式
python3 -m baseline_engine.cli --input examples/baseline_input.json

# 隔离方式（推荐用于读取外部导出文件，避免 cwd 被植入模块）
python3 -I run_baseline.py -i examples/baseline_input.json -o report.json
```

退出码：`0` 评估完成（不符合项以报告为准）；`2` 输入配置错误（未知引用、
别名成环、重复快照等）；`1` 参数或文件读取错误。

## 输入结构（JSON 风格）

| 字段 | 说明 |
| --- | --- |
| `platform_defaults` | 平台基础层：默认 user/group、写入路径、禁止身份、是否默认要求低权限 |
| `service_defs` | 服务定义；`aliases` 声明别名，`alias_of` 定义别名转发服务 |
| `overlays` | 覆盖层，按 `target`（服务名或别名）定向 |
| `snapshots` | 进程实测快照；可用规范名或任一别名上报，每个规范服务至多一份 |

### 覆盖层列表语义

列表字段（`write_paths`、`forbidden_identities`）支持三种操作，可写成
简写对象 `{"op": "...", "items": [...]}`：

- `merge`（默认）：与上层列表保序去重合并；
- `replace`：**显式清除上层列表**，以本层内容为准（`items` 可为空）；
- `remove`：从上层列表剔除指定元素。

标量字段（`user`、`group`、`low_privilege_required` 等）遵循“显式给值即
覆盖上层”。分层顺序为：**平台默认值 → 规范服务定义 → 别名定义层（按别名
依赖顺序）→ 覆盖层（按输入顺序）**。

### 别名规则

- 别名引用的服务**按最终合并后的定义判定**，别名本身不单独成报；
- 快照、覆盖层均可使用别名，最终归一到规范服务；
- 别名链成环、别名与规范定义冲突、引用未知服务均抛出
  `BaselineConfigError`（CLI 退出码 2）。

## 判定状态与不符合项来源

| 状态 | 含义 |
| --- | --- |
| `compliant` | 配置与运行现场均符合低权限要求 |
| `pending_remediation` | **配置符合预期，但进程身份/写入现场不符** |
| `noncompliant` | 最终配置本身存在缺陷（如配置为 root、未声明用户/写入范围） |
| `no_snapshot` | 尚无运行快照，单列；已能发现的配置缺陷仍随报告指出 |
| `skipped` | `low_privilege_required=false`，不纳入低权限核对 |

分桶优先级：存在 `config` 来源不符合项 → `noncompliant`；否则存在
`runtime` 来源不符合项 → `pending_remediation`；全部通过 → `compliant`。
每个 finding 都带 `source` 字段（`config` / `runtime`），明确指出问题来自
**配置**还是**运行现场**，两类问题同时存在时各自保留、不会互相吞掉。

路径判定按目录前缀做包含检查（不访问文件系统）：实际路径等于允许路径或
位于其下即合规；同前缀兄弟目录（如允许 `/var/app/edge` 时写入
`/var/app/edge-extra/x`）判越界。

## 验证范围与运行方式

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_baseline.py` 共 18 个用例，全部执行真实断言，覆盖题面交叉约束：

- **可接受结果**：配置与快照一致 → `compliant`；免低权限服务 → `skipped`；
- **应被阻止/拒绝的结果**：配置 root → `noncompliant` 且来源为 `config`；
  别名成环、未知覆盖/快照引用 → 拒绝并抛出异常；
- **待整改**：进程用户/用户组不符、写入越界 → `pending_remediation`，
  来源为 `runtime`；
- **无快照单列**：`no_snapshot` 桶并保留已识别的配置缺陷；
- **覆盖层交错边界**：merge 去重、remove 剔除、replace 显式清除上层列表
  多覆盖层按顺序生效；JSON 对象形式覆盖项解析；
- **别名场景**：别名快照归一、别名定义层参与合并、定向别名的覆盖层作用于
  规范服务、别名链成环拒绝；
- **写入范围边界**：恰好等于允许路径放行、兄弟目录越界拦截；
- **来源归属与分桶优先级**：config 与 runtime 问题并存时归入
  `noncompliant` 但两类 finding 均保留；
- **CLI 端到端**：真实子进程外的 `main()` 调用，断言退出码、报告文件内容
  与错误输入退出码 2。

所有夹具均为测试内或 `examples/` 下的本地合成数据，不包含任何真实主机
探测、攻击或漏洞利用内容。
