"""SOAP Envelope 解析：Fault 检测、命名空间化字段与 xop:Include 提取。"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .errors import NS_SOAP11, NS_SOAP12, NS_XOP, NS_LAB


def _qn(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def find_soap_ns(root: ET.Element) -> str | None:
    for ns in (NS_SOAP11, NS_SOAP12):
        if root.tag == _qn(ns, "Envelope"):
            return ns
    return None


def _local_find(parent: ET.Element, soap_ns: str, local_name: str) -> ET.Element | None:
    """同时兼容带 SOAP 命名空间限定与裸本地名两种 1.1 Fault 子元素写法。"""
    el = parent.find(_qn(soap_ns, local_name))
    if el is not None:
        return el
    for child in parent:
        if child.tag.split("}", 1)[-1] == local_name:
            return child
    return None


def extract_fault(root: ET.Element, soap_ns: str) -> dict | None:
    body = root.find(_qn(soap_ns, "Body"))
    if body is None:
        return None
    fault = _local_find(body, soap_ns, "Fault")
    if fault is None:
        return None
    out: dict = {"soap_version": "1.2" if soap_ns == NS_SOAP12 else "1.1"}
    if soap_ns == NS_SOAP12:
        code_el = fault.find(_qn(soap_ns, "Code") + "/" + _qn(soap_ns, "Value"))
        reason_el = fault.find(_qn(soap_ns, "Reason") + "/" + _qn(soap_ns, "Text"))
        node_el = _local_find(fault, soap_ns, "Node")
        out["code"] = (code_el.text or "").strip() if code_el is not None else ""
        out["reason"] = (reason_el.text or "").strip() if reason_el is not None else ""
        out["node"] = (node_el.text or "").strip() if node_el is not None else ""
    else:
        # SOAP 1.1：faultcode/faultstring/faultactor 是 Fault 的直接子元素
        faultcode = _local_find(fault, soap_ns, "faultcode")
        faultstring = _local_find(fault, soap_ns, "faultstring")
        faultactor = _local_find(fault, soap_ns, "faultactor")
        out["code"] = (faultcode.text or "").strip() if faultcode is not None else ""
        out["reason"] = (faultstring.text or "").strip() if faultstring is not None else ""
        out["node"] = (faultactor.text or "").strip() if faultactor is not None else ""
    detail = _local_find(fault, soap_ns, "detail")
    if detail is None:  # SOAP 1.2 为 Detail
        detail = _local_find(fault, soap_ns, "Detail")
    detail_map: dict[str, str] = {}
    if detail is not None:
        for child in detail.iter():
            if child is detail:
                continue
            tag = child.tag.split("}", 1)[-1]
            detail_map[tag] = (child.text or "").strip()
    out["detail"] = detail_map
    return out


def _xop_href(channel: ET.Element) -> tuple[str | None, ET.Element | None]:
    """返回 (cid, include 元素)；无 xop:Include 时返回 (None, None)。"""
    # Include 嵌在 lab:BinarySample 内，find 只查直接子元素，故用 iter 深查
    inc = next(channel.iter(_qn(NS_XOP, "Include")), None)
    if inc is None:
        return None, None
    href = inc.get("href", "").strip()
    if href.lower().startswith("cid:"):
        href = href[4:].strip()
    # 兼容 RFC 2392 的 cid:<...> 尖括号写法
    if href.startswith("<") and href.endswith(">"):
        href = href[1:-1].strip()
    elif href.startswith("<"):
        href = href[1:].strip()
    return href or None, inc


def extract_measurements(root: ET.Element, soap_ns: str) -> dict:
    """提取命名空间化的测量字段与每个通道的 cid 引用。"""
    body = root.find(_qn(soap_ns, "Body"))
    if body is None:
        return {"response_tag": None, "instrument_id": None, "measurements": []}
    resp = body.find(_qn(NS_LAB, "MeasurementQueryResponse"))
    if resp is None:
        return {"response_tag": None, "instrument_id": None, "measurements": []}

    instrument_id = (resp.findtext(_qn(NS_LAB, "InstrumentId")) or "").strip()
    measurements: list[dict] = []
    for meas in resp.findall(_qn(NS_LAB, "Measurement")):
        meas_id = (meas.findtext(_qn(NS_LAB, "MeasurementId")) or "").strip()
        sampled_at = (meas.findtext(_qn(NS_LAB, "SampledAt")) or "").strip()
        channels: list[dict] = []
        for ch in meas.findall(_qn(NS_LAB, "Channel")):
            code = (ch.findtext(_qn(NS_LAB, "Code")) or "").strip()
            unit = (ch.findtext(_qn(NS_LAB, "Unit")) or "").strip()
            expected_digest = (ch.findtext(_qn(NS_LAB, "SummaryDigest")) or "").strip()
            cid, inc = _xop_href(ch)
            channels.append({
                "code": code,
                "unit": unit,
                "summary_digest_xml": expected_digest,
                "cid": cid,
                "has_xop_include": inc is not None,
            })
        measurements.append({
            "measurement_id": meas_id,
            "sampled_at": sampled_at,
            "channels": channels,
        })
    return {
        "response_tag": "MeasurementQueryResponse",
        "instrument_id": instrument_id,
        "measurements": measurements,
    }
