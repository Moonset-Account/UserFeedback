"""合规核对核心逻辑：重登记设备的卷加密与当前密钥托管核验。

业务规则（来自需求基线）：
- 资产清单给出当前登记批次（batch）和每台设备应加密的卷。
- 对每个（资产, 批次, 卷），只取当前批次内序号（sequence）最高的上报；
  同一最高序号存在内容冲突（观测时间/加密状态/密钥标识不一致）时标为待复核。
- 通过条件：卷已加密、观测时间严格早于核对时间且相差不超过 24 小时、
  且存在同时匹配 资产+登记批次+卷+当前密钥标识 的托管回执。
- 设备轮换恢复密钥后，旧密钥的回执不再有效，只有新密钥的匹配回执才能重新通过。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MAX_REPORT_AGE = timedelta(hours=24)

STATUS_PASS = "PASS"
STATUS_UNENCRYPTED = "UNENCRYPTED"
STATUS_MISSING_REPORT = "MISSING_REPORT"
STATUS_STALE_REPORT = "STALE_REPORT"
STATUS_SEQUENCE_CONFLICT = "SEQUENCE_CONFLICT"
STATUS_AWAITING_ESCROW = "AWAITING_ESCROW"

ALL_STATUSES = (
    STATUS_PASS,
    STATUS_UNENCRYPTED,
    STATUS_MISSING_REPORT,
    STATUS_STALE_REPORT,
    STATUS_SEQUENCE_CONFLICT,
    STATUS_AWAITING_ESCROW,
)

REPORT_REQUIRED_FIELDS = (
    "asset_id",
    "batch",
    "volume",
    "sequence",
    "observed_at",
    "encrypted",
    "key_id",
)
RECEIPT_REQUIRED_FIELDS = ("asset_id", "batch", "volume", "key_id")


def parse_time(value, field="time"):
    """解析 ISO 8601 时间；无显式时区时按 UTC 处理。支持结尾 Z。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 ISO 8601 字符串，实际: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法 ISO 8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _require(record, fields, kind, index):
    missing = [f for f in fields if f not in record]
    if missing:
        raise ValueError(f"{kind}[{index}] 缺少字段: {', '.join(missing)}")


def validate_reports(reports):
    """校验并规范化加密上报记录。"""
    if not isinstance(reports, list):
        raise ValueError("加密上报必须是数组")
    norm = []
    for i, r in enumerate(reports):
        if not isinstance(r, dict):
            raise ValueError(f"reports[{i}] 必须是对象")
        _require(r, REPORT_REQUIRED_FIELDS, "reports", i)
        seq = r["sequence"]
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError(f"reports[{i}].sequence 必须是整数")
        enc = r["encrypted"]
        if not isinstance(enc, bool):
            raise ValueError(f"reports[{i}].encrypted 必须是布尔值")
        parse_time(r["observed_at"], f"reports[{i}].observed_at")  # 仅校验格式
        norm.append(
            {
                "asset_id": str(r["asset_id"]),
                "batch": str(r["batch"]),
                "volume": str(r["volume"]),
                "sequence": seq,
                "observed_at": str(r["observed_at"]),
                "encrypted": enc,
                "key_id": str(r["key_id"]),
            }
        )
    return norm


def validate_receipts(receipts):
    """校验并规范化恢复密钥托管回执。"""
    if not isinstance(receipts, list):
        raise ValueError("托管回执必须是数组")
    norm = []
    for i, r in enumerate(receipts):
        if not isinstance(r, dict):
            raise ValueError(f"receipts[{i}] 必须是对象")
        _require(r, RECEIPT_REQUIRED_FIELDS, "receipts", i)
        if "received_at" in r:
            parse_time(r["received_at"], f"receipts[{i}].received_at")
        norm.append(
            {
                "asset_id": str(r["asset_id"]),
                "batch": str(r["batch"]),
                "volume": str(r["volume"]),
                "key_id": str(r["key_id"]),
            }
        )
    return norm


