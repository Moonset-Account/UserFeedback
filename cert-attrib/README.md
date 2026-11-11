# cert-attrib — 证书判定分歧离线归因程序（C++17）

两个发行渠道在更新信任配置后对同一批服务证书给出不同结论。本程序以维护组提供的
**结构化记录中的签名验证结果作为唯一密码学证据**（引擎自身不做任何密码学运算），
按固定检查点顺序独立重放各渠道的验证流程，然后对齐两端逐检查点结论，找出
**最早出现分歧的检查点**并把差异归入四类原因：

| 归因类别 | 对应检查点 | 典型情形 |
|---|---|---|
| `incomplete_candidate_path` | `path_construction` / `signature_chain` | 候选链断裂、签名证据缺失、同名异钥下证据用错公钥 |
| `trust_anchor_change` | `anchor_selection` | 锚版本快照不同、旧锚移除/新锚引入、同名异钥锚裁决 |
| `time_window` | `validity_window` | 验证时刻不同、到期/未生效边界、端点纳入策略不同 |
| `purpose_constraint` | `purpose_constraint` | EKU 用途不匹配、非 CA 签发、pathLen 超限 |
| `mixed` | 多个检查点 | 两个及以上独立检查点同时出现实质差异 |
| `unexplained` | `final_decision` | 策略+证据推出的结论与客户端日志不一致（如本地黑名单） |

## 目录结构

```
cert-attrib/
├── include/certattrib/
│   ├── json.hpp      # 自实现迷你 JSON（无第三方依赖，离线可用）
│   ├── model.hpp     # 记录结构、检查点/原因码/报告结构
│   ├── loader.hpp    # JSON -> 数据集，含字段校验
│   ├── engine.hpp    # 离线验证引擎 + 分歧比对
│   └── report.hpp    # JSON 报告与中文文本视图
├── src/              # loader.cpp / engine.cpp / report.cpp / main.cpp
├── tests/attrib_tests.cpp   # 58 项断言的验收测试
├── data/acceptance.json     # 合成验收数据（全部为虚构标识）
├── Makefile
└── README.md
```

## 构建与运行

```bash
make            # 仅需 clang++/g++ 与 C++17，无外部依赖
make test       # 跑验收测试（当前 58 checks, 0 failures）
make run        # 对合成数据生成 build/report.json 并打印文本报告
# 或手动：
./build/cert-attrib --data data/acceptance.json \
                    --json-out build/report.json --text
# 聚焦单个叶证书：
./build/cert-attrib --data data/acceptance.json --leaf leaf-2 --text
```

## 检查点模型（固定顺序，分歧定位即取首个结论不同的检查点）

1. **path_construction** — 候选链逐跳核对 `(child.issuer, child.issuer_key_id) ==
   (parent.subject, parent.key_id)`，不满足即 `broken_chain`。覆盖交叉签发与链断裂。
2. **signature_chain** — 对链上每个签发关系 `(child, child.issuer_key_id)` 查
   `signature_evidence` 记录：无记录为证据缺失；若只存在**其他签发公钥**下的证据，
   判为同名异钥/重签（`signature_key_mismatch`）；结论为 `invalid` 即
   `signature_invalid`。引擎只消费结论，不自行验签。
3. **anchor_selection** — 在渠道锚版本快照内按 subject+key 选锚（见下文同名锚规则）。
4. **validity_window** — 用渠道自己的 `verification_time` 对链上每张证书做时间窗判定，
   端点语义由 `validity_endpoint_inclusive` 决定。
5. **purpose_constraint** — CA 基本约束、`pathLenConstraint`（按该 CA 下方中间 CA 数计）、
   叶证书 EKU 是否含策略要求用途。
6. **final_decision** — 任一路径通过即信任；全部失败时按最早失败检查点取汇总原因。
   失败检查点之后的检查点记为 `skipped_after_failure`，保证两端序列始终对齐可比。

### 同名异钥锚选择规则（anchor_selection）

设候选路径顶端证书 subject 为 S、key 为 K，快照中同 subject 的锚可能有多把 key：

1. 快照版本下没有 subject=S 的锚 → `anchor_not_trusted`；
2. 有 subject=S 且含 key=K 的生效锚：
   - 若生效同名锚只有一把 key，直接选中；
   - 若有多把同名异钥生效锚，必须由 `preferred_anchor_key_id` 确定性裁决：
     未配置 → `anchor_name_collision`；偏好与 K 不一致 → `anchor_name_collision`；
3. key=K 的锚只存在于其他版本 → `anchor_version_absent`；
4. 同 subject 但所有生效锚 key 均不等于 K：单钥情形 `anchor_not_trusted`，
   多钥情形 `anchor_name_collision`。

### 有效期端点是否纳入有效区间

**由策略字段 `validity_endpoint_inclusive` 逐渠道配置，没有全局默认之外的隐式假设**
（输入缺省按 `true` 处理，即 RFC 5280 的常见语义）：

- `true`（含端点，闭区间 `[not_before, not_after]`）：
  `not_before <= t <= not_after` 有效；`t < not_before` → `not_yet_valid`；
  `t > not_after` → `expired`。**恰好在 not_after 时刻仍有效。**
