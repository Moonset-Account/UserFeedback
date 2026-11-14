"""密钥退役清点核心逻辑。

读取资产使用关系、各产物的签发时间与对应类型的验证保留期、
已完成的重新签发记录以及盘点时间，按密钥计算：

  * 还能产生新引用的入口（若依赖资产可继续派生新版本，附一条来源链）
  * 仍需旧公钥验证的存量产物
  * 可安全退役的最早时点

规则：
  * 重新签发只解除对应产物版本的依赖；
  * 重新签发记录缺失完成时间 -> 进入人工清点；
  * 引用去向不明 -> 进入人工清点。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["InventoryError", "build_report", "parse_time", "format_time"]


class InventoryError(ValueError):
    """输入清单不合法。"""


def parse_time(value, field):
    """解析 ISO-8601 时间戳；无显式时区时按 UTC 处理。"""
    if not isinstance(value, str):
        raise InventoryError(f"{field}: 期望 ISO-8601 字符串，实际为 {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InventoryError(f"{field}: 非法时间戳 {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require(mapping, field, ctx):
    if not isinstance(mapping, dict):
        raise InventoryError(f"{ctx}: 期望 JSON 对象")
    value = mapping.get(field)
    if value is None:
        raise InventoryError(f"{ctx}: 缺少必填字段 {field}")
    return value


def _normalize(data):
    """校验并规范化输入清单。"""
    if not isinstance(data, dict):
        raise InventoryError("清单顶层必须是 JSON 对象")
    inventory_time = parse_time(_require(data, "inventory_time", "inventory"), "inventory_time")

    policies = {}
    for idx, raw in enumerate(data.get("retention_policies") or []):
        ctx = f"retention_policies[{idx}]"
        atype = _require(raw, "artifact_type", ctx)
        days = _require(raw, "verify_retention_days", ctx)
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            raise InventoryError(f"{ctx}: verify_retention_days 必须是非负整数")
        if atype in policies:
            raise InventoryError(f"{ctx}: 类型 {atype} 的验证保留期重复定义")
        policies[atype] = timedelta(days=days)

    assets = {}
    for idx, raw in enumerate(data.get("assets") or []):
        ctx = f"assets[{idx}]"
        aid = _require(raw, "asset_id", ctx)
        if aid in assets:
            raise InventoryError(f"{ctx}: 资产 {aid} 重复定义")
        assets[aid] = {
            "asset_id": aid,
            "kind": raw.get("kind", "derived"),
            "active": bool(raw.get("active", False)),
            "derives_from": raw.get("derives_from"),
        }
    for asset in assets.values():
        parent = asset["derives_from"]
        if parent is not None and parent not in assets:
            raise InventoryError(
                f"资产 {asset['asset_id']} 的 derives_from 指向未知资产 {parent}"
            )

    keys = []
    seen_keys = set()
    for idx, raw in enumerate(data.get("keys") or []):
        ctx = f"keys[{idx}]"
        kid = _require(raw, "key_id", ctx)
        if kid in seen_keys:
            raise InventoryError(f"{ctx}: 密钥 {kid} 重复定义")
        seen_keys.add(kid)
        stopped = raw.get("signing_stopped_at")
        keys.append({
            "key_id": kid,
            "signing_stopped_at": (
                parse_time(stopped, f"{ctx}.signing_stopped_at")
                if stopped is not None
                else None
            ),
        })

    artifacts = {}
    for idx, raw in enumerate(data.get("artifacts") or []):
        ctx = f"artifacts[{idx}]"
        arid = _require(raw, "artifact_id", ctx)
        if arid in artifacts:
            raise InventoryError(f"{ctx}: 产物 {arid} 重复定义")
        kid = _require(raw, "key_id", ctx)
        if kid not in seen_keys:
            raise InventoryError(f"{ctx}: 未知密钥 {kid}")
        asset_id = _require(raw, "asset_id", ctx)
        if asset_id not in assets:
            raise InventoryError(f"{ctx}: 未知资产 {asset_id}")
        atype = _require(raw, "artifact_type", ctx)
        if atype not in policies:
            raise InventoryError(f"{ctx}: 产物类型 {atype} 缺少验证保留期策略")
        issued = parse_time(_require(raw, "issued_at", ctx), f"{ctx}.issued_at")
        artifacts[arid] = {
            "artifact_id": arid,
            "key_id": kid,
            "asset_id": asset_id,
            "artifact_type": atype,
            "issued_at": issued,
        }

    reissues = {}
    for idx, raw in enumerate(data.get("reissues") or []):
        ctx = f"reissues[{idx}]"
        arid = _require(raw, "artifact_id", ctx)
        if arid not in artifacts:
            raise InventoryError(f"{ctx}: 未知产物 {arid}")
        completed = raw.get("completed_at")
        record = {
            "reissue_id": raw.get("reissue_id")
            or f"{arid}#{len(reissues.get(arid, ())) + 1}",
            "artifact_id": arid,
            "completed_at": (
                parse_time(completed, f"{ctx}.completed_at")
                if completed is not None
                else None
            ),
        }
        reissues.setdefault(arid, []).append(record)

    edges = []
    for idx, raw in enumerate(data.get("usage_edges") or []):
        ctx = f"usage_edges[{idx}]"
        src = _require(raw, "from_asset", ctx)
        if src not in assets:
            raise InventoryError(f"{ctx}: 未知来源资产 {src}")
        dst = raw.get("to_asset")
        if dst is not None and dst not in assets:
            raise InventoryError(f"{ctx}: 未知去向资产 {dst}")
        known = raw.get("destination_known")
        if known is None:
            known = dst is not None
        edges.append({
            "edge_id": raw.get("edge_id") or f"edge-{idx + 1}",
            "from_asset": src,
            "to_asset": dst,
            "destination_known": bool(known),
        })

    return {
        "inventory_time": inventory_time,
        "policies": policies,
        "assets": assets,
        "keys": keys,
        "artifacts": artifacts,
        "reissues": reissues,
        "edges": edges,
    }


def _chain_to_entry(asset_id, assets):
    """沿 derives_from 向上寻找入口资产。

    返回 (entry_asset_id 或 None, 来源链)。来源链从依赖资产出发、
    到入口资产为止；派生关系成环时沿链终止，不会死循环。
    """
    chain = [asset_id]
    current = asset_id
    while True:
        node = assets[current]
        if node["kind"] == "entry_point":
            return current, chain
        parent = node["derives_from"]
        if parent is None or parent in chain:
            return None, chain
        chain.append(parent)
        current = parent


def _key_report(key, norm):
    kid = key["key_id"]
    inventory_time = norm["inventory_time"]
    retained = []
    manual = []
    dep_assets = set()

    for arid in sorted(norm["artifacts"]):
        art = norm["artifacts"][arid]
        if art["key_id"] != kid:
            continue
        records = norm["reissues"].get(arid, ())
        cleared = False
        for rec in records:
            if rec["completed_at"] is None:
                manual.append({
                    "reason": "reissue_completion_missing",
                    "artifact_id": arid,
                    "reissue_id": rec["reissue_id"],
                    "detail": "重新签发记录缺失完成时间，无法确认依赖是否解除",
                })
            else:
                # 重新签发只解除该产物版本的依赖
                cleared = True
        if cleared:
            continue
        expiry = art["issued_at"] + norm["policies"][art["artifact_type"]]
        if expiry > inventory_time:
            dep_assets.add(art["asset_id"])
            retained.append({
                "artifact_id": arid,
                "asset_id": art["asset_id"],
                "artifact_type": art["artifact_type"],
                "issued_at": format_time(art["issued_at"]),
                "retention_expires_at": format_time(expiry),
                "_expiry": expiry,
            })

    # 还能产生新引用的入口：依赖资产可沿派生链到达仍在活动的入口
    open_entries = {}
    for asset_id in sorted(dep_assets):
        entry, chain = _chain_to_entry(asset_id, norm["assets"])
        if (
            entry is not None
            and norm["assets"][entry]["active"]
            and entry not in open_entries
        ):
            open_entries[entry] = {
                "asset_id": entry,
                "via_asset": asset_id,
                "source_chain": chain,
            }

    # 引用去向不明 -> 人工清点
    for edge in norm["edges"]:
        if not edge["destination_known"] and edge["from_asset"] in dep_assets:
            manual.append({
                "reason": "reference_destination_unknown",
                "edge_id": edge["edge_id"],
                "from_asset": edge["from_asset"],
                "detail": "引用去向不明，无法确认旧公钥是否仍被验证",
            })

    manual.sort(key=lambda item: (item["reason"], item.get("artifact_id")
                                  or item.get("edge_id") or ""))
    retained.sort(key=lambda item: item["artifact_id"])
    open_list = [open_entries[name] for name in sorted(open_entries)]

    blocking = []
    if manual:
        status = "manual_review"
        earliest = None
        blocking.append("manual_review_required")
    elif open_list:
        status = "retain_until"
        earliest = None
        blocking.append("open_entry_points")
    elif retained:
        status = "retain_until"
        earliest = max(item["_expiry"] for item in retained)
    else:
        status = "retirable_now"
        earliest = inventory_time

    for item in retained:
        del item["_expiry"]

    return {
        "key_id": kid,
        "signing_stopped_at": (
            format_time(key["signing_stopped_at"])
            if key["signing_stopped_at"] is not None
            else None
        ),
        "status": status,
        "open_entry_points": open_list,
        "retained_stock": retained,
        "manual_review": manual,
        "earliest_safe_retirement": format_time(earliest) if earliest else None,
        "blocking_reasons": blocking,
    }


def build_report(data):
    """根据清单数据生成按密钥划分的退役清点报告。"""
    norm = _normalize(data)
    return {
        "tool": "key-retirement-inventory",
        "inventory_time": format_time(norm["inventory_time"]),
        "keys": [_key_report(key, norm) for key in norm["keys"]],
    }
