"""SOAP/MTOM 报文手工构建器（样例生成器侧，与采集器代码完全分离）。

构建的协议子集：
- 外层 multipart/related；type="application/soap+xml"；start 指向根 part；
- 根 part 为 SOAP 1.1 Envelope，字段使用 urn:fictional-lab-instrument:v1 命名空间；
- 附件 part 携带 Content-ID，二进制 LABSMP v1 使用 application/octet-stream 传输编码；
- xop:Include 以 href="cid:..." 引用附件，关联键只有 Content-ID。
"""
from __future__ import annotations

import struct

from labsoap.digest import channel_digest
from labsoap.errors import LABSMP_MAGIC, LABSMP_SAMPLE_MEDIA_TYPE, NS_LAB, NS_SOAP11, NS_XOP

BOUNDARY = "----=fictional-lab-mtom-boundary-7f3a91"
_ROOT_CID = "rootpart@fictional-lab.invalid"


def build_labsmp(measurement_id: str, values: list[float]) -> bytes:
    mid = measurement_id.encode("utf-8")
    if len(mid) > 32:
        raise ValueError("measurement_id 超过 32 字节")
    return LABSMP_MAGIC + struct.pack("<I", len(values)) + struct.pack(f"<{len(values)}d", *values) \
        + mid.ljust(32, b"\x00")


def cid_for(measurement_id: str, code: str) -> str:
    return f"{measurement_id}.{code}@fictional-lab.invalid"


def _attachment_headers(cid: str, media_type: str, binary: bool) -> list[bytes]:
    lines = [
        f"Content-ID: <{cid}>".encode("ascii"),
        f"Content-Type: {media_type}".encode("ascii"),
    ]
    if binary:
        lines.append(b"Content-Transfer-Encoding: binary")
    else:
        lines.append(b"Content-Transfer-Encoding: 7bit")
    return lines


def _build_part(headers: list[bytes], payload: bytes) -> bytes:
    return b"\r\n".join(headers) + b"\r\n\r\n" + payload


def build_envelope_xml(scenario: str, spec: dict, *, corrupt_digest: bool = False) -> bytes:
    """生成带命名空间字段与 xop:Include 的 SOAP Envelope 字节。"""
    if "fault" in spec:
        f = spec["fault"]
        detail_items = "".join(
            f"<lab:{k}>{_xml_escape(v)}</lab:{k}>" for k, v in f["detail"].items()
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\r\n'
            f'<soap:Envelope xmlns:soap="{NS_SOAP11}" xmlns:lab="{NS_LAB}" '
            f'xmlns:xop="{NS_XOP}">\r\n'
            '  <soap:Body>\r\n'
            '    <soap:Fault>\r\n'
            f'      <faultcode>{_xml_escape(f["code"])}</faultcode>\r\n'
            f'      <faultstring>{_xml_escape(f["reason"])}</faultstring>\r\n'
            f'      <detail>{detail_items}</detail>\r\n'
            '    </soap:Fault>\r\n'
            '  </soap:Body>\r\n'
            '</soap:Envelope>\r\n'
        )
        return xml.encode("utf-8")

    parts_xml: list[str] = []
    for meas in spec["measurements"]:
        channels_xml: list[str] = []
        for ch in meas["channels"]:
            cid = cid_for(meas["measurement_id"], ch["code"])
            sample = build_labsmp(meas["measurement_id"], ch["values"])
            digest = channel_digest(ch["code"], sample)
            if corrupt_digest and ch["code"] == "PH_03":
                digest = "0" * 64  # 故意写错的摘要
            channels_xml.append(
                "        <lab:Channel>\r\n"
                f'          <lab:Code>{_xml_escape(ch["code"])}</lab:Code>\r\n'
                f'          <lab:Unit>{_xml_escape(ch["unit"])}</lab:Unit>\r\n'
                "          <lab:BinarySample>\r\n"
                f'            <xop:Include xmlns:xop="{NS_XOP}" href="cid:{cid}"/>\r\n'
                "          </lab:BinarySample>\r\n"
                f"          <lab:SummaryDigest>{digest}</lab:SummaryDigest>\r\n"
                "        </lab:Channel>\r\n"
            )
        parts_xml.append(
            "      <lab:Measurement>\r\n"
            f'        <lab:MeasurementId>{_xml_escape(meas["measurement_id"])}</lab:MeasurementId>\r\n'
            f'        <lab:SampledAt>{_xml_escape(meas["sampled_at"])}</lab:SampledAt>\r\n'
            + "".join(channels_xml)
            + "      </lab:Measurement>\r\n"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n'
        f'<soap:Envelope xmlns:soap="{NS_SOAP11}" xmlns:lab="{NS_LAB}" xmlns:xop="{NS_XOP}">\r\n'
        '  <soap:Body>\r\n'
        '    <lab:MeasurementQueryResponse>\r\n'
        f'      <lab:InstrumentId>{_xml_escape(spec["instrument_id"])}</lab:InstrumentId>\r\n'
        + "".join(parts_xml)
        + '    </lab:MeasurementQueryResponse>\r\n'
        '  </soap:Body>\r\n'
        '</soap:Envelope>\r\n'
    )
    return xml.encode("utf-8")