- `false`（排端点，开区间 `(not_before, not_after)`）：
  `not_before < t < not_after` 有效；`t == not_before` 或 `t == not_after` 均拒绝。
  **恰好在 not_after 时刻判过期。**

## 支持的策略字段（channels[]）

| 字段 | 类型 | 含义 |
|---|---|---|
| `channel_id` | string | 渠道标识 |
| `anchor_version` | int | 信任锚快照版本；锚记录仅在 `version <= 该值` 且未在该版本前移除时生效 |
| `verification_time` | int | 该渠道执行验证的时刻（epoch 秒），用于全部时间窗判定 |
| `required_eku` | string | 要求叶证书具备的 EKU 用途（如 `serverAuth`）；空串=不指定 |
| `enforce_eku` | bool | 是否强制 EKU 检查；false 时跳过 |
| `enforce_basic_constraints` | bool | 是否校验中间证书 `is_ca` 与 `path_len` |
| `validity_endpoint_inclusive` | bool | 有效期端点是否纳入有效区间（见上节） |
| `preferred_anchor_key_id` | string | 同名多钥锚冲突时确定性选择的 key；空串=无偏好（冲突即拒绝） |
| `reject_on_missing_evidence` | bool | 签名证据缺失时是否拒绝；false 时保守放行但在检查点 detail 中标注 |

输入记录字段：`certificates`（id/subject/issuer/key_id/issuer_key_id/not_before/
not_after/is_ca/path_len/eku）、`trust_anchors`（anchor_id/subject/key_id/version/
added_at/removed_at）、`signature_evidence`（child_cert_id/issuer_key_id/result/
checked_at，result ∈ valid|invalid）、`candidate_paths`（path_id/leaf_id/chain）、
`client_decisions`（channel_id/leaf_id/verdict/reason_code/last_checkpoint，可选，
仅作被解释对象，不参与引擎推导）。

## 验收数据覆盖的四类必备场景（data/acceptance.json）

| 场景 | 数据 | 预期 |
|---|---|---|
| 旧路径失效但另一条仍有效（交叉签发） | `leaf-1`：同一中间密钥 `kinx` 分别由 G1/G2 根交叉签发；v100 快照移除 G1 锚、v200 引入 G2 锚 | legacy 接受 `p1-old`、拒 `p1-new`；modern 反之但接受 `p1-new`；终审一致，最早路径级分歧在 `anchor_selection`，归因 `trust_anchor_change` |
| 证书到期边界 | `leaf-2` 的 `not_after=1100000`，ch-edge-exclusive 恰在该时刻验证且端点排除 → 过期；ch-lenient 同刻端点纳入 → 有效；modern 晚于该时刻 → 过期 | 最早分歧 `validity_window`，归因 `time_window` |
| 同名锚选择差异 | `leaf-3` 顶端 subject 均为 "Shared Issuer"，v200 快照含 ks1/ks2 两把同名锚；modern 偏好 ks2、lenient 偏好 ks1、edge 无偏好 | 偏好与路径 key 不符或无偏好 → `anchor_name_collision`，归因 `trust_anchor_change` |
| 签名结论缺失 | `leaf-4` 所需 `leaf-4@kim` 无任何证据，仅有异钥 `kim-rogue` 下的证据 | 严格渠道判 `signature_key_mismatch`；lenient 因 `reject_on_missing_evidence=false` 放行并标注，归因 `incomplete_candidate_path` |
| 附加：不可解释分歧 | `leaf-5` 策略推导全通过，ch-modern 客户端以 `local_blacklist` 本地拒绝 | 引擎两端一致；比对判 `unexplained`，`both_client_decisions_explained=false` |
| 附加：断裂路径不影响兄弟路径 | `leaf-6` 的 `p6-bad` 链断裂，`p6-good` 完整 | 断裂路径在路径构造阶段失败，终审仍经完整路径信任 |

## 输出报告

`--json-out` 生成结构化报告，含：

- `channel_verdicts`：每叶证书 × 每渠道的终审、汇总原因、接受路径，以及每条候选路径
  的五个检查点 PASS/FAIL、原因码、人类可读细节、实际引用的证据引用
  （`child_cert_id@issuer_key_id`）、与客户端日志的对账结论；
- `divergence_reports`：每对渠道 × 每叶证书的 `divergent`、`earliest_checkpoint`、
  `attribution`、逐路径逐检查点的 `attribution_factors`、实际不同的策略字段
  `policy_deltas`、两端客户端判定是否都能由各自策略解释。

`--text` 输出等价的中文终端视图，便于人工复核。

## 设计边界

- 引擎不解析真实 X.509/ASN.1，也不做密码学运算；签名结论完全以输入证据为准，
  适合离线、可审计的归因场景。
- 证据按 `(child_cert_id, issuer_key_id)` 精确匹配，因此同名异钥、重签、交叉签发
  都体现为不同的证据键，不会被错误复用。
- 若候选路径集合在两端不同（本数据模型中路径为全局共享记录），应先在数据层拆成
  两份数据集分别运行；当前比对器对路径 id 做对齐，缺失侧不会被静默当作通过。
