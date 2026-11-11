"""采集引擎：浏览器采集 -> 语义契约检查 -> 隔离/发布 -> 批准后重采。

关键保证：
  * 任何未通过当前已批准契约检查的字段，其记录一律进入 quarantine，不会出现在 published；
  * 列头级违规隔离整列涉及的全部记录，记录级违规只隔离对应记录；
  * 批准操作生成不可变新契约版本，旧规则与受影响记录范围随批准记录永久保留；
  * 引擎只消费浏览器采集结果（RawPage），不读取任何测试真值文件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import checks
from .browser import BrowserDriver, RawPage
from .contracts import (
    STAT_MEAN,
    STAT_MEDIAN,
    TYPE_NUMBER,
    Contract,
)
from .storage import Storage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Engine:
    def __init__(self, storage: Storage, driver: BrowserDriver | None = None) -> None:
        self.storage = storage
        self._external_driver = driver is not None
        self._driver = driver or BrowserDriver()

    # ------------------------------------------------------------------
    # 运行采集任务
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        scenario: str,
        url: str,
        contract_version: str = "v1",
        note: str = "",
    ) -> dict[str, Any]:
        run_id = self.storage.new_run_id()
        run_dir = self.storage.run_dir(run_id)
        contract = self.storage.load_contract(contract_version)

        raw: RawPage = self._driver.collect(
            url,
            contract,
            screenshot_path=str(run_dir / "screenshot.png"),
        )
        (run_dir / "table.html").write_text(raw.table_html, encoding="utf-8")

        result = self._evaluate(run_id=run_id, scenario=scenario, url=url, note=note,
                                contract=contract, raw=raw)
        self.storage.save_run(run_id, result)
        return result

    def _evaluate(
        self,
        *,
        run_id: str,
        scenario: str,
        url: str,
        note: str,
        contract: Contract,
        raw: RawPage,
    ) -> dict[str, Any]:
        violations: list[checks.Violation] = []
        observed: dict[str, dict[str, Any]] = {}

        # 1) 列头语义检查（column 级）
        for cf in contract.fields:
            obs = checks.build_observed(cf, raw.headers.get(cf.key))
            observed[cf.key] = obs.to_dict() if obs else None
            violations.extend(checks.check_header(cf, obs))

        column_bad_keys = {v.field_key for v in violations if v.level == "column"}

        # 2) 单元格数据类型检查（record 级）
        for row_index, row in enumerate(raw.rows):
            for cf in contract.fields:
                violations.extend(checks.check_cell(cf, row_index, row.get(cf.key)))

        record_bad: dict[int, set[str]] = {}
        for v in violations:
            if v.level == "record" and v.row_index is not None:
                record_bad.setdefault(v.row_index, set()).add(v.field_key)

        # 3) 组装记录：published 与 quarantined 严格分离
        published: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        for row_index, row in enumerate(raw.rows):
            bad_keys = column_bad_keys | record_bad.get(row_index, set())
            parsed = self._parse_row(contract, row)
            record = {
                "row_index": row_index,
                "raw": row,
                "values": parsed,
                "violation_codes": sorted(
                    v.code for v in violations
                    if (v.level == "record" and v.row_index == row_index)
                    or (v.level == "column" and v.field_key in bad_keys)
                ),
                "blocked_fields": sorted(bad_keys),
            }
            if bad_keys:
                record["reason"] = self._quarantine_reason(bad_keys, violations, row_index)
                quarantined.append(record)
            else:
                published.append(record)

        status = "passed" if not violations else "quarantined"
        violations_sorted = sorted(violations, key=lambda v: (checks.violation_severity(v.code), v.field_key))

        result = {
            "run_id": run_id,
            "scenario": scenario,
            "note": note,
            "url": url,
            "started_at": raw.collected_at,
            "finished_at": _now_iso(),
            "status": status,
            "contract_version": contract.version,
            "contract": contract.to_dict(),
            "observed_fields": observed,
            "violations": [v.to_dict() for v in violations_sorted],
            "published": published,
            "quarantined": quarantined,
            "evidence": {
                "page_title": raw.page_title,
                "table_found": raw.table_found,
                "row_count": raw.row_count,
                "access": raw.access,
                "screenshot": "screenshot.png",
                "table_html": "table.html",
                "headers": raw.headers,
            },
        }
        return result

    @staticmethod
    def _parse_row(contract: Contract, row: dict[str, str | None]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for cf in contract.fields:
            text = row.get(cf.key)
            if cf.value_type == TYPE_NUMBER:
                values[cf.key] = checks.parse_number(text or "")
            else:
                values[cf.key] = text
        return values

    @staticmethod
    def _quarantine_reason(
        bad_keys: set[str], violations: list[checks.Violation], row_index: int
    ) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for v in violations:
            if v.field_key not in bad_keys:
                continue
            if v.level == "record" and v.row_index != row_index:
                continue
            tag = f"{v.field_key}:{v.code}"
            if tag not in seen:
                seen.add(tag)
                parts.append(v.message)
        return "；".join(parts)

    # ------------------------------------------------------------------
    # 基于观察到的语义生成新字段映射（供用户审阅，批准后才生效）
    # ------------------------------------------------------------------
    @staticmethod
    def build_mapping_suggestion(
        contract: Contract, result: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        suggestions: dict[str, dict[str, Any]] = {}
        observed_map = result["observed_fields"]
        for cf in contract.fields:
            obs = observed_map.get(cf.key)
            if not obs:
                continue
            payload = cf.__dict__.copy()
            # 标签：把实际列头标签纳入允许列表（保留原标签，旧规则可回溯）
            label_text = obs["label_text"]
            if label_text and label_text not in payload["allowed_labels"]:
                payload["allowed_labels"] = (*payload["allowed_labels"], label_text)
            # 单位：采用观察到的、支持子集内的同量纲单位
            if cf.expected_unit and obs["unit"]:
                payload["expected_unit"] = obs["unit"]
            # 统计口径：按观察到的限定说明更新
            if obs["stat"] in (STAT_MEAN, STAT_MEDIAN):
                payload["required_stat"] = obs["stat"]
                if obs["stat"] == STAT_MEDIAN:
                    payload["required_qualifiers"] = ("中位数",)
            suggestions[cf.key] = payload
        return suggestions

    def approve(
        self,
        *,
        run_id: str,
        field_keys: list[str],
        approved_by: str,
        reason: str,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """批准指定字段的新映射，生成新契约版本并保留旧规则与受影响记录范围。"""
        result = self.storage.load_run(run_id)
        base_contract = self.storage.load_contract(result["contract_version"])
        if overrides is None:
            suggestions = self.build_mapping_suggestion(base_contract, result)
            overrides = {key: suggestions[key] for key in field_keys if key in suggestions}
        else:
            overrides = {key: overrides[key] for key in field_keys if key in overrides}
        if not overrides:
            raise ValueError("没有可批准的字段映射")

        # 受影响记录范围：该次运行中被这些字段隔离的记录（列头级=整列全部行）
        affected_rows = sorted(
            {
                rec["row_index"]
                for rec in result["quarantined"]
                if set(rec["blocked_fields"]) & set(field_keys)
            }
        )
        affected_scope = {
            "source_run_id": run_id,
            "field_keys": sorted(field_keys),
            "row_indices": affected_rows,
            "record_count": len(affected_rows),
            "quarantined_total": len(result["quarantined"]),
        }

        new_contract, approval_path = self.storage.create_approval(
            run_id=run_id,
            overrides=overrides,
            base_contract=base_contract,
            affected_scope=affected_scope,
            approved_by=approved_by or "匿名审核人",
            reason=reason or "用户在界面上批准语义映射调整",
        )
        return {
            "new_contract_version": new_contract.version,
            "approval_file": str(approval_path),
            "affected_scope": affected_scope,
            "approved_fields": sorted(field_keys),
        }

    def close(self) -> None:
        if not self._external_driver:
            self._driver.close()
