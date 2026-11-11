# ReleaseGate — 独立构建插件的本地发布门禁

主程序开启控制流完整性（CFI）保护后，独立构建的插件部分集成路径可能落在保护范围外。
本门禁在发布前消费构建清单，对每条跨组件间接调用边核验证据链，并检查依赖图结构。

## 输入（构建清单 JSON）

| 字段 | 含义 |
| --- | --- |
| `components[].id` | 组件身份 = 名称 + 版本；**同名插件的不同版本是不同身份** |
| `components[].buildTarget` | 构建目标（违规定位用） |
| `components[].team` | 负责该组件的团队（违规定位用） |
| `components[].abiVersion` | 组件 ABI 版本 |
| `edges[]` | 间接调用边：`source` 经 `interface` 调用 `target` |
| `coverageClaims[]` | CFI 保护覆盖声明：组件受保护的接口集合 |
| `exemptions[]` | 带期限豁免：只对指定组件身份 + 指定接口生效 |

豁免与声明均按**完整组件身份（名称 + 版本）**匹配，一个版本的声明/豁免不会让其他版本搭便车。

## 判定规则（按顺序）

对每条跨组件间接调用边：

1. **unknown_component** — 边引用了清单中不存在的组件身份；
2. **abi_mismatch** — 调用两端 ABI 版本不一致（硬错误，豁免不可压制）；
3. 目标接口必须被目标组件的覆盖声明包含：
   - 组件无任何声明 → **missing_declaration**；
   - 有声明但不含该接口 → **uncovered_edge**；
   - 存在精确匹配「目标组件身份 + 接口」且未过期的豁免 → 放行，记入 `exemptedCalls`；
   - 匹配的豁免已过期（`expiresAt <= 评估时刻`）→ **expired_exemption**；
4. 全部通过的边记入 `evidencedCalls`（有完整证据的跨组件调用）。

另外对依赖图做三色 DFS 环检测：

5. **dependency_cycle** — 图中存在环；环按最小组件身份旋转归一化后去重，独立于调用边判定，豁免不可压制。

组件内部自环边不参与跨组件判定与环检测。

每条调用类违规都列出：构建目标（`buildTarget`）、接口（`interface`）、负责团队（`team`），
以及来源侧的构建目标与团队，便于分派整改。

## 用法

```bash
swift build
swift test

# 评估清单（--now 用于可重复地判定豁免期限，默认取当前时间）
swift run release-gate Examples/sample-manifest.json --now 2026-11-12 --pretty --report-out report.json
echo $?   # 0=通过，1=拒绝（有违规），2=用法/解析错误
```

stdout 输出机器可读的 JSON 报告，stderr 输出人类可读摘要；
`--report-out` 额外把 JSON 报告写入文件。日期支持 ISO-8601 与 `yyyy-MM-dd`（UTC 解释）。

## 测试覆盖（11 个用例）

- 同名插件的不同版本：证据/缺失声明/未知身份分别归属，声明不跨版本生效；
- 同一组件同时存在受保护与未覆盖的调用边；
- 有效豁免只匹配指定接口，且只匹配指定组件身份；
- 过期豁免产生 `expired_exemption` 且不放行（含到期时刻边界）；
- 依赖图含环（含旋转去重）、自环与无环图不误报；
- ABI 不一致即使有豁免也违规；
- 清单编解码与日期往返。
