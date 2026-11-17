# imgplan — 图片代理的显式连接计划

把"允许访问的目标"从一个 URL 变成一份**明确、不可变、带有效期的连接计划**，
并让连接适配器在结构上只能使用计划内的地址。解析与时钟均为注入式适配器，
包本身不发起任何真实网络请求（`socket`/`ssl`/HTTP 客户端/子进程均未导入，
见 C04 的静态扫描断言）。

## 目录

```
imgplan/
  __init__.py    公共 API 导出
  errors.py      带稳定 code 的异常类型
  domains.py     标签化域名规则（exact / .suffix / *.wildcard）
  netpolicy.py   禁止网段判定，含 IPv4 映射 IPv6 的处理
  resolver.py    解析适配器协议 + 可运行时改写的内存适配器
  clock.py       可控时钟（FakeClock / SystemClock）
  planner.py     核心规划器与不可变 ConnectionPlan
  connector.py   被计划钉死的连接适配器
  session.py     逐跳重定向会话（每跳独立规划）
tools/
  local_verify.py  本地核验入口（--list / --case）
examples/
  rebinding_demo.py 可重放的 DNS 重绑定演示
```

## 安全模型与实际 API

### 1. 输入

```python
ConnectionPlanner(
    allow_list: DomainAllowList,   # 域名允许规则
    forbidden:  ForbiddenNetworks, # 禁止的 IP 网段（v4/v6）
    resolver:   ResolverAdapter,   # 返回带 TTL 的 CNAME 链与全部地址
    clock:      Clock,             # 可控时钟
)
plan = planner.plan_url("https://img.example.org/a.jpg")
```

解析适配器只需实现 `resolve(hostname) -> Resolution`：

```python
Resolution(hostname, cname_chain=("img.example.org", "edge.example.org"),
           addresses=("203.0.113.10", "2001:db8::1"), ttl_seconds=30)
```

### 2. 域名按标签匹配（`domains.py`）

| 规则 | 含义 | 命中示例 | 不命中示例 |
|---|---|---|---|
| `cdn.example.com` | 精确匹配 | `cdn.example.com` | `x.cdn.example.com` |
| `.media.example.org` | 点后缀，含自身与任意深度子域 | `media.example.org`、`a.b.media.example.org` | `xmedia.example.org`（标签边界） |
| `*.img.example.net` | 单标签通配 | `a.img.example.net` | `img.example.net`、`a.b.img.example.net` |

匹配始终按完整 DNS 标签、大小写不敏感、忽略末尾点。部分标签通配
（`*cdn...`、`foo.*.com`）在构造规则时直接抛 `InvalidRuleError`。
主机名要求至少两个标签、单标签 ≤63 字符、仅允许字母数字与连字符且
连字符不居首尾；**不做 IDNA/punycode 编码**，调用方需传入 ASCII 名。

### 3. 完整 CNAME 链与全地址集合授权（`planner.py`）

- `cname_chain` 上的**每一个名字**都必须通过允许列表，任一不匹配即抛
  `DomainRuleError(code="domain_not_allowed")`，异常带 `offender`。
- 地址集合中**任一项**落入禁止网段即整体拒绝，抛
  `ForbiddenAddressError(code="forbidden_address")`，异常带 `offending`
  （归一化后的违规地址列表）。不是只检查最终选中的那一个 IP。
- **IPv4 映射 IPv6**（`::ffff:a.b.c.d`）按内嵌 IPv4 地址、针对 IPv4 网段
  判定（`netpolicy.effective_ip` / `is_ipv4_mapped`），因此无法用 IPv6
  形态绕过对 `169.254.0.0/16`、`10.0.0.0/8` 等网段的封禁；计划内地址也
  统一归一化为 IPv4 形态存储。
- 规划器**无缓存**：每次 `plan_url` 都重新调用解析适配器并重跑全部检查。

### 4. 成功计划的内容（`ConnectionPlan`，frozen dataclass）

`plan_id`、`url`、`scheme`、`request_host`、`cname_chain`、`port`、
`host_header`、`server_name`（SNI，始终为裸主机名）、
`approved_addresses`（去重排序后的**全部**批准地址元组）、
`selected_ip`（确定性选择：IPv4 优先，再按数值排序取首项）、
`ttl_seconds`、`created_at`、`expires_at = created_at + ttl_seconds`。

计划不可变：frozen dataclass + 元组字段，授权后无法被原地篡改。
端口规则：`http`→80、`https`→443；显式端口进入 `Host` 头
（`host:port`），SNI 仍为裸主机名。带 userinfo、非 http(s) scheme、
字面量 IP 目标的 URL 一律 `InvalidUrlError(code="invalid_url")`。

### 5. 连接适配器只能使用计划地址（`connector.py`）

```python
connector = PlannedConnector(plan, clock)
conn = connector.connect()                       # 拨 selected_ip
conn = connector.connect("203.0.113.10")         # 仅当属于批准集合
```

- 计划过期（`clock.now() >= expires_at`，边界即失效）后任何连接都抛
  `PlanExpiredError(code="plan_expired")`，必须重新规划。
