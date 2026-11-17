# Pod 服务账号令牌可读性分析库

分析同一 Pod 中哪些容器**最终**能读到 Kubernetes 服务账号令牌，并解释每个生效
字段来自原始 Pod 还是哪一次准入变更。纯 Python 标准库实现，不连接真实集群，
所有输入均为本地合成 JSON 清单。

## 目录结构

```
satoken_analyzer/          分析库
  __init__.py              公共 API 导出
  analyzer.py              四阶段分析引擎
  cli.py                   python -m 入口（非隔离模式）
samples/                   C01–C04 可重放样本清单
tools/
  local_verify.py          本地核验入口（--list / --case）
  analyze_manifest.py      单份清单分析 CLI（适配 python -I）
README.md
```

## 公共 API

```python
from satoken_analyzer import analyze, analyze_file, resolve_automount, AnalysisError

result = analyze(manifest)          # dict -> dict，结果可 json.dumps
result = analyze_file("pod.json")   # 从文件读取

decision, basis = resolve_automount(pod, service_account)
# decision: bool；basis 含 podValue / serviceAccountValue / effective / provenance
```

分析分四阶段：

1. **自动挂载决策**：Pod 的 `spec.automountServiceAccountToken` 覆盖
   ServiceAccount 的同名字段；两者都缺省按 Kubernetes 默认 `true`。
2. **准入变更引擎**：按 `admissionMutations` 数组顺序执行，维护最终
   `volumes` / `volumeMounts`，每个生效字段保留溯源（`pod` / `mutation` /
   `default`，含 mutation id 与字段路径）。
3. **容器维度解析**：`initContainers`、`containers`、`ephemeralContainers`
   分别出结果；显式 projected token 沿 volume → volumeMount 独立追踪，
   与自动挂载互不影响。
4. **audience 审计**：可读令牌与容器登记的必要 audience 对比，输出
   `overexposed` / `purposeMismatch` / `insufficientInfo` /
   `unmetRequirement` 四类发现。

## 输入清单结构

```json
{
  "pod": {
    "metadata": {"name": "..."},
    "spec": {
      "serviceAccountName": "app-sa",
      "automountServiceAccountToken": false,
      "volumes": [
        {"name": "v", "projected": {"sources": [
          {"serviceAccountToken": {
            "path": "token",
            "audience": ["aud-a"],
            "expirationSeconds": 1800
          }}
        ]}}
      ],
      "initContainers": [],
      "containers": [
        {"name": "c", "volumeMounts": [
          {"name": "v", "mountPath": "/run/secrets/x", "readOnly": true}
        ]}
      ],
      "ephemeralContainers": []
    }
  },
  "serviceAccount": {
    "metadata": {"name": "app-sa"},
    "automountServiceAccountToken": true
  },
  "admissionMutations": [],
  "tokenRegistry": {
    "v:token": {"purpose": "call-internal-api"}
  },
  "containerRequirements": {
    "c": {
      "requiredAudiences": ["aud-a"],
      "declaredPurpose": "call-internal-api"
    }
  }
}
```

### 准入变更操作（按数组顺序执行）

| op | 关键字段 | 语义 |
| --- | --- | --- |
| `addVolume` | `volume` | 新增卷，重名报错；溯源指向该 mutation |
| `removeVolume` | `volumeName` | 删除卷，并级联删除各容器指向它的挂载（记录在 `effectiveConfig.removedMounts`） |
| `renameVolume` | `from` / `to` | 重命名卷，同步改写所有挂载的 `name`，记录 `renameChain` |
| `patchVolume` | `volumeName` / `patch` | 整体覆盖卷的顶层字段（如 `projected`），被覆盖字段溯源指向该 mutation |
| `setVolumeMounts` | `container` / `volumeMounts` | 整体替换某容器的挂载列表，全部挂载字段溯源指向该 mutation |

非法结构、未知 op、引用不存在的卷/容器均抛 `AnalysisError`。
挂载指向不存在的卷不报错，标记为 `dangling: true` 并列入容器的
`danglingMounts`，该挂载不产生可读令牌。

### 审计判定规则

- **overexposed（多余暴露）**：容器已登记 `requiredAudiences`，某可读令牌
  携带必要集合之外的 audience。`readOnly: true` 只表示只读，令牌仍可读取，
  暴露记录保留 `readOnly` 标记。
- **purposeMismatch（用途不符）**：令牌在 `tokenRegistry` 登记的 `purpose`
  与容器 `declaredPurpose` 都存在且不相等。
