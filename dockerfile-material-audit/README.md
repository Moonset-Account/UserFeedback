# Dockerfile 外部材料内容摘要绑定审计器

零依赖 Node.js 纯文本审计工具。针对**只含 `FROM`、`ARG`、`COPY --from` 的简化 Dockerfile**，
结合发布目标名、合成构建参数与批准的材料摘要清单，回答一个门禁问题：

> 发布目标真正依赖的每份外部材料，是否都绑定了清单中**来源相同、内容摘要匹配**的审批记录？

工具不执行构建、不访问注册表，全部结论来自文本静态分析。

## 背景问题

发布平台的构建审批记录里虽然保存了镜像摘要，但 Dockerfile 中的实际引用仍是
`image:tag` 这类**可变标签**——标签可以被重新指向不同内容，审批摘要与实际拉取的内容
之间没有强制绑定。审计器逐条比对“实际引用身份”与“审批材料身份”，暴露这种偏差。

## 用法

```bash
node bin/audit.js \
  --dockerfile examples/Dockerfile \
  --target release \
  --params examples/build-params.json \
  --approvals examples/approvals.json
# 加 --json 输出结构化报告
```

退出码：`0` PASS · `2` FAIL（存在未绑定/不匹配）· `3` INCONCLUSIVE（待核实）· `1` 输入错误。

输入 JSON 形态：

```jsonc
// build-params.json：合成构建参数（键值对）
{ "BASE_REGISTRY": "registry.example.com" }

// approvals.json：批准的材料摘要清单
[ { "id": "APV-1",
    "registry": "registry.example.com",
    "repository": "team/builder",
    "tag": "v3",
    "digest": "sha256:..." } ]
```

## 审计模型

### 1. 依赖闭包：谁是发布目标“真正依赖”的材料

- 从发布目标阶段出发，沿 `FROM <内部阶段>` 与 `COPY --from=<内部阶段>` 做 BFS 可达性分析；
- **不可达阶段**（未被任何可达阶段引用）的外部材料不参与门禁，报告中单独列出；
- 阶段名含 `test`（大小写不敏感）视为测试阶段，其材料即使可达也**仅供测试、不参与门禁**；
- 每条结论带依赖路径（如 `release -> runtime -> builder`），证明间接依赖的来源。

### 2. 材料身份：区分同名材料的不同来源

- **来源身份** `sourceKey = registry/repository`：无 host 的引用归一化到 `docker.io`，
  Docker Hub 单段官方仓库补 `library/` 前缀（`ubuntu` → `docker.io/library/ubuntu`）；
- **内容身份** `digest`：引用带 `@digest` 才算内容固定（`pinned`），仅有 tag 一律视为可变；
- `infra/cacerts` 在 `registry.example.com` 与 `mirror.corp.internal` 是两份不同材料，
  审批摘要不能跨来源顶替。

### 3. 绑定判定（6 种状态）

| 状态 | 含义 |
|---|---|
| `PINNED_APPROVED` | 带 `@digest`，且清单存在**同源同摘要**记录 |
| `PINNED_UNAPPROVED` | 带 `@digest`，但清单无同源匹配摘要（含同摘要属于另一来源的情况） |
| `MUTABLE_TAG_APPROVED` | 可变标签；清单只有同源标签级/其他摘要记录，当前引用未绑定内容摘要 |
| `MUTABLE_TAG_OTHER_SOURCE` | 可变标签；同名材料的审批摘要属于**另一来源** |
| `MUTABLE_TAG_UNAPPROVED` | 可变标签；清单无任何同源记录 |
| `PENDING` | 构建参数无法解析（ARG 无默认值且合成参数未提供），身份待人工核实 |

门禁结论：有任一违规 → `FAIL`；无违规但有待核实 → `INCONCLUSIVE`；全部固定且匹配 → `PASS`。

### 4. 变量解析

- 支持 `$VAR`、`${VAR}`、`${VAR:-default}`；作用域遵循 Docker 语义（首个 `FROM` 前为全局
  ARG，之后为阶段内 ARG），合成构建参数覆盖默认值；
- 解析后若仍残留未解析变量，**不做猜测**，该条标记 `PENDING` 并列出变量名与指令行。

## 报告内容

每条发现都给出题目要求的三要素：

- **触发问题的指令行**：物理行号 + 原始指令文本（如第 8 行 `FROM ${BASE_REGISTRY}/...:${BASE_TAG}`）；
- **实际材料身份**：来源、tag、digest、是否内容固定；
- **适用的审批记录**：清单中命中的审批编号与完整身份（无命中则为 `无`），并文字说明偏差原因。

## 验证场景（`test/auditor.test.js`，15 项）

1. **发布目标已固定内容**：`@digest` + 同源同摘要 → `PINNED_APPROVED` / PASS；
2. **间接引用可变标签**：可变标签藏在被 `FROM`/`COPY --from` 间接依赖的中间阶段，
   仍被定位到具体指令行并判 `MUTABLE_TAG_APPROVED` / FAIL；
3. **审批摘要对应另一来源**：同 repository 不同 registry → `MUTABLE_TAG_OTHER_SOURCE`；
   digest 固定但同摘要只存在于另一来源 → `PINNED_UNAPPROVED`；
4. **未使用阶段不影响门禁**：不可达阶段中的违规材料计数为 0 违规，结论仍 PASS；
5. 另有测试阶段豁免、`COPY --from` 外部镜像、ARG 待核实（INCONCLUSIVE）、
   违规与待核实并存时 FAIL 优先、镜像归一化与解析器行号等单元测试。

```bash
npm test
```

## 目录结构

```
src/
  imageRef.js   镜像引用解析与来源/内容身份归一化
  variable.js   ARG 变量替换与未解析变量检测
  parser.js     简化 Dockerfile 解析（保留行号、支持折行）
  auditor.js    可达性分析 + 清单比对 + 报告生成
  report.js     中文文本报告格式化
bin/audit.js    CLI（--json / 退出码）
examples/       可直接运行的示例输入（含 Dockerfile.pending 待核实用例）
test/           node:test 测试套件
```
