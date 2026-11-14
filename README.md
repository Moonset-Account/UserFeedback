# 容器发布准入检查（SEC-20261114-054）

上线前对候选工作负载做准入检查：特权模式、root 运行、宿主机目录挂载；
支持审批例外（绑定镜像摘要，镜像一变即需重新核对）与已上线版本比对。
纯 Python 3 标准库，无第三方依赖。

## 本地入口

```bash
# 准入检查（退出码 0=放行 1=拦截 2=输入错误，结果 JSON 输出到 stdout）
/usr/local/bin/python3 app.py \
    --candidate examples/candidate_blocked.json \
    --deployed  examples/deployed.json \
    --baseline  examples/baseline.json \
    --exceptions examples/exceptions.json \
    --now 2026-11-14T00:00:00Z --pretty

# 本地核验（unittest 套件 + 样例/边界断言，任一失败即非零退出）
/usr/local/bin/python3 tools/local_verify.py
```

## 输入格式

**候选/已上线工作负载**（`--candidate` / `--deployed`，同一结构）：

```json
{
  "namespace": "payments",
  "name": "checkout",
  "defaults": {"runAsUser": 1000},
  "containers":     [{"name": "app", "image": "repo/app@sha256:...",
                      "securityContext": {"privileged": false, "runAsUser": 1000},
                      "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
  "initContainers": [ ... 同 containers ... ],
  "volumes": [{"name": "data", "hostPath": {"path": "/var/lib/x"}}]
}
```

**团队基线**（`--baseline`，可选；缺省三条规则全部强制）：

```json
{"team": "payments",
 "rules": {"privileged": {"enforced": true},
           "run-as-root": {"enforced": true},
           "host-path": {"enforced": true, "allowedPaths": ["/var/log/cache"]},
           "run-identity-unconfirmed": {"enforced": true}}}
```

`allowedPaths` 按“精确等于或位于放行目录之下”匹配。

**审批例外**（`--exceptions`，可选；`{"exceptions": [...]}` 或裸数组）：

```json
{"id": "EX-1", "namespace": "payments", "workload": "checkout",
 "container": "app", "imageDigest": "sha256:...", "violation": "host-path",
 "hostPath": "/var/lib/x", "expiresAt": "2026-12-31T23:59:59Z"}
```

例外必须**同时**匹配命名空间、工作负载、容器、镜像摘要、违规项；
挂载类（`host-path`）例外还要匹配 `hostPath`；`expiresAt <= 评估时刻` 视为已到期不生效。
镜像只带 tag 没有摘要时，任何例外都无法匹配（无法确认绑定对象）。

## 输出语义

```json
{
  "decision": "allow | block",
  "reasons": ["未豁免违规的描述..."],
  "candidate": {"violations": [{
      "id": "V001", "type": "privileged | run-as-root | host-path | run-identity-unconfirmed",
      "location": "containers[0](app)", "containerKind": "container | initContainer",
      "imageDigest": "sha256:...", "effectiveValue": true,
      "exception": {"status": "none | applied | expired", "exceptionId": "EX-1"},
      "suppressed": false, "isNew": true}]},
  "deployed": {"violations": [...]},          // 仅提供 --deployed 时
  "newViolations": [...],                      // 仅提供 --deployed 时：候选相对已上线新增的未豁免违规
  "summary": {"totalViolations": 3, "suppressedByException": 1,
              "unsuppressed": 2, "newViolations": 2}
}
```

- 任一未豁免违规即 `block`（退出码 1），全部豁免或无违规即 `allow`（退出码 0）。
- 每条违规给出位置（普通/初始化容器 + 下标 + 名称）、实际生效值与例外匹配情况。
- 运行身份：容器 `securityContext.runAsUser` 覆盖工作负载 `defaults.runAsUser`；
  两处都缺失时产生 `run-identity-unconfirmed` 违规并拦截。
- 提供 `--deployed` 时，两个版本用**同一基线**评估；违规按
  （类型, 容器类别, 容器名, 宿主机路径）比对，`isNew=true` 表示候选新增。
- `--now` 固定评估时刻（用于确定性判断例外到期），缺省取当前 UTC 时间。

## 需求 → 测试/样例映射

| 需求 | 覆盖 |
|---|---|
| 普通+初始化容器检查特权/root/宿主机挂载 | `TestPrivilegedAndRootAndHostPath`、样例 S2/S3 |
| 容器 UID 覆盖工作负载默认；两处缺失拦截 | `TestRunAsUserOverride`、边界 B1/B2 |
| 例外五要素+hostPath+到期匹配 | `TestExceptions`、样例 S4、边界 B3 |
| 镜像摘要变化后旧例外失效 | `test_digest_change_invalidates_exception`、样例 S5 |
| 逐项位置/生效值/例外状态 | `test_violation_reports_location_value_and_exception_status`、S2/S3 |
| 已上线版本同基线比对、标出新增 | `TestDeployedDiff`、样例 S6 |
| 流水线消费（退出码 + JSON） | `TestCli`、样例 S1/S7 |

## 限制

- 输入为合成离线 JSON，不连接真实集群/镜像仓库；镜像摘要从 `image` 的 `@` 后缀解析。
- 时间按 ISO 8601 解析（支持 `Z`），到期判断为 `expiresAt <= now`。
- 基线中未知规则名、非法 JSON、缺字段等输入错误以退出码 2 拒绝。
