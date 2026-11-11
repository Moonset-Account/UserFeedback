# XML 解析工厂外部资源限制离线分析报告

- 策略: `xml-external-resource-hardening`
- 覆盖环境: env-dev, env-prod
- 入口统计: 共 5 个；完整合规 5，明确偏离 0，证据缺失 0
- 覆盖顺序冲突: 0 处

## 覆盖顺序冲突（具体来源）

无。

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

- 入口判定: **外部资源限制完整**
- 调用链: CS-002(imports.invoice.InvoiceImport.run @ src/imports/invoice_import.py:57)
- 工厂引用: `imports.invoice.InvoiceFactory` -> `F-CUSTOM-INVOICE` （别名归一）
- 修复位置: `src/imports/invoice_import.py:57`

#### 环境 `env-dev`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |

#### 环境 `env-prod`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-CUSTOM-INVOICE @ config/factories/import-invoice.yaml [factory-default] |

### EP-SUPPLIER — 供应商 XML 导入

- 入口判定: **外部资源限制完整**
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

#### 环境 `env-prod`: 外部资源限制完整 (all_required_settings_meet_policy)

| 设置项 | 要求值 | 有效值 | 状态 | 来源 |
| --- | --- | --- | --- | --- |
| `feature_disallow_doctype_decl` | True | True | 满足 | F-SUPPLIER @ config/factories/import-supplier.yaml [factory-default] |
| `feature_external_general_entities` | False | False | 满足 | OV-PROD-002 @ deployment/prod/security-overrides.properties [deployment-config] |
| `feature_external_parameter_entities` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_load_external_dtd` | False | False | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |
| `feature_secure_processing` | True | True | 满足 | F-PUBLIC-COMMON @ config/factories/public-common.yaml [factory-default] |

### EP-PRICING — 定价 XML 导入（动态工厂）

- 入口判定: **外部资源限制完整**
- 调用链: CS-004(imports.pricing.PricingImport.buildFactory @ src/imports/pricing_import.py:72)
- 工厂引用: `imports.pricing.DynamicFactory.create` -> `F-DYN-PRICING` （别名归一）
- 修复位置: `src/imports/pricing_import.py:72`

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
