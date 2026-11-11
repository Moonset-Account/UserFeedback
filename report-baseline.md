# XML 解析工厂外部资源限制离线分析报告

- 策略: `xml-external-resource-hardening`
- 覆盖环境: env-dev, env-prod
- 入口统计: 共 5 个；完整合规 2，明确偏离 2，证据缺失 1
- 覆盖顺序冲突: 1 处

## 覆盖顺序冲突（具体来源）

- `env-prod` / `F-SUPPLIER` / `feature_external_general_entities`：2 条覆盖同处 precedence=30，取值互不相同：
  - `OV-PROD-001` 值=False 来源=`config/application-prod.yaml` layer=`application-yaml`
  - `OV-PROD-002` 值=True 来源=`deployment/prod/security-overrides.properties` layer=`deployment-config`

## 入口明细（含配置链与修复位置）

### EP-CATALOG — 商品目录 XML 导入

- 入口判定: **外部资源限制完整**
- 调用链: CS-001(imports.catalog.CatalogImport.run @ src/imports/catalog_import.py:41)
- 工厂引用: `imports.catalog.CatalogXmlFactory` -> `F-CUSTOM-CATALOG` （别名归一）
- 修复位置: `src/imports/catalog_import.py:41`

#### 环境 `env-dev`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-CUSTOM-CATALOG @ config/factories/import-catalog.yaml [factory-default] |

#### 环境 `env-prod`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-CUSTOM-CATALOG @ config/factories/import-catalog.yaml [factory-default] |

### EP-INVOICE — 发票 XML 导入

- 入口判定: **配置明确偏离要求**
- 调用链: CS-002(imports.invoice.InvoiceImport.run @ src/imports/invoice_import.py:57)
- 工厂引用: `imports.invoice.InvoiceFactory` -> `F-CUSTOM-INVOICE` （别名归一）
- 修复位置: `src/imports/invoice_import.py:57`

#### 环境 `env-dev`: 配置明确偏离要求 (one_or_more_settings_explicitly_violate_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | None | 证据缺失 | （无证据） |
| `feature_external_general_entities` | False | None | 证据缺失 | （无证据） |
| `feature_external_parameter_entities` | False | None | 证据缺失 | （无证据） |
| `feature_load_external_dtd` | False | True | 明确偏离 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |

#### 环境 `env-prod`: 配置明确偏离要求 (one_or_more_settings_explicitly_violate_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | None | 证据缺失 | （无证据） |
| `feature_external_general_entities` | False | None | 证据缺失 | （无证据） |
| `feature_external_parameter_entities` | False | None | 证据缺失 | （无证据） |
| `feature_load_external_dtd` | False | True | 明确偏离 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |

### EP-SUPPLIER — 供应商 XML 导入

- 入口判定: **配置明确偏离要求**
- 调用链: CS-003(imports.supplier.SupplierImport.run @ src/imports/supplier_import.py:63)
- 工厂引用: `imports.supplier.SupplierFactory` -> `F-SUPPLIER` （别名归一）
- 修复位置: `src/imports/supplier_import.py:63`

#### 环境 `env-dev`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-SUPPLIER @ config/factories/import-supplier.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | OV-DEV-001 @ config/application-dev.yaml [application-yaml] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |

#### 环境 `env-prod`: 配置明确偏离要求 (override_precedence_conflict_undetermined_effective_value)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-SUPPLIER @ config/factories/import-supplier.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 覆盖冲突(有效值未定) | OV-PROD-001 @ config/application-prod.yaml [application-yaml] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |

> ⚠️ 覆盖冲突: env-prod/F-SUPPLIER/feature_external_general_entities 存在 2 条 precedence=30 的覆盖，已按 layer_order 兜底选择 OV-PROD-001；来源: config/application-prod.yaml, deployment/prod/security-overrides.properties

### EP-PRICING — 定价 XML 导入（动态工厂）

- 入口判定: **证据缺失**
- 调用链: CS-004(imports.pricing.PricingImport.buildFactory @ src/imports/pricing_import.py:72)
- 工厂引用: `imports.pricing.DynamicFactory.create` -> `F-DYN-PRICING` （别名归一）
- 修复位置: `src/imports/pricing_import.py:72`

#### 环境 `env-dev`: 证据缺失 (dynamic_factory_unresolved)

- 动态工厂 offline_resolved_factory_id 为空: 工厂类名来自运行期配置键，离线清单中无解析结果，无法静态绑定到任何 factory_id

#### 环境 `env-prod`: 证据缺失 (dynamic_factory_unresolved)

- 动态工厂 offline_resolved_factory_id 为空: 工厂类名来自运行期配置键，离线清单中无解析结果，无法静态绑定到任何 factory_id

### EP-SHARED — 共享通道 XML 导入（经别名引用公共工厂）

- 入口判定: **外部资源限制完整**
- 调用链: CS-005(imports.shared.SharedImport.run @ src/imports/shared_import.py:35)
- 工厂引用: `xml.SecureFactory` -> `F-PUBLIC-COMMON` （别名归一）
- 修复位置: `src/imports/shared_import.py:35`

#### 环境 `env-dev`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |

#### 环境 `env-prod`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