- 拨计划外地址（含禁止地址、重绑定后的新地址）抛 `PlanError`。
- `connect(resolver=...)` 模拟"连接前再查一次 DNS"的客户端行为：新解析
  结果只能**收窄**候选，凡不在批准集合内的新地址一律拒绝；若新结果已不
  含任何批准地址，也拒绝并要求重新授权。重查永远不能替换已批准 IP。

### 6. 重定向逐跳重新规划（`session.py`）

`PlannedSession.follow(url)` 每跳都调用一次 `planner.plan_url`：不继承上
一跳的解析结果、计划或连接器；上一跳允许不代表下一跳允许。每跳产生独立
`plan_id`，被拒绝的跳返回 `allowed=False` 与 `rejection_code`，不影响既
有计划。

### 7. 可变化的内存适配器（`resolver.InMemoryResolver`）

`set_record(...)` 建立记录，`replace_addresses(...)` 可在运行时改写同一
主机名的地址集合以模拟 DNS 重绑定；`call_count` / `call_sequence` /
`total_calls` 用于审计"确实重新查询了域名"。

## 核验命令

使用工作区 `.venv`（`python3` 已在 PATH 上指向它），无需安装任何包：

```bash
python3 tools/local_verify.py --list
python3 tools/local_verify.py --case C01     # 单用例
python3 tools/local_verify.py --case all     # 全部
python3 -I tools/local_verify.py --case C04  # 隔离模式同样可跑
```

退出码：全部通过 `0`；任一断言失败、未捕获异常、零断言、整组跳过或入口
缺失为非零；未知用例退出码 `2`。每组输出一条 JSON，含 `case`、
`assertions`、`passed`、`failed`、`skipped`、`seed`（样本/种子）、
`expected_summary`、`observed_summary`、`failures`。

当前实际断言数：**C01=21、C02=20、C03=16、C04=12，共 69 条，全部通过**。

### 用例与可重放样本（均为合成地址，无真实数据）

| 用例 | 覆盖点 | 关键样本 |
|---|---|---|
| C01 | 标签规则、完整 CNAME 链、全地址集合任一禁用即拒 | 允许 `cdn.example.com`/`.media.example.org`/`*.img.example.net`；禁 `10.0.0.0/8`、`198.51.100.0/24`、`2001:db8:dead::/48`；恶意链 `relay.attacker.test` |
| C02 | IPv4 映射 IPv6 按内嵌 v4 判定、端口/Host/SNI、适配器地址钉死 | `::ffff:169.254.169.254`（元数据地址，映射形态仍被拒）、`::ffff:203.0.113.7`（归一化为 v4）、`:8443` 显式端口 |
| C03 | 可控时钟下的快照有效期、过期重算、每跳重新授权 | TTL=30s，时钟从 1000 推进到 1030 验证边界；同主机两跳地址 `.30→.31` 证明无地址继承 |
| C04 | 内存适配器重绑定不能替换已批准 IP；全程无真实网络 | 变异序列 `203.0.113.50 → 198.51.100.50 → 10.0.0.66 → 加塞 10.9.9.9 → 回到 .50`；另含对 `imgplan/` 源码的 AST 静态扫描，断言未导入 socket/ssl/HTTP 客户端/subprocess |

演示脚本（可独立重放，输出每步裁决）：

```bash
python3 examples/rebinding_demo.py
```

JSON 输出只包含合成主机名与 RFC 5737 / RFC 3849 文档地址、错误码与计划
摘要；不含凭据、真实敏感样本或私有账户内容。

## 设计取舍与未覆盖边界（审计须知）

- 核验包装器只是运行入口，**不是独立合格证明**；审计仍应阅读源码与真实
  运行日志，对无法在本环境证实的内容保留"未知"结论。
- 禁止网段完全由调用方提供，包**不内置** `127.0.0.0/8`、`169.254.0.0/16`、
  `0.0.0.0/8`、链路本地/唯一本地 IPv6 等默认封禁；C02/C04 中的元数据网段
  是测试显式传入的规则。生产部署需自行配置完整策略。
- 未实现真实 DNS/DoH 适配器、真实 TCP/TLS 拨号与 HTTP 重定向上限；
  `PlannedConnection` 是模拟连接记录。接入真实栈时必须保留"先规划后拨号、
  拨号地址 ∈ 批准集合、过期即拒"这一边界。
- 不做 IDNA 编码与证书校验；SNI 固定为原始裸主机名，证书验证策略不在本
  模块范围。
- 不解析 CNAME 链本身的信任关系（如链内 DNAME、大小写混淆之外的同形字），
  只做标签化允许匹配；通配仅支持前导 `*.` 单标签形态。
- TTL 取适配器自报值，规划器不做最小值钳制；时钟跳变由注入的 `Clock`
  实现负责，`FakeClock` 禁止回拨。
- 仅支持绝对 `http`/`https` URL，不支持 `CONNECT`、裸 IP、非标准 scheme
  与 userinfo。
