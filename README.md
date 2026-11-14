# SEC-20261114-040 — 公开错误映射与内部诊断字段脱敏

错误响应映射层：支持人员可凭链路编号查后台详情，用户侧只看到短消息。
纯 Python 3 标准库实现，无第三方依赖。

## 功能

- 输入：结构化错误对象（dict）、接口名、请求标识、公开错误码登记配置。
- 已登记错误码 → 客户端收到配置的固定文案；未知错误 → 通用文案 +
  `INTERNAL_ERROR`，不泄露内部错误码与内部 message。
- 内部诊断输出保留嵌套 `cause` 异常原因、`env` 环境变量字段、`stack`
  堆栈，但按字段规则脱敏：
  - 错误对象中 `secret_fields` 显式标记的字段（点分路径或裸字段名）；
  - 命中默认秘密模式的字段名（password / token / secret / api_key /
    authorization / credential 等，大小写不敏感）；
  - 脱敏值统一替换为 `***REDACTED***`。
- 链路编号（`trace_id`）在客户端与内部输出中保持一致。
- 循环引用替换为 `{"__truncated__": "circular-reference"}`；嵌套深度
  超过 `MAX_DEPTH=8` 的部分替换为
  `{"__truncated__": "max-depth-exceeded"}`，输出始终可 JSON 序列化。

## 本地入口

```bash
# 一键核验（单测 + 样例/边界断言）
/usr/local/bin/python3 tools/local_verify.py

# 内置两模块嵌套错误示例（模块A支付服务包装模块B订单存储错误）
/usr/local/bin/python3 app.py --demo

# 自定义输入
/usr/local/bin/python3 app.py --interface payments.charge --request-id req-123 \
    --registry examples/registry.json --error error.json
cat error.json | /usr/local/bin/python3 app.py --interface x --request-id r --error -

# 样例/边界断言（逐条 CASE 输出）
/usr/local/bin/python3 examples/run_demo.py
```

## 输入格式

结构化错误对象（JSON object），字段均可选：

```json
{
  "code": "PAYMENT_DECLINED",
  "message": "内部诊断消息",
  "cause": { "... 嵌套错误对象 ..." },
  "env": {"DB_PASSWORD": "...", "APP_ENV": "prod"},
  "stack": ["module.func:line"],
  "secret_fields": ["env.DB_PASSWORD"]
}
```

登记配置（`--registry`，见 `examples/registry.json`）：

```json
{
  "generic_message": "系统繁忙，请稍后重试",
  "unknown_code": "INTERNAL_ERROR",
  "mappings": {
    "PAYMENT_DECLINED": {"client_code": "PAYMENT_DECLINED",
                         "client_message": "支付未成功，请更换支付方式后重试"}
  }
}
```

## 输出语义

`app.py` 向 stdout 打印：

```json
{
  "client":   {"ok": false, "error": {"code": ..., "message": ..., "trace_id": ...}},
  "internal": {"trace_id": ..., "interface": ..., "error": { "... 脱敏错误树 ..." }}
}
```

- `client`：用户侧短消息，只含对外错误码、固定/通用文案、链路编号。
- `internal`：支持/后台诊断视图，含接口名与脱敏后的完整错误树；
  与 `client` 的 `trace_id` 相同，可据此关联后台详情。

## 需求 → 测试/样例映射

| 需求 | 覆盖 |
| --- | --- |
| 已登记错误映射固定文案 | `tests/test_mapper.py::TestRegisteredMapping`，demo `demo.client_fixed_message` |
| 未知错误通用文案 | `tests/test_mapper.py::TestUnknownMapping`，`tests/test_cli.py::TestCliErrorFile` |
| 嵌套原因/环境变量/堆栈供内部诊断 | `tests/test_mapper.py::TestInternalViewContent`，demo `demo.internal_*` |
| 内部输出按字段规则脱敏 | `tests/test_redact.py`，demo `demo.internal_redacts_*` |
| 链路编号两类输出一致 | `tests/test_mapper.py::TestTraceIdConsistency`，demo `demo.trace_id_consistent` |
| 循环引用/超深链有界截断 | `tests/test_bounds.py`，demo `boundary.circular_truncated`、`boundary.deep_chain_truncated` |
| 两模块嵌套错误本地示例 | `examples/modules.py` + `examples/run_demo.py`，`tests/test_cli.py::TestCliDemo` |

## 限制

- 脱敏基于字段名规则（显式标记 + 默认模式），不做字段值内容扫描；
  未命中规则的秘密值不在保护范围内。
- 深度上限固定为 `MAX_DEPTH=8`（`errmap/redact.py`），截断处只保留
  标记不保留原始内容。
- 非 JSON 原生类型在内部输出中转为有界 repr 字符串。
- 仅本地离线运行，不连接任何外部系统。