- **insufficientInfo（信息不足）**：① 容器未在 `containerRequirements`
  登记（无法判断必要性）；② 令牌未在 `tokenRegistry` 登记用途（无法核对
  用途）。同一令牌可同时命中两条。
- **unmetRequirement（需求未满足）**：容器登记的必要 audience 没有任何
  可读令牌覆盖。
- 自动挂载令牌以固定 id `auto:serviceaccount-token` 表示；未登记 audience
  时默认 `kubernetes.default.svc`，未登记有效期时默认 3600 秒。
- projected token 的 audience 缺省时同样回退到 `kubernetes.default.svc`，
  并标记 `audienceDefaulted: true`。
- `tokenRegistry` 键支持 `卷名` 或 `卷名:来源path` 两种形式，后者优先。

## 命令与本地核验

使用工作区自带解释器，`-I` 隔离模式运行（不读取环境变量与用户 site 包）：

```bash
# 列出全部用例
.venv/bin/python -I tools/local_verify.py --list

# 运行单组核验（每组输出一条 JSON 记录）
.venv/bin/python -I tools/local_verify.py --case C01
.venv/bin/python -I tools/local_verify.py --case C02
.venv/bin/python -I tools/local_verify.py --case C03
.venv/bin/python -I tools/local_verify.py --case C04

# 分析任意清单
.venv/bin/python -I tools/analyze_manifest.py samples/c02_mutation_order_provenance.json
```

每组 JSON 记录包含：`case`、`sample`、`assertions`（实际断言数及
passed/failed/skipped）、`expectedSummary`、`observedSummary`、`failures`、
`ok`。任一断言失败、入口缺失、未实现、零断言或整组跳过均以**非零退出**；
输出只含合成的 audience / 用途名，不含凭据或真实敏感样本。

当前断言规模：C01 = 5，C02 = 4，C03 = 3，C04 = 5，共 17 条，全部可重放。

## 可重放样本说明

| Case | 样本 | 核心场景 |
| --- | --- | --- |
| C01 | `samples/c01_automount_override_and_projected.json` | SA 要求自动挂载、Pod 显式关闭；显式 projected token 仅 app 容器经独立卷路径可读，另含自动挂载三个决策分支的函数级断言 |
| C02 | `samples/c02_mutation_order_provenance.json` | 新增 → 重命名 → 覆盖卷字段 → 替换挂载 → 删卷（级联删挂载），断言最终配置与每个字段的 mutation 溯源 |
| C03 | `samples/c03_container_types_shared_volume.json` | 初始化/普通/临时容器独立结果；**共享卷只被 init 与 app-a 挂载，app-b 与临时容器读不到**；自动挂载关闭后仍有显式令牌投射 |
| C04 | `samples/c04_audience_audit.json` | 只读挂载仍计可读且多余 audience 暴露；用途不符；未登记令牌与未登记容器两种信息不足；无令牌时需求未满足 |

## 未覆盖边界（诚实声明）

- 不模拟真实 kube-apiserver / kubelet / 准入 Webhook，只消费合成的变更
  记录序列；变更记录本身的真实性无法由本库证实。
- 不做 Kubernetes 完整 schema 校验（如镜像拉取、安全上下文、PodSecurity、
  FSGroup/root 权限对文件可读性的影响）。
- 不解析 token 内容、不校验 JWT 签名/签发者/真实过期时刻；audience 与
  有效期完全以清单声明为准。
- 不建模 Linux 文件权限、`subPath` 单文件挂载、`subPathExpr`、挂载传播、
  符号链接逃逸等运行时因素。
- 真实集群中临时容器（ephemeralContainer）不允许声明 volumeMounts；本库
  仍按清单字段机械分析，若清单给了挂载也会计算，不额外拒绝。
- 不建模 BoundServiceAccountToken 的轮换、NodeRestriction、TokenRequest API
  动态签发，以及 1.22+ 自动挂载本身也是 projected 卷的实现细节（本库将其
  抽象为固定 id 的自动令牌）。
- `patchVolume` 为顶层字段整体覆盖语义，不实现 JSON Patch / strategic merge
  patch 的数组合并与键策略。
- 网络策略、宿主机路径、日志/环境变量间接泄露令牌等侧信道不在分析范围。
- 核验包装器是运行入口而非独立合格证明；审计仍应阅读库代码与真实日志，
  对无法从清单证实的内容保留"未知"。
