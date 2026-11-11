# SEC-VULN-026 离线分析：导入功能 XML 解析工厂外部资源限制审查

## 背景与目标

公共 XML 解析工厂已设置外部资源限制（禁 DOCTYPE、禁外部通用/参数实体、禁外部
DTD、启用安全处理），但部分导入入口创建的是**自己的工厂**，可能绕过公共限制。

本工具不读取、不执行任何业务源码，只消费**离线调用关系与配置清单**（抽象配置字段
+ 调用点编号），从每个导入入口推导工厂在不同环境中的**有效设置**，并区分三类结论：

| 判定 | 含义 |
| --- | --- |
| `compliant` | 外部资源限制完整，全部必设项满足策略 |
| `explicit_deviation` | 配置明确偏离要求（含覆盖顺序冲突导致有效值未定、且候选值含违规分支） |
| `evidence_missing` | 证据缺失（必设项无任何来源，或动态工厂无法离线解析） |

## 目录结构

```
analyzer/xml_factory_audit.py     分析器（仅标准库，python3 -I 运行）
data/offline/                     基线离线清单（存在遗漏）
  manifest-00-policy.json         安全策略：必设项与要求值、覆盖环境
  manifest-01-factories.json      工厂定义：默认值、parent 继承、别名、动态解析信息
  manifest-02-environments.json   环境覆盖：来源、layer、precedence
  manifest-03-callsites.json      调用点 CS-### 与导入入口（调用链 + 工厂引用）
data/offline-fixed/               修复后的清单（结构完全相同，用于回归闭环）
tests/test_acceptance.py          验收测试：四类数据 + 修复后同一检查通过
report-baseline.{json,md}         基线分析报告
report-fixed.{json,md}            修复后分析报告
```

## 清单模型（抽象字段）

- **工厂继承**：`parent_factory` 指向父工厂，子工厂默认值覆盖父工厂同名设置；
  未在子工厂出现的设置沿继承链向上取值，逐项记录来源工厂与定义文件。
- **别名归一**：每个工厂在 `aliases` 中声明全部引用名；入口的 `factory_ref`
  无论是别名还是 `factory_id`，都解析到**同一个配置定义**。别名重复映射会直接报错。
- **环境覆盖**：`(environment, factory, setting)` 上可挂多条覆盖，每条带来源文件、
  `layer` 与 `precedence`。
- **覆盖顺序冲突**：同一键上存在多条覆盖且**最高 precedence 不唯一**时判定冲突，
  报告逐条列出冲突来源（`override_id`、值、文件、layer）。冲突不会被静默吞掉：
  该设置标记为 `override_conflict`，若任一候选值违反策略，环境判定为
  `explicit_deviation`。分析器内部仅用 `layer_order_for_tiebreak` 做确定性兜底，
  并在 `conflict_notes` 中完整披露。
- **动态工厂**：`kind=dynamic` 且 `resolution.offline_resolved_factory_id` 为空时，
  无法静态绑定，入口在所有环境判定 `evidence_missing / dynamic_factory_unresolved`；
  补齐离线解析目标后，分析器跟随到目标工厂按同一流程评估。

## 四类验收数据（基线清单）

| 入口 | 场景 | 基线结论 |
| --- | --- | --- |
| `EP-CATALOG` (CS-001) | **继承成功**：自定义工厂继承公共工厂，5 项限制全部满足 | 两环境均 `compliant` |
| `EP-INVOICE` (CS-002) | **自定义工厂遗漏限制**：未继承；3 项无证据，`feature_load_external_dtd=true` 显式违规 | 两环境均 `explicit_deviation` |
| `EP-SUPPLIER` (CS-003) | **生产覆盖开发值 + 冲突**：dev 由 OV-DEV-001 覆盖且合规；prod 的 OV-PROD-001(false) 与 OV-PROD-002(true) 同 precedence=30 冲突 | dev `compliant`，prod `explicit_deviation` |
| `EP-PRICING` (CS-004) | **无法解析的动态工厂**：类名来自运行期配置，离线无绑定 | 两环境均 `evidence_missing` |
| `EP-SHARED` (CS-005) | 别名一致性：`xml.SecureFactory` 归一到 `F-PUBLIC-COMMON` | 两环境均 `compliant` |

## 运行方式

```bash
# 基线分析，输出机器可读 JSON 与补丁复查用 Markdown
python3 -I analyzer/xml_factory_audit.py \
  --data-dir data/offline \
  --json-out report-baseline.json \
  --md-out report-baseline.md

# 修复后清单回归（同一分析器、同一套检查）
python3 -I analyzer/xml_factory_audit.py \
  --data-dir data/offline-fixed \
  --json-out report-fixed.json \
  --md-out report-fixed.md

# 验收测试
python3 -I tests/test_acceptance.py
```

`-I` 以隔离模式运行，避免工作目录中的同名模块被 `import` 加载。

## 补丁复查怎么用报告

每个入口报告给出：

1. **入口与调用链**：`entry_id` → `callsite_chain`（CS 编号、符号、文件、行号）；
2. **配置链**：工厂引用 → 别名归一后的 `factory_id` → 继承链上每个设置的来源
   （工厂定义文件或环境覆盖 `override_id`）；
3. **逐设置对比**：要求值 / 有效值 / 状态 / 来源；
4. **修复位置**：`fix_location`，即补丁应落点的调用点。

基线报告中的三个修复点：

- `src/imports/invoice_import.py:57`：让发票工厂继承公共工厂（或逐项补齐 5 项
  限制），并将 `feature_load_external_dtd` 改为 `false`；
- `deployment/prod/security-overrides.properties` 与
  `config/application-prod.yaml`：为两条生产覆盖赋予不同 precedence（或统一为
  安全值 `false`），消除顺序冲突；
- `src/imports/pricing_import.py:72`：固定动态工厂类名并在清单中补齐
  `offline_resolved_factory_id`，使其可静态绑定到公共安全工厂。

修复内容已体现在 `data/offline-fixed/`，对同一分析器重跑后：5 个入口在 dev/prod
全部 `compliant`、冲突数为 0，即"补齐配置后同一条检查通过"。