def evaluate_volume(asset_id, batch, volume, reports, receipts, check_time):
    """对单个（资产, 批次, 卷）做核对，返回卷级结果字典。"""
    base = {"asset_id": asset_id, "batch": batch, "volume": volume}
    candidates = [
        r
        for r in reports
        if r["asset_id"] == asset_id and r["batch"] == batch and r["volume"] == volume
    ]
    if not candidates:
        return {**base, "status": STATUS_MISSING_REPORT, "reason": "当前登记批次内无该卷上报"}

    max_seq = max(r["sequence"] for r in candidates)
    top = [r for r in candidates if r["sequence"] == max_seq]
    fingerprints = {
        (parse_time(r["observed_at"]), r["encrypted"], r["key_id"]) for r in top
    }
    if len(fingerprints) > 1:
        return {
            **base,
            "status": STATUS_SEQUENCE_CONFLICT,
            "sequence": max_seq,
            "reason": f"最高序号 {max_seq} 存在 {len(top)} 条内容冲突的上报，待复核",
        }

    current = top[0]
    observed = parse_time(current["observed_at"])
    key_id = current["key_id"]
    detail = {
        **base,
        "sequence": max_seq,
        "observed_at": current["observed_at"],
        "key_id": key_id,
    }

    if not current["encrypted"]:
        return {**detail, "status": STATUS_UNENCRYPTED, "reason": "最新上报显示卷未加密"}

    if not (observed < check_time and check_time - observed <= MAX_REPORT_AGE):
        return {
            **detail,
            "status": STATUS_STALE_REPORT,
            "reason": "观测时间不在 (核对时间-24h, 核对时间) 窗口内",
        }

    matched = [
        rc
        for rc in receipts
        if rc["asset_id"] == asset_id
        and rc["batch"] == batch
        and rc["volume"] == volume
        and rc["key_id"] == key_id
    ]
    if not matched:
        return {
            **detail,
            "status": STATUS_AWAITING_ESCROW,
            "reason": "无同时匹配资产/批次/卷/当前密钥标识的托管回执",
        }

    return {**detail, "status": STATUS_PASS, "matched_receipts": len(matched)}


def evaluate(inventory, reports, receipts, check_time):
    """整体核对：逐卷评估并汇总到设备与全局。"""
    if not isinstance(inventory, dict):
        raise ValueError("资产清单必须是对象")
    default_batch = inventory.get("batch")
    assets = inventory.get("assets")
    if not isinstance(assets, list):
        raise ValueError("资产清单缺少 assets 数组")

    reports = validate_reports(reports)
    receipts = validate_receipts(receipts)

    devices = []
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict) or "asset_id" not in asset:
            raise ValueError(f"assets[{idx}] 必须是含 asset_id 的对象")
        asset_id = str(asset["asset_id"])
        batch = asset.get("batch", default_batch)
        if not batch:
            raise ValueError(f"assets[{idx}] 未提供登记批次（资产级或全局 batch）")
        batch = str(batch)
        volumes = asset.get("volumes", [])
        if not isinstance(volumes, list):
            raise ValueError(f"assets[{idx}].volumes 必须是数组")

        vol_results = [
            evaluate_volume(asset_id, batch, str(v), reports, receipts, check_time)
            for v in volumes
        ]
        counts = {s: 0 for s in ALL_STATUSES}
        for vr in vol_results:
            counts[vr["status"]] += 1
        devices.append(
            {
                "asset_id": asset_id,
                "batch": batch,
                "status": STATUS_PASS
                if all(vr["status"] == STATUS_PASS for vr in vol_results)
                else "FAIL",
                "volume_summary": {k: v for k, v in counts.items() if v},
                "volumes": vol_results,
            }
        )

    total_counts = {s: 0 for s in ALL_STATUSES}
    for d in devices:
        for vr in d["volumes"]:
            total_counts[vr["status"]] += 1
    passed = sum(1 for d in devices if d["status"] == STATUS_PASS)
    return {
        "check_time": check_time.isoformat(),
        "devices": devices,
        "summary": {
            "devices_total": len(devices),
            "devices_passed": passed,
            "devices_failed": len(devices) - passed,
            "volume_status_counts": {k: v for k, v in total_counts.items() if v},
        },
    }
