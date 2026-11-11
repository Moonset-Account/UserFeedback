"""提取流水线：记录读取 → 摘要校验 → revisit 显式引用还原 → 字段提取。

关键规则（对应验收要求）：

1. 归档记录标识（WARC-Record-ID）与目标 URL（WARC-Target-URI）全程分开处理；
2. revisit 只按「明确引用」还原正文：
   - 首选 WARC-Refers-To 指向的记录 ID；
   - 其次 WARC-Refers-To-Target-URI + WARC-Refers-To-Date 精确匹配；
   - **绝不**把「最近一个同 URL 正文」当作任意 revisit 的引用目标；
3. 被引用记录自身摘要校验失败时，不允许作为可信正文复用；
4. 引用不存在、引用类型不可复用、坏摘要均产出明确错误，流水线不中断；
5. 同 URL 多个时间版本逐条独立提取、独立断言。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .digests import compute_digest, sha256_hex, verify_digest
from .extract_fields import extract_fields
from .reader import MemberError, RawRecord, read_warc_file

REVISIT_PROFILES = (
    "http://netpreserve.org/warc/1.1/revisit/server-not-modified",
    "http://netpreserve.org/warc/1.0/revisit/identical-payload-digest",
)
REUSABLE_TYPES = ("response", "resource")


@dataclass
class ExtractEntry:
    index: int
    rec_type: str
    record_id: str | None
    target_uri: str | None
    warc_date: str | None
    http_statusline: str | None
    file_offset: int
    member_index: int
    digest_ok: bool | None
    digest_label: str | None
    actual_payload_digest: str | None
    payload_sha256: str | None
    fields: dict | None = None
    # revisit 复用信息
    reused_from_record_id: str | None = None
    reused_from_index: int | None = None
    reused_by_explicit_ref: bool = False
    reference_rule: str | None = None
    # 关联的 request 记录（WARC-Concurrent-To）
    request_record_ids: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _check_payload_digest(raw: RawRecord) -> tuple[bool | None, str | None, str | None]:
    label = raw.header("WARC-Payload-Digest")
    if raw.rec_type == "revisit":
        # revisit 记录块本身不含被复用正文，摘要标签描述的是「被引用的正文」
        return None, label, None
    if label is None or not raw.payload:
        return None, label, None
    actual = compute_digest(raw.payload, (label.split(":", 1)[0] if label and ":" in label else "sha1"))
    result = verify_digest(raw.payload, label)
    return result, label, actual


def _normalize_warc_date(value: str | None) -> str | None:
    """把 WARC-Date 归一化到 UTC 秒级时间戳，容忍 ``Z`` 与 ``+00:00`` 等写法。"""
    if not value:
        return None
    text = value.strip()
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return text  # 无法解析时按原值参与匹配
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _index_records(records: list[RawRecord]):
    by_id: dict[str, RawRecord] = {}
    by_uri_date: dict[tuple[str, str], RawRecord] = {}
    for raw in records:
        if raw.record_id:
            by_id[raw.record_id] = raw
        # URI+Date 索引只放可复用正文记录，避免 request 等同键记录覆盖
        if (raw.target_uri and raw.warc_date
                and raw.rec_type in REUSABLE_TYPES):
            key = (raw.target_uri, _normalize_warc_date(raw.warc_date))
            by_uri_date.setdefault(key, raw)
    return by_id, by_uri_date


def resolve_revisit(raw: RawRecord, records: list[RawRecord],
                    digest_results: dict[int, tuple[bool | None, str | None, str | None]],
                    by_id: dict[str, RawRecord],
                    by_uri_date: dict[tuple[str, str], RawRecord]) -> dict:
    """按明确引用还原 revisit 的有效正文。

    返回 dict：reused_raw / reused_from_id / rule / error。
    """
    refers_to = (raw.header("WARC-Refers-To") or "").strip("<>") or None
    ref_uri = raw.header("WARC-Refers-To-Target-URI")
    ref_date = raw.header("WARC-Refers-To-Date")
    profile = raw.header("WARC-Profile")
    if profile and profile not in REVISIT_PROFILES:
        return {"error": f"不支持的 WARC-Profile：{profile}，无法确定复用规则"}

    target: RawRecord | None = None
    rule = None

    # 规则 1：显式记录 ID 引用（最高优先级，且只按 ID 解析）
    if refers_to:
        target = by_id.get(refers_to)
        rule = "warc-refers-to-record-id"
        if target is None:
            return {
                "error": (
                    f"WARC-Refers-To 指向的记录 {refers_to} 在归档中不存在"
                    "（缺失引用，不使用同 URL 其他记录兜底）"
                ),
                "rule": rule,
                "ref_id": refers_to,
            }
    # 规则 2：显式 URL + 日期精确匹配
    elif ref_uri and ref_date:
        normalized_date = _normalize_warc_date(ref_date)
        target = by_uri_date.get((ref_uri, normalized_date))
        rule = "warc-refers-to-uri-and-date"
        if target is None:
            return {
                "error": (
                    f"引用目标 {ref_uri} @ {ref_date} 在归档中不存在"
                    "（缺失引用，不使用同 URL 最近记录兜底）"
                ),
                "rule": rule,
                "ref_id": None,
            }
    else:
        return {
            "error": "revisit 记录缺少 WARC-Refers-To 或 WARC-Refers-To-Target-URI/Date 明确引用，无法还原正文",
            "rule": None,
            "ref_id": None,
        }

    if target.rec_type not in REUSABLE_TYPES:
        return {
            "error": f"引用记录 {target.record_id} 类型为 {target.rec_type}，不是可复用正文（response/resource）",
            "rule": rule,
            "ref_id": target.record_id,
        }

    target_digest_ok = digest_results.get(target.index, (None,))[0]
    if target_digest_ok is False:
        return {
            "error": (
                f"引用记录 {target.record_id} 的 WARC-Payload-Digest 校验失败，"
                "其正文不可信，拒绝复用"
            ),
            "rule": rule,
            "ref_id": target.record_id,
        }

    # 摘要交叉核对：revisit 自带的 Payload-Digest 应与被引用正文一致
    revisit_label = raw.header("WARC-Payload-Digest")
    if revisit_label and target.payload:
        cross = verify_digest(target.payload, revisit_label)
        if cross is False:
            return {
                "error": (
                    f"revisit 摘要 {revisit_label} 与被引用记录 {target.record_id} "
                    "的实际正文摘要不一致，拒绝复用"
                ),
                "rule": rule,
                "ref_id": target.record_id,
            }

    return {
        "reused_raw": target,
        "reused_from_id": target.record_id,
        "rule": rule,
        "error": None,
        "ref_id": target.record_id,
    }


def run_pipeline(path: str) -> dict:
    """对一个 WARC 文件执行完整提取，返回可 JSON 序列化的结果。"""
    records: list[RawRecord]
    member_errors: list[MemberError]
    records, member_errors = read_warc_file(path)

    digest_results: dict[int, tuple[bool | None, str | None, str | None]] = {}
    for raw in records:
        digest_results[raw.index] = _check_payload_digest(raw)

    by_id, by_uri_date = _index_records(records)

    # request → 其并发 response 的映射
    concurrent_map: dict[str, list[str]] = {}
    for raw in records:
        if raw.rec_type == "request":
            parent = (raw.header("WARC-Concurrent-To") or "").strip("<>")
            if parent:
                concurrent_map.setdefault(parent, []).append(raw.record_id)

    entries: list[ExtractEntry] = []
    for raw in records:
        digest_ok, digest_label, actual_digest = digest_results[raw.index]
        entry = ExtractEntry(
            index=raw.index,
            rec_type=raw.rec_type,
            record_id=raw.record_id,
            target_uri=raw.target_uri,
            warc_date=raw.warc_date,
            http_statusline=raw.http_statusline,
            file_offset=raw.file_offset,
            member_index=raw.member_index,
            digest_ok=digest_ok,
            digest_label=digest_label,
            actual_payload_digest=actual_digest,
            payload_sha256=sha256_hex(raw.payload) if raw.payload else None,
            request_record_ids=list(concurrent_map.get(raw.record_id or "", [])),
            errors=list(raw.errors),
        )

        if digest_ok is False:
            entry.errors.append(
                f"坏摘要：头部 {digest_label} 与实际正文 {actual_digest} 不匹配"
            )

        if raw.rec_type in ("response", "revisit", "request", "resource"):
            effective_payload = raw.payload
            content_type = raw.http_header("Content-Type")

            if raw.rec_type == "revisit":
                resolution = resolve_revisit(raw, records, digest_results, by_id, by_uri_date)
                entry.reference_rule = resolution.get("rule")
                if resolution.get("error"):
                    entry.errors.append(resolution["error"])
                    entry.reused_by_explicit_ref = False
                else:
                    reused: RawRecord = resolution["reused_raw"]
                    effective_payload = reused.payload
                    content_type = reused.http_header("Content-Type")
                    entry.reused_from_record_id = reused.record_id
                    entry.reused_from_index = reused.index
                    entry.reused_by_explicit_ref = True
                    entry.payload_sha256 = sha256_hex(effective_payload)
                    entry.warnings.append(
                        f"正文按明确引用（{entry.reference_rule}）复用自记录 "
                        f"{reused.record_id}；HTTP 响应头仍取本 revisit 记录"
                    )

            if raw.rec_type in ("response", "revisit", "resource") and effective_payload:
                if content_type and "html" in content_type.lower():
                    extracted = extract_fields(effective_payload, raw.target_uri or "", content_type)
                    entry.fields = extracted["fields"]
                    entry.payload_sha256 = extracted["payload_sha256"]
                    entry.parse_errors = extracted["parse_errors"]
                    if extracted["parse_errors"]:
                        entry.errors.extend(extracted["parse_errors"])
                elif content_type is None and (
                    effective_payload[:15].lstrip().lower().startswith(b"<!doctype html")
                    or b"<html" in effective_payload[:200].lower()
                ):
                    extracted = extract_fields(effective_payload, raw.target_uri or "")
                    entry.fields = extracted["fields"]
                    entry.payload_sha256 = extracted["payload_sha256"]
                    entry.warnings.append("响应未声明 Content-Type，按内容嗅探为 HTML 处理")
                else:
                    entry.warnings.append(
                        f"非 HTML 负载（Content-Type={content_type or '未知'}），仅记录摘要不提取字段"
                    )
            elif raw.rec_type == "revisit" and not entry.reused_from_record_id:
                pass  # 引用失败错误已记录
        entries.append(entry)

    return {
        "file": path,
        "total_records": len(records),
        "record_type_counts": _count_types(records),
        "member_errors": [asdict(e) for e in member_errors],
        "entries": [e.to_dict() for e in entries],
        "raw_records": [r.to_summary() for r in records],
    }


def _count_types(records: list[RawRecord]) -> dict:
    counts: dict[str, int] = {}
    for raw in records:
        counts[raw.rec_type] = counts.get(raw.rec_type, 0) + 1
    return counts
