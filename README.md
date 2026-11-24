# 发布预检：数据地域与故障切换路径核对组件

## 背景与判定口径

法务核准的数据地域以**数据实际落地位置**为准；链路中只是转发的节点
（缓存读、网关转发等）另行记录，不参与核准判定。

本组件读取发布链路中缓存、异步日志、容灾副本等步骤的地域设置、
继承默认与故障切换条件，按步骤动作（action）逐节点核对，并对每种
数据类型分别给出结论：

- **可经过区域（transitable_regions）**：该数据链路会途经的所有区域，
  无论是否落地；
- **可持久化区域（persistable_regions）**：该数据会实际落盘/留副本的
  区域，必须全部在该数据类型的法务核准清单内；
- **仅途经区域（transit_only_regions）**：只出现转发动作、没有落地的
  区域（= 可经过 − 可持久化）；
- **异地回退落地（cross_region_landings）**：当主路径全程保持在本区，
  而故障切换（回退）分支会在其他区域留下缓存/日志/副本时，返回
  **从数据入口到该副本的完整节点路径与触发条件链**。

### 动作语义（按是否落地分类）

| action | persists | 含义 |
| --- | --- | --- |
| `cache_read` | false | 读缓存，途经不落盘 |
| `cache_write` | true | 写缓存，数据落地 |
| `async_log` | true | 异步日志落盘 |
| `replicate` | true | 容灾副本写入 |
| `forward` | false | 纯转发网关 |

动作表可在配置顶层 `actions` 中扩展或覆盖（只需声明
`{"persists": true|false}`）。未知动作在解析阶段直接报错。

### 地域继承

子配置未显式写 `region` 时，**按父配置的生效地域判定**；根节点缺省时
取顶层 `home_region`（本区）。解析结果中 `StepNode.region_inherited`
标记该地域是否来自继承。

## 配置结构（JSON）

```json
{
  "name": "order-pipeline",
  "home_region": "cn-east",
  "data_types": ["order", "audit"],
  "approvals": {
    "order": ["cn-east", "cn-north"],
    "audit": ["cn-east"]
  },
  "actions": {
    "edge_forward": {"persists": false, "description": "边缘转发"}
  },
  "pipeline": {
    "id": "entry",
    "step": "api-gateway",
    "action": "forward",
    "region": "cn-east",
    "data_types": [],
    "next": {
      "id": "cache",
      "action": "cache_write",
      "on": {
        "cache_down": {
          "id": "cache_dr",
          "action": "cache_write",
          "region": "cn-north"
        }
      }
    }
  }
}
```

- `next`：主路径后继；`on`：故障切换条件到回退子树的映射，可嵌套，
  条件会沿路径累积成触发条件链。
- `data_types` 缺省（空列表）表示该步骤处理全部数据类型；填写后只对
  所列数据生效。节点上引用未在顶层声明的数据类型会报错。
- 每个数据类型必须给出非空的法务核准区域清单。

## 公开 API

```python
from region_preflight import (
    from_dict,          # dict -> PipelineConfig（含结构校验与地域继承解析）
    load_config,        # JSON 文件路径 -> PipelineConfig
    PreflightEngine,    # 引擎：PreflightEngine(config).run() -> PreflightResult
    PreflightConfigError,
    render_text,        # PreflightResult -> 可读文本报告
    to_dict,            # PreflightResult -> JSON 可序列化字典
)

result = PreflightEngine(from_dict(raw)).run()
result.passed                       # 是否无 error
result.reports["order"]             # 单数据类型 DataReport
report.transitable_regions          # 可经过区域
report.persistable_regions          # 可持久化区域
report.transit_only_regions         # 仅途经区域
report.cross_region_landings        # 异地回退落地（路径+触发条件）
landing.path                        # 入口到副本的节点 ID 列表
landing.conditions                  # 如 ["cache[cache_down]"]
landing.approved                    # 该异地落地是否在核准清单内
report.transit_records              # 仅转发节点（另行记录）
report.findings                     # error / warning 明细
```

### 发现（finding）口径

| code | 级别 | 触发条件 |
| --- | --- | --- |
| `UNAPPROVED_PERSISTENCE` | error | 持久化区域不在该数据的法务核准清单 |
| `APPROVED_FAILOVER_LANDING` | warning | 回退分支在异地落地但已核准，需知悉 |
| `MAIN_ROUTE_OUTSIDE_HOME` | warning | 主路径本身离开本区（不阻断，但不再满足“主路径一直在本区”的特殊场景前提） |

CLI 退出码：`0` 通过；`1` 存在 error；`2` 配置本身不合法。

## 运行方法

需要 Python 3.11+，无第三方依赖（仅标准库）。

```bash
# 运行测试（题面指定方式）
python3 -m unittest discover -s tests -v

# 对示例配置做预检
python3 -m region_preflight examples/pipeline.sample.json
python3 -m region_preflight examples/pipeline.sample.json --json
```

## 验证范围（本地合成数据，无网络、无真实目标）

测试共 35 个，全部通过（普通模式与 `python3 -I` 隔离模式均验证）：

- `test_happy_path.py`：全本区正常链路；转发节点只进可经过、不进
  可持久化；仅途经区域集合口径。
- `test_inheritance.py`：多层子配置继承父地域；回退子树继承后由孙节点
  显式覆盖；根节点回退 `home_region`；空地域拒绝；JSON 夹具继承语义。
- `test_failover_paths.py`（题面核心）：主路径全程本区、回退分支异地
  留缓存/副本时，返回入口到副本的路径与触发条件；已核准为 warning、
  未核准为 error 但路径仍返回；回退分支上的转发节点只记录不核准；
  嵌套回退的条件链累积；主路径本身离区时不产生特殊落地记录。
- `test_data_types.py`：每种数据独立的可经过/可持久化集合与独立核准；
  同一异地副本对 A 数据已核准、对 B 数据未核准；未知数据类型、
  缺失核准清单拒绝。
- `test_edge_cases.py`：自定义动作注册、未知动作/重复 ID/空条件/
  空核准清单拒绝、finding 去重、转发与落地步骤交错归类、单节点链路。
- `test_end_to_end.py`：加载 JSON 夹具跑全流程，校验区域分类、
  异地落地路径与条件、转发记录、JSON 可序列化、文本报告关键段落、
  CLI 三种退出码（0/1/2）。

## 目录

```
region_preflight/   组件源码（actions/config/models/preflight/report/cli）
examples/           合成示例配置
tests/              单元与端到端测试、JSON 夹具
```
