"""验收测试：四类数据 + 修复闭环。

以 `python3 -I tests/test_acceptance.py` 运行。
测试不联网、不执行被分析代码，只驱动 analyzer 读取离线清单。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzer.xml_factory_audit import (  # noqa: E402
    VERDICT_COMPLIANT,
    VERDICT_DEVIATION,
    VERDICT_MISSING,
    evaluate,
    load_manifest,
    render_markdown,
)

BASELINE_DIR = ROOT / "data" / "offline"
FIXED_DIR = ROOT / "data" / "offline-fixed"


def entry_by_id(report, entry_id):
    for entry in report["entry_reports"]:
        if entry["entry_id"] == entry_id:
            return entry
    raise AssertionError(f"报告中缺少入口 {entry_id}")


def env_report(entry, env_id):
    for env in entry["environments"]:
        if env["environment_id"] == env_id:
            return env
    raise AssertionError(f"入口 {entry['entry_id']} 缺少环境 {env_id}")


def setting_status(env, setting_name):
    for item in env["settings"]:
        if item["setting"] == setting_name:
            return item["status"], item
    raise AssertionError(
        f"环境 {env['environment_id']} 缺少设置项 {setting_name}"
    )


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  PASS: {message}")


def test_baseline_four_categories():
    print("[基线清单] 四类验收数据")
    report = evaluate(load_manifest(BASELINE_DIR))

    # 1) 继承成功：EP-CATALOG 经 parent_factory 继承公共工厂全部限制
    catalog = entry_by_id(report, "EP-CATALOG")
    check(catalog["resolved_factory_id"] == "F-CUSTOM-CATALOG",
          "EP-CATALOG 别名归一到 F-CUSTOM-CATALOG")
    check(catalog["alias_normalized"] is True,
          "EP-CATALOG 标记为别名归一引用")
    for env_id in ("env-dev", "env-prod"):
        env = env_report(catalog, env_id)
        check(env["verdict"] == VERDICT_COMPLIANT,
              f"EP-CATALOG/{env_id} 继承后外部资源限制完整")
    inherited = env_report(catalog, "env-dev")
    status, item = setting_status(inherited, "feature_external_general_entities")
    check(status == "meets" and item["origin"]["factory_id"] == "F-PUBLIC-COMMON",
          "EP-CATALOG 的外部实体设置来源可追溯到父工厂 F-PUBLIC-COMMON")

    # 2) 自定义工厂遗漏限制：EP-INVOICE 自带工厂未继承且未配全
    invoice = entry_by_id(report, "EP-INVOICE")
    check(invoice["resolved_factory_id"] == "F-CUSTOM-INVOICE",
          "EP-INVOICE 归一到 F-CUSTOM-INVOICE")
    for env_id in ("env-dev", "env-prod"):
        env = env_report(invoice, env_id)
        check(env["verdict"] == VERDICT_DEVIATION,
              f"EP-INVOICE/{env_id} 判定为明确偏离/证据不足组合")
        miss_status, _ = setting_status(env, "feature_disallow_doctype_decl")
        check(miss_status == "missing_evidence",
              f"EP-INVOICE/{env_id} 缺少 DOCTYPE 限制证据")
        dev_status, dev_item = setting_status(env, "feature_load_external_dtd")
        check(dev_status == "explicit_deviation"
              and dev_item["effective_value"] is True,
              f"EP-INVOICE/{env_id} 外部 DTD 加载被显式开启（偏离）")

    # 3) 生产环境覆盖开发值 + 覆盖顺序冲突：EP-SUPPLIER
    supplier = entry_by_id(report, "EP-SUPPLIER")
    dev_env = env_report(supplier, "env-dev")
    prod_env = env_report(supplier, "env-prod")
    _, dev_item = setting_status(dev_env, "feature_external_general_entities")
    check(dev_item["origin"]["override_id"] == "OV-DEV-001",
          "EP-SUPPLIER/env-dev 有效值来自开发覆盖 OV-DEV-001")
    check(dev_env["verdict"] == VERDICT_COMPLIANT,
          "EP-SUPPLIER/env-dev 限制完整")
    conflict_status, _ = setting_status(
        prod_env, "feature_external_general_entities"
    )
    check(conflict_status == "override_conflict",
          "EP-SUPPLIER/env-prod 检测到覆盖顺序冲突，有效值未定")
    check(prod_env["verdict"] == VERDICT_DEVIATION,
          "EP-SUPPLIER/env-prod 因冲突且候选值含违规分支，判定偏离")
    check(len(prod_env["conflict_notes"]) == 1,
          "EP-SUPPLIER/env-prod 输出 1 条冲突说明")
    note = prod_env["conflict_notes"][0]
    check(set(note["conflicting_override_ids"]) == {"OV-PROD-001", "OV-PROD-002"},
          "冲突说明点名 OV-PROD-001 与 OV-PROD-002 两个具体来源")
    top_conflicts = report["precedence_conflicts"]
    check(len(top_conflicts) == 1
          and top_conflicts[0]["sources"][0]["source"].endswith(
              "application-prod.yaml")
          and top_conflicts[0]["sources"][1]["source"].endswith(
              "security-overrides.properties"),
          "全局冲突清单列出双方具体配置文件来源")

    # 4) 无法解析的动态工厂：EP-PRICING
    pricing = entry_by_id(report, "EP-PRICING")
    check(pricing["resolved_factory_id"] == "F-DYN-PRICING",
          "EP-PRICING 别名可归一到动态工厂定义")
    for env_id in ("env-dev", "env-prod"):
        env = env_report(pricing, env_id)
        check(env["verdict"] == VERDICT_MISSING
              and env["reason"] == "dynamic_factory_unresolved",
              f"EP-PRICING/{env_id} 动态工厂无法静态解析，证据缺失")

    # 别名一致性：xml.SecureFactory 与公共工厂其他别名归一到同一 factory_id
    shared = entry_by_id(report, "EP-SHARED")
    check(shared["resolved_factory_id"] == "F-PUBLIC-COMMON",
          "EP-SHARED 经别名 xml.SecureFactory 归一到 F-PUBLIC-COMMON")
    check(shared["entry_verdict"] == VERDICT_COMPLIANT,
          "EP-SHARED 与公共工厂配置定义一致，限制完整")

    # 汇总计数（入口级按环境最差结果聚合）
    summary = report["summary"]
    check(summary["by_verdict"][VERDICT_COMPLIANT] == 2,
          "基线：2 个入口限制完整（CATALOG、SHARED）")
    check(summary["by_verdict"][VERDICT_DEVIATION] == 2,
          "基线：2 个入口存在偏离（INVOICE、SUPPLIER 因生产冲突）")
    check(summary["by_verdict"][VERDICT_MISSING] == 1,
          "基线：1 个入口证据缺失（PRICING）")

    # Markdown 报告可渲染且包含修复位置
    md = render_markdown(report)
    for fix in ("src/imports/invoice_import.py:57",
                "src/imports/supplier_import.py:63",
                "src/imports/pricing_import.py:72"):
        check(fix in md, f"Markdown 报告包含修复位置 {fix}")

    return report


def test_fixed_manifest_passes_same_check():
    print("[修复清单] 补齐配置后同一条检查全部通过")
    report = evaluate(load_manifest(FIXED_DIR))

    check(report["summary"]["precedence_conflict_count"] == 0,
          "修复后无覆盖顺序冲突")
    for entry in report["entry_reports"]:
        check(entry["entry_verdict"] == VERDICT_COMPLIANT,
              f"{entry['entry_id']} 修复后入口级判定为限制完整")
        for env in entry["environments"]:
            check(env["verdict"] == VERDICT_COMPLIANT,
                  f"{entry['entry_id']}/{env['environment_id']} 环境级判定合规")
            for item in env["settings"]:
                check(item["status"] == "meets",
                      f"{entry['entry_id']} {item['setting']} 状态为 meets")

    invoice = entry_by_id(report, "EP-INVOICE")
    check(invoice["resolved_factory_id"] == "F-CUSTOM-INVOICE",
          "修复后 EP-INVOICE 工厂标识不变（同一检查、同一入口）")
    env = env_report(invoice, "env-prod")
    status, item = setting_status(env, "feature_load_external_dtd")
    check(status == "meets" and item["effective_value"] is False,
          "修复后 EP-INVOICE 外部 DTD 加载已关闭")
    status, item = setting_status(env, "feature_disallow_doctype_decl")
    check(status == "meets" and item["origin"]["factory_id"] == "F-PUBLIC-COMMON",
          "修复后 EP-INVOICE 经继承获得 DOCTYPE 限制")

    pricing = entry_by_id(report, "EP-PRICING")
    check(pricing.get("dynamic_resolved_to") == "F-PUBLIC-COMMON",
          "修复后 EP-PRICING 动态工厂离线解析到公共安全工厂")
    check(pricing["entry_verdict"] == VERDICT_COMPLIANT,
          "修复后 EP-PRICING 按解析目标评估为限制完整")

    supplier = entry_by_id(report, "EP-SUPPLIER")
    prod_env = env_report(supplier, "env-prod")
    _, item = setting_status(prod_env, "feature_external_general_entities")
    check(item["origin"]["override_id"] == "OV-PROD-002"
          and item["effective_value"] is False,
          "修复后生产环境唯一最高 precedence 覆盖 OV-PROD-002 生效且为安全值")

    return report


def test_report_json_shape():
    print("[结构] 机器可读报告字段完整性")
    report = evaluate(load_manifest(BASELINE_DIR))
    payload = json.loads(json.dumps(report, ensure_ascii=False))
    for key in ("policy_id", "environments_in_scope", "summary",
                "precedence_conflicts", "entry_reports"):
        check(key in payload, f"报告顶层包含 {key}")
    entry = payload["entry_reports"][0]
    for key in ("entry_id", "callsite_chain", "factory_ref",
                "resolved_factory_id", "environments", "fix_location"):
        check(key in entry, f"入口报告包含 {key}")


def main():
    test_baseline_four_categories()
    test_fixed_manifest_passes_same_check()
    test_report_json_shape()
    print("\n全部验收测试通过。")


if __name__ == "__main__":
    main()