def build_attachment_parts(spec: dict, *, drop_cids: set[str] | None = None,
                           duplicate_cid: str | None = None) -> list[tuple[list[bytes], bytes, str]]:
    """返回 (headers, payload, cid) 附件 part 列表。"""
    drop_cids = drop_cids or set()
    out: list[tuple[list[bytes], bytes, str]] = []
    for meas in spec["measurements"]:
        for ch in meas["channels"]:
            cid = cid_for(meas["measurement_id"], ch["code"])
            if cid in drop_cids:
                continue
            payload = build_labsmp(meas["measurement_id"], ch["values"])
            headers = _attachment_headers(cid, LABSMP_SAMPLE_MEDIA_TYPE, binary=True)
            out.append((headers, payload, cid))
            if duplicate_cid and cid.endswith("TEMP_21@fictional-lab.invalid"):
                # 再追加一个同 Content-ID 但字节内容不同的冲突 part
                conflict_payload = build_labsmp(meas["measurement_id"], [v + 100.0 for v in ch["values"]])
                out.append((_attachment_headers(cid, LABSMP_SAMPLE_MEDIA_TYPE, binary=True),
                            conflict_payload, cid))
    return out


def build_mime_entity(scenario: str, spec: dict) -> bytes:
    """按场景规则组装完整 multipart/related 实体字节。"""
    corrupt = scenario == "digest_mismatch"
    envelope = build_envelope_xml(scenario, spec, corrupt_digest=corrupt)
    root_headers = [
        f"Content-ID: <{_ROOT_CID}>".encode("ascii"),
        b"Content-Type: text/xml; charset=UTF-8",
        b"Content-Transfer-Encoding: 8bit",
    ]
    root_part = _build_part(root_headers, envelope)

    if "fault" in spec:
        ordered = [root_part]
    else:
        drop = set()
        dup = None
        if scenario == "missing_reference":
            target = spec["measurements"][0]
            drop = {cid_for(target["measurement_id"], "HUM_09")}
        if scenario == "duplicate_cid":
            dup = "dup"
        attachments = build_attachment_parts(spec, drop_cids=drop, duplicate_cid=dup)
        attachment_parts = [_build_part(h, p) for h, p, _ in attachments]
        if scenario == "parts_reordered":
            ordered = attachment_parts + [root_part]  # 附件先于 XML 到达
        else:
            ordered = [root_part] + attachment_parts

    boundary = BOUNDARY.encode("ascii")
    # 标准 MIME 多部分布局：边界行 / part / CRLF+边界行 / part … / CRLF+闭合边界
    chunks: list[bytes] = []
    for i, chunk in enumerate(ordered):
        if i == 0:
            chunks.append(b"--" + boundary + b"\r\n" + chunk)
        else:
            chunks.append(b"\r\n--" + boundary + b"\r\n" + chunk)
    chunks.append(b"\r\n--" + boundary + b"--\r\n")
    body = b"".join(chunks)
    outer = (
        f'Content-Type: multipart/related; type="text/xml"; '
        f'boundary="{BOUNDARY}"; start="<{_ROOT_CID}>"\r\n\r\n'
    ).encode("ascii")
    return outer + body


def _xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
