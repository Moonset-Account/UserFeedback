# 隔离状态模块（SEC-20261114-008）

控制台侧的设备隔离/解除隔离状态跟踪模块。针对的场景：隔离命令发出后终端可能先掉线，
之后把多次操作的回执一并补上来，回执顺序与发送顺序无关。

## 运行环境

- Python 3 标准库，无第三方依赖。
- 本地可重跑入口（在项目根目录执行）：

```bash
/usr/local/bin/python3 app.py demo                                  # 内置演示
/usr/local/bin/python3 app.py run samples/out_of_order_events.jsonl # 乱序回执样例
/usr/local/bin/python3 app.py run samples/resend_after_failure.jsonl# 失败重发样例
/usr/local/bin/python3 tools/local_verify.py                        # 核验入口（单测+样例断言）
```

## 核心语义

- 每次操作请求分配全局递增的**命令编号 cmd_id** 和按设备递增的**操作序号 op_seq**。
- 控制台视图包含三项：**期望状态**（最新意图）、**最后确认状态**（已确认的最大 op_seq
  对应的状态）、**仍待确认的命令**（已发送未回执）。
- 成功回执仅当其 op_seq 大于已确认序号时才推进最后确认状态；先前操作的成功回执只
  补全历史（该发送记录标记成功），**不覆盖**已确认的更新操作。
- 失败回执只标记该次发送失败，不改变确认状态；此时可**重发当前意图**：沿用原
  op_seq、分配新 cmd_id，发送历史完整保留。当前意图已被确认后禁止重发。
- 未知设备、未知命令编号、重复回执均显式报错。

## 事件流输入格式（JSONL）

每行一个 JSON 事件，`#` 开头与空行忽略：

```json
{"type": "send",    "device": "dev-01", "action": "isolate" | "release"}
{"type": "resend",  "device": "dev-01"}
{"type": "receipt", "device": "dev-01", "cmd_id": 2, "result": "success" | "failed"}
```

## 输出语义

- 每个事件处理后输出一行处理结果和一行控制台视图（期望状态/最后确认状态/待确认命令）。
- 结尾对每台设备输出稳定可机器校验的汇总行：
  `FINAL device=<id> expected=<期望状态> confirmed=<最后确认状态> pending=<待确认数>`。

## 需求演示结论

`samples/out_of_order_events.jsonl`（先隔离 cmd1/op1，再解除 cmd2/op2，解除回执先到）：

1. 解除回执(cmd 2)先到 → 最后确认状态推进为 `released`(op2)，cmd 1 仍待确认；
2. 隔离回执(cmd 1)后到 → 仅补全历史，不覆盖已确认的 `released`；
3. **最终设备状态：期望=released，最后确认=released，待确认=0（设备处于解除隔离状态）。**

## 需求 → 测试/样例映射

| 需求点 | case_id | 位置 |
| --- | --- | --- |
| 记录每次请求的命令编号与操作序号 | REQ-01 | tests/test_isolation.py::RequestNumberingTest |
| 控制台显示期望/最后确认/待确认命令 | REQ-02 | tests/test_isolation.py::ConsoleViewTest |
| 解除回执先到即确认 released | REQ-03 | tests/test_isolation.py::OutOfOrderReceiptTest::test_req03 |
| 旧成功回执补历史不覆盖新确认 | REQ-04 | 同类 test_req04 / test_req04b；SAMPLE-02/06 |
| 失败重发沿用原操作序号并保留历史 | REQ-05 | tests/test_isolation.py::ResendTest；SAMPLE-03/05 |
| 未知命令/未知设备回执报错 | REQ-06 | tests/test_isolation.py::BoundaryTest::test_req06* |
| 重复回执报错 | REQ-07 | 同类 test_req07 |
| 意图已确认后禁止重发；无意图禁止重发 | REQ-08 | 同类 test_req08* |
| 本地事件流演示与最终设备状态 | REQ-09 | tests/test_isolation.py::CliDemoTest；SAMPLE-01/04 |

## 限制

- 纯本地内存实现，不持久化；进程退出后状态丢失。
- 事件流为合成离线输入，不连接真实终端、云平台或生产系统。
- 命令编号在单次进程内全局递增；跨进程不保证连续。
- 回执必须携带本进程内已分配的 cmd_id，否则按未知命令报错。
