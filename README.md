# 云存储匿名访问离线预检

提交云存储配置前，判断哪些对象前缀会向匿名请求开放。模块完全离线：
只读取本地快照文件，不做任何网络请求或目标探测。

## 实际入口

```bash
python3 -m precheck <snapshot_dir>          # CLI，报告输出到 stdout（JSON）
python3 -m unittest discover -s tests -v    # 验收测试
```

运行时为 Python 3.11.6（`python3 --version` 验证），仅使用标准库。

## 快照输入（snapshot_dir 下四个 JSON 文件）

| 文件 | 内容 |
| --- | --- |
| `defaults.json` | 继承的默认设置：`inherited.block_public_access`、资源级 `overrides`、已批准分发入口 `approved_distribution_endpoints`、快照内已知条件属性 `known_attributes` |
| `org_boundary.json` | 组织边界语句（Allow/Deny、principal、actions、resources、condition） |
| `resource_policies.json` | 各桶资源策略语句，结构同上 |
| `prefixes.json` | 待评估的对象前缀清单 |

示例快照见 `examples/snapshot/`。

## 评估规则（交叉约束）

1. **组织显式拒绝覆盖资源放行**：组织边界中的 Deny 优先于资源策略中的 Allow。
2. **分发入口不连带直连**：放行对象为分发服务身份（`principal.type=service`）时，
   只计入“经批准分发入口的访问”，不开放匿名直连；且分发入口必须在
   `approved_distribution_endpoints` 清单内，否则该放行被忽略。
3. **部分公开给出范围与依据**：放行范围与目标前缀取交集，逐范围给出
   `allowed` / `denied` / `pending` / `partially_public` 状态及来源语句依据。
4. **快照外属性标待确认**：语句 `condition.attributes` 不在 `known_attributes`
   内时，相应范围标记为 `pending`（放行与拒绝的条件均如此）。

每个前缀输出 `direct`（匿名直连）与 `distribution`（经批准分发入口）两组结果，
状态取值为 `public` / `partially_public` / `denied` / `pending`。

## 验证范围（tests/test_precheck.py，12 个用例）

- **可接受结果**：前缀整体公开（资源级覆盖关闭默认阻止 + 匿名放行）；
  经批准分发入口可访问。
- **应被阻止/拒绝结果**：组织显式拒绝覆盖资源放行；继承默认
  `block_public_access=true` 阻止公开；未批准分发入口的放行被忽略；
  服务身份放行不开放匿名直连。
- **边界/交错场景**：前缀部分公开时的公开子范围与依据；条件依赖快照外
  属性（如 `aws:SourceIp`）时标记待确认；待确认的拒绝条件同样导致待确认；
  组织拒绝收窄资源放行范围后其余部分仍公开；示例快照端到端跑通 CLI 入口。
