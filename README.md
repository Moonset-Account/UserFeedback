# release_gate — 临时豁免累计开放额度发布判定模块

风险委员会为临时豁免设定**累计开放额度**。本模块读取环境范围、控制项、
豁免生效区间和实际启停事件，在指定考核窗口内以**真实开放区间**扣减额度：

- 同一控制项拆成多次申请，消耗的是同一条额度（额度键 = `环境 ID + 控制项 ID`，窗口内累计）；
- 重叠豁免的开放时间只算一次（区间并集）；
- 尚未到期但已收回（发生实际 CLOSE）的区间，在实际关闭时结束；
- 新申请的预计开放区间并入当期已用区间，超过额度即阻止打开对应环境，
  并返回**导致超量的具体时间段**；
- 支持用 `as_of` 冻结到任意历史时点做判定，事后更正事件后可复核过去的决定。

## 目录与入口

```
release_gate/
  intervals.py   半开区间 [start, end)：裁剪、并集合并、交集、超量尾部、时长解析
  models.py      GateEvent / Exemption / ControlQuota / NewApplication / EvaluationBundle
  engine.py      核心判定：real_open_segments、evaluate_application、evaluate_bundle
  loader.py      JSON 输入加载与引用一致性校验
  review.py      决定落盘（原子写）与事后复核 DecisionRepository
  cli.py         命令行实现
release_gate_cli.py   根目录 CLI 入口（兼容 python3 -I）
run_tests.py          测试入口（兼容 python3 -I）
examples/bundle.json  示例输入（含一条可放行、一条应阻止的申请）
tests/                unittest 验收用例（41 个）
```

### Python API

```python
from release_gate import load_bundle_file, evaluate_bundle, DecisionRepository

bundle = load_bundle_file("examples/bundle.json")
result = evaluate_bundle(bundle)
for decision in result.decisions:
    print(decision.application_id, decision.allowed, decision.reason,
          decision.overage_segments)

# 决定连同输入快照落盘，事后用更正后的输入复核
repo = DecisionRepository("decisions.json")
repo.save(result, snapshot_dict)
report = repo.review(corrected_bundle)          # 默认沿用原 as_of
# report.has_changes / report.items[*].changed / recomputed 决定
```

### 命令行

```bash
# 判定；退出码 0=全部放行，1=存在阻止，2=输入错误
python3 -I release_gate_cli.py evaluate examples/bundle.json --store decisions.json

# 事后更正后复核；退出码 0=结论无变化，1=存在翻转，2=输入错误
python3 -I release_gate_cli.py review corrected.json --store decisions.json
```

普通环境（不带 `-I`）也可以使用 `python -m release_gate evaluate ...`。

### 运行测试

```bash
python3 -I run_tests.py
```

## 关键语义

1. **时间模型**：全部使用时区感知 UTC 时间；区间为左闭右开 `[start, end)`，
   相邻区间合并后不产生空隙、不重复计时。朴素 datetime 按 UTC 处理。
2. **真实开放区间**（`real_open_segments`）：在豁免生效区间内按时间排序
   配对 OPEN/CLOSE；OPEN 后无 CLOSE 时开放到 `min(生效结束, as_of, 窗口结束)`；
   窗口外与 `as_of` 之后的事件一律忽略。重复 OPEN、无匹配 CLOSE、事件越过
   生效边界等异常数据不静默吞掉，而是写入 `warnings` 并按生效区间收敛。
3. **额度扣减**：先把每条豁免的真实开放区间裁剪到考核窗口，再按
   `(环境, 控制项)` 做并集合并求已用时长；新申请的预计区间同样裁剪窗口后
   并入。`合并后时长 > 额度` 才阻止（恰等于额度放行）。
4. **超量时间段**：按时间顺序跳过已消耗额度后，预计开放区间中落在额度之外
   的部分，即 `overage_segments`，精确到秒。
5. **批次内顺序扣减**：同一批次多份申请按输入顺序判定，前一份被放行的申请
   立即占用额度供后续申请扣减；被阻止的申请不占用额度。
6. **防御性拒绝**：环境不在窗口环境范围内（`ENV_OUT_OF_SCOPE`）、未配置
   额度（`QUOTA_NOT_CONFIGURED`）、预计区间裁剪后为空（`PROJECTION_EMPTY`）
   均默认阻止；加载器还会拒绝悬空事件引用、环境/控制项不一致、重复 ID 等。
7. **事后复核**：每条决定保存时附带完整输入快照；复核默认沿用原快照的
   `as_of`，保证“站在当时的时点”重算，只反映被更正数据造成的差异，也可
   显式传入新的复核时点。

## 输入 JSON 结构

```json
{
  "window": {"start": "2026-11-01T00:00:00Z", "end": "2026-11-11T00:00:00Z"},
  "as_of": "2026-11-05T12:00:00Z",
  "environments": ["prod-eu"],
  "quotas": [
    {"env_id": "prod-eu", "control_id": "C-01", "max_open_duration": "PT12H"}
  ],
  "exemptions": [
    {"exemption_id": "EX-1", "env_id": "prod-eu", "control_id": "C-01",
     "effective_start": "...", "effective_end": "..."}
  ],
  "events": [
    {"event_id": "EV-1", "exemption_id": "EX-1", "env_id": "prod-eu",
     "control_id": "C-01", "action": "OPEN", "occurred_at": "..."}
  ],
  "applications": [
    {"application_id": "APP-1", "env_id": "prod-eu", "control_id": "C-01",
     "projected_intervals": [{"start": "...", "end": "..."}]}
  ]
}
```

- 时长支持 ISO-8601（`PT1H30M`、`P2D`）或数字秒；
- 区间支持 `{start, end}` 对象或 `[start, end]` 数组；
- `action` 大小写不敏感，内部规范化为 `OPEN`/`CLOSE`。

## 测试验证范围（tests/，41 个用例，全部含真实断言）

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_intervals.py` | 重叠/相邻合并、并集去重、窗口裁剪、交集、超量尾部、时长解析、非法区间拒绝、UTC 规范化 |
| `test_engine.py` | **可接受结果**：合并后恰等于额度的边界放行；**应被阻止**：超 1 秒即阻止并给出超量段、拆分申请共享额度、重叠豁免只算一次；**边界/交错**：未到期已收回在实际关闭时结束、`as_of` 冻结历史（同一输入不同时点结论相反）、窗口裁剪、跨环境额度隔离、环境越界/缺额度的防御性拒绝、异常事件告警 |
| `test_review.py` | 决定落盘往返、补录缺失 CLOSE 后复核翻转、无影响更正保持原决定、显式复核时点、更正包缺申请时报错 |
| `test_loader.py` | 动作/数组区间规范化、悬空事件/环境不一致/越界额度/重复 ID/缺字段/非法动作拒绝、示例数据端到端判定 |
| `test_cli.py` | CLI 判定退出码与落盘、更正后复核翻转、错误输入退出码 |

所有测试只使用标准库与工作区内合成夹具，不发起任何网络访问或真实目标探测。
