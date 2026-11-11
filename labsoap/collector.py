"""采集器主编排：MIME 解析 → 安全 XML → Content-ID 关联 → 附件解码 → 摘要校验。

核心原则：
- 关联只按 Content-ID 与 xop:Include 的 cid 引用进行，绝不使用附件出现顺序；
- 完整且每个通道摘要正确的测量组才进入正式结果；
- 所有解析后的数值都带 XML 字段与附件字节两类来源证据；
- 任何错误都转成带错误码、可解释信息与证据的结构化结论，不抛裸异常给页面。
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

from . import sources
from .digest import channel_digest
from .envelope import extract_fault, extract_measurements, find_soap_ns
from .errors import (
    CollectorError,
    ERR_DANGLING_PART,
    ERR_INTERNAL,
    ERR_MEASUREMENT_ID_MISMATCH,
    ERR_MISSING_REFERENCE,
    ERR_SAMPLE_FORMAT,
    ERR_SOAP_FAULT,
    ERR_SUMMARY_MISMATCH,
    ERR_UNKNOWN_SOURCE,
    ERR_XML_MALFORMED,
    STATUS_COMPLETE,
    STATUS_FAULT,
    STATUS_FETCH_ERROR,
    STATUS_REJECTED,
    SUPPORTED_SUBSET,
)
from .mime_parser import parse_mime_message
from .sample_format import parse_labsmp
from .secure_xml import parse_secure_xml


def _part_evidence(part) -> dict:
    return {
        "part_index": part.index,
        "content_id": part.content_id,
        "content_type": part.content_type,
        "byte_length": len(part.payload),
        "sha256": hashlib.sha256(part.payload).hexdigest(),
        "hex_preview": part.payload[:64].hex(),
        "base64": base64.b64encode(part.payload).decode("ascii"),
        "headers": part.raw_headers,
    }


def _xml_field_evidence(element_tag: str, text: str) -> dict:
    return {"source": "xml_field", "field": element_tag, "value": text}


def collect(source: dict) -> dict:
    """执行一次采集。source: {"kind": "http"|"file", "ref": ...}。"""
    started_at = datetime.now(timezone.utc).isoformat()
    base = {
        "source": source,
        "started_at": started_at,
        "status": None,
        "error": None,
        "measurement_groups": [],
        "fault": None,
        "raw_evidence": None,
        "supported_subset": SUPPORTED_SUBSET,
    }
    try:
        if source.get("kind") == "http":
            fetched = sources.fetch_http(source["ref"])
        elif source.get("kind") == "file":
            fetched = sources.fetch_file(source["ref"])
        else:
            raise CollectorError(ERR_UNKNOWN_SOURCE, f"未知来源类型：{source.get('kind')!r}")

        raw = fetched["raw"]
        parsed = parse_mime_message(raw)
        base["raw_evidence"] = {
            "source_kind": fetched["source_kind"],
            "source_ref": fetched["source_ref"],
            "http_status": fetched["http_status"],
            "outer_content_type": parsed.content_type_header,
            "boundary": parsed.boundary,
            "total_bytes": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "parts": [_part_evidence(p) for p in parsed.parts],
        }

        root_el = parse_secure_xml(parsed.root.payload)
        soap_ns = find_soap_ns(root_el)
        if soap_ns is None:
            raise CollectorError(ERR_XML_MALFORMED, "根 XML 不是 SOAP 1.1/1.2 Envelope")

        # 1) SOAP Fault：业务错误，结构化返回（不算采集崩溃）
        fault = extract_fault(root_el, soap_ns)
        if fault is not None:
            base["status"] = STATUS_FAULT
            base["fault"] = fault
            base["error"] = CollectorError(
                ERR_SOAP_FAULT,
                "；".join(part for part in (fault.get("code", ""), fault.get("reason", "")) if part),
                evidence={"fault_detail": fault.get("detail", {})},
            ).to_dict()
            return base

        envelope_data = extract_measurements(root_el, soap_ns)
        if envelope_data["response_tag"] is None:
            raise CollectorError(ERR_XML_MALFORMED, "Body 中未找到 lab:MeasurementQueryResponse")

        # 2) 收集全部 cid 引用，检查缺失与孤立附件
        referenced_cids: set[str] = set()
        missing: list[dict] = []
        for meas in envelope_data["measurements"]:
            for ch in meas["channels"]:
                if not ch["has_xop_include"]:
                    continue
                cid = ch["cid"]
                referenced_cids.add(cid)
                if cid not in parsed.cid_index:
                    missing.append({
                        "measurement_id": meas["measurement_id"],
                        "channel_code": ch["code"],
                        "missing_cid": cid,
                    })
        if missing:
            raise CollectorError(
                ERR_MISSING_REFERENCE,
                f"{len(missing)} 个 xop:Include 引用的附件缺失："
                + "；".join(f"{m['measurement_id']}/{m['channel_code']} -> {m['missing_cid']}" for m in missing),
                evidence={"missing_references": missing},
            )

        attachment_cids = {p.content_id for p in parsed.parts if p is not parsed.root and p.content_id}
        dangling = sorted(attachment_cids - referenced_cids)
        if dangling:
            raise CollectorError(
                ERR_DANGLING_PART,
                f"存在未被引用的孤立附件：{', '.join(dangling)}",
                evidence={"dangling_cids": dangling},
            )

        # 3) 逐测量组关联（按 CID，不用顺序）、解码、校验
        groups: list[dict] = []
        group_errors: list[dict] = []
        for meas in envelope_data["measurements"]:
            group = {
                "measurement_id": meas["measurement_id"],
                "sampled_at": meas["sampled_at"],
                "instrument_id": envelope_data["instrument_id"],
                "channels": [],
                "accepted": True,
                "group_errors": [],
            }
            for ch in meas["channels"]:
                cid = ch["cid"]
                part = parsed.cid_index.get(cid)  # 仅按标识查找
                channel_result = {
                    "code": ch["code"],
                    "unit": ch["unit"],
                    "cid": cid,
                    "value_provenance": {
                        "code": _xml_field_evidence("lab:Code", ch["code"]),
                        "unit": _xml_field_evidence("lab:Unit", ch["unit"]),
                        "summary_digest_xml": _xml_field_evidence("lab:SummaryDigest", ch["summary_digest_xml"]),
                        "attachment": _part_evidence(part) if part is not None else None,
                    },
                }
                if part is None:  # 理论上已被前面的 missing 检查拦截，防御性处理
                    group["accepted"] = False
                    group["group_errors"].append({
                        "channel_code": ch["code"], "code": ERR_MISSING_REFERENCE,
                        "detail": f"cid {cid} 无对应附件",
                    })
                    continue
                try:
                    sample = parse_labsmp(part.payload)
                except CollectorError as exc:
                    group["accepted"] = False
                    group["group_errors"].append({
                        "channel_code": ch["code"], "code": exc.code, "detail": exc.detail,
                    })
                    continue
                if sample["measurement_id"] != meas["measurement_id"]:
                    group["accepted"] = False
                    group["group_errors"].append({
                        "channel_code": ch["code"],
                        "code": ERR_MEASUREMENT_ID_MISMATCH,
                        "detail": f"附件内嵌 '{sample['measurement_id']}' 与 XML MeasurementId '{meas['measurement_id']}' 不一致",
                    })
                recomputed = channel_digest(ch["code"], part.payload)
                digest_ok = recomputed == ch["summary_digest_xml"]
                if not digest_ok:
                    group["accepted"] = False
                    group["group_errors"].append({
                        "channel_code": ch["code"],
                        "code": ERR_SUMMARY_MISMATCH,
                        "detail": f"XML 摘要 {ch['summary_digest_xml'][:16]}… 与重算 {recomputed[:16]}… 不一致",
                        "evidence": {"expected_xml": ch["summary_digest_xml"], "recomputed": recomputed},
                    })
                channel_result["sample"] = {
                    "format": "LABSMP v1",
                    "point_count": sample["count"],
                    "values": sample["values"],
                    "embedded_measurement_id": sample["measurement_id"],
                    "attachment_byte_length": len(part.payload),
                    "attachment_sha256": hashlib.sha256(part.payload).hexdigest(),
                }
                channel_result["summary_check"] = {
                    "digest_xml": ch["summary_digest_xml"],
                    "digest_recomputed": recomputed,
                    "match": digest_ok,
                    "algorithm": "sha256(utf8(channel_code) + NUL + raw_attachment_bytes)",
                }
                group["channels"].append(channel_result)
            if group["accepted"]:
                groups.append(group)
            else:
                group_errors.append({"measurement_id": group["measurement_id"], "errors": group["group_errors"]})

        if group_errors:
            raise CollectorError(
                ERR_SUMMARY_MISMATCH if any(
                    e["code"] == ERR_SUMMARY_MISMATCH
                    for ge in group_errors for e in ge["errors"]
                ) else ERR_SAMPLE_FORMAT,
                f"{len(group_errors)} 个测量组未通过完整校验，不进入正式结果",
                evidence={"rejected_groups": group_errors, "parsed_groups_preview": groups},
            )

        base["status"] = STATUS_COMPLETE
        base["measurement_groups"] = groups
        return base

    except CollectorError as exc:
        base["status"] = STATUS_FAULT if exc.code == ERR_SOAP_FAULT else (
            STATUS_FETCH_ERROR if exc.code in ("HTTP_FETCH_ERROR", "UNKNOWN_SOURCE") else STATUS_REJECTED
        )
        base["error"] = exc.to_dict()
        return base
    except Exception as exc:  # 防御：任何意外都转结构化错误
        base["status"] = STATUS_REJECTED
        base["error"] = CollectorError(ERR_INTERNAL, repr(exc)).to_dict()
        return base
