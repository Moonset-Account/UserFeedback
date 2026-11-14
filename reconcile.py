"""采集端与归档端审计清单的离线对账核心逻辑。

语义约定：
- 记录字段：tenant（租户）、shard（分片）、seq（序号）、digest（内容摘要）、status（接收状态）。
- 采集端记录 status == "received" 才视为已写入本地审计记录，纳入对账期望集合；
  其他状态（如 "dropped"）的采集端记录不参与对账。
- 归档端记录 status == "persisted" 且 digest 与采集端一致，才计为“已确认”，
  只有已确认记录计入水位线。
- 水位线从该 (tenant, shard) 的最小期望序号（已确认的起始序号）起，沿连续已确认
  序号向前推进，跨不过任一缺口（缺失、未持久化或摘要不一致都会阻断）；若起始序号
  本身未确认，水位线记为 start_seq - 1；该组没有期望记录时水位线为 None。
- 连续缺口 = 期望集合中未确认、且不属于摘要不一致的序号，合并为连续区间输出。
- 摘要不一致 = 双方都有该序号但 digest 不同的记录（不论归档端状态）。
- 归档端多出 = 归档端存在但采集端期望集合中不存在的记录。
- 补传计划 = 缺口（reason="missing"）+ 摘要不一致（reason="digest_mismatch"），
  保持原序号与原摘要，按 (tenant, shard, seq) 升序排列；相同输入（与记录书写顺序
  无关）生成完全相同的计划。
"""

RECEIVED_STATUS = "received"    # 采集端已写入本地审计记录
PERSISTED_STATUS = "persisted"  # 归档端已持久化

REQUIRED_FIELDS = ("tenant", "shard", "seq", "digest", "status")


class ManifestError(ValueError):
    """清单格式或内容非法。"""


def validate_records(records, source):
    """校验并规范化清单记录，返回新列表。重复 (tenant, shard, seq) 视为非法。"""
    if not isinstance(records, list):
        raise ManifestError(f"{source}清单必须是记录数组")
    seen = set()
    normalized = []
    for index, rec in enumerate(records):
        where = f"{source}清单第 {index} 条记录"
        if not isinstance(rec, dict):
            raise ManifestError(f"{where}不是 JSON 对象")
        for field in REQUIRED_FIELDS:
            if field not in rec:
                raise ManifestError(f"{where}缺少字段 {field!r}")
        tenant = rec["tenant"]
        shard = rec["shard"]
        seq = rec["seq"]
        digest = rec["digest"]
        status = rec["status"]
        if not isinstance(tenant, str) or not tenant:
            raise ManifestError(f"{where}的 tenant 必须是非空字符串")
        if not isinstance(shard, str) or not shard:
            raise ManifestError(f"{where}的 shard 必须是非空字符串")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ManifestError(f"{where}的 seq 必须是非负整数")
        if not isinstance(digest, str) or not digest:
            raise ManifestError(f"{where}的 digest 必须是非空字符串")
        if not isinstance(status, str) or not status:
            raise ManifestError(f"{where}的 status 必须是非空字符串")
        key = (tenant, shard, seq)
        if key in seen:
            raise ManifestError(
                f"{where}与已有记录重复: tenant={tenant} shard={shard} seq={seq}"
            )
        seen.add(key)
        normalized.append(
            {"tenant": tenant, "shard": shard, "seq": seq, "digest": digest, "status": status}
        )
    return normalized


def _to_ranges(sorted_seqs):
    """把升序序号列表合并为连续区间 [{start, end}, ...]。"""
    ranges = []
    for seq in sorted_seqs:
        if ranges and seq == ranges[-1]["end"] + 1:
            ranges[-1]["end"] = seq
        else:
            ranges.append({"start": seq, "end": seq})
    return ranges


def _reconcile_group(key, expected, archived):
    """对单个 (tenant, shard) 组对账。

    expected: {seq: 采集端记录}（仅含 status == received 的记录）
    archived: {seq: 归档端记录}
    """
    tenant, shard = key

    mismatches = []
    extras = []
    for seq in sorted(archived):
        arec = archived[seq]
        crec = expected.get(seq)
        if crec is None:
            extras.append({"seq": seq, "digest": arec["digest"], "status": arec["status"]})
        elif arec["digest"] != crec["digest"]:
            mismatches.append(
                {
                    "seq": seq,
                    "collection_digest": crec["digest"],
                    "archive_digest": arec["digest"],
                    "archive_status": arec["status"],
                }
            )

    # 已确认 = 归档端已持久化且摘要一致
    confirmed = set()
    for seq, crec in expected.items():
        arec = archived.get(seq)
        if arec is not None and arec["status"] == PERSISTED_STATUS and arec["digest"] == crec["digest"]:
            confirmed.add(seq)

    if expected:
        start = min(expected)
        end = max(expected)
        mismatch_seqs = {m["seq"] for m in mismatches}
        gap_seqs = [
            seq
            for seq in range(start, end + 1)
            if seq in expected and seq not in confirmed and seq not in mismatch_seqs
        ]
        gaps = _to_ranges(gap_seqs)
        # 水位线：从起始序号起沿连续已确认序号推进，跨不过任一缺口
        watermark = start - 1
        seq = start
        while seq in confirmed:
            watermark = seq
            seq += 1
    else:
        start = None
        watermark = None
        gaps = []

    plan = []
    for item in mismatches:
        crec = expected[item["seq"]]
        plan.append(
            {
                "tenant": tenant,
                "shard": shard,
                "seq": item["seq"],
                "digest": crec["digest"],
                "reason": "digest_mismatch",
            }
        )
    for gap in gaps:
        for seq in range(gap["start"], gap["end"] + 1):
            plan.append(
                {
                    "tenant": tenant,
                    "shard": shard,
                    "seq": seq,
                    "digest": expected[seq]["digest"],
                    "reason": "missing",
                }
            )
    plan.sort(key=lambda item: item["seq"])

    return {
        "tenant": tenant,
        "shard": shard,
        "start_seq": start,
        "watermark": watermark,
        "gaps": gaps,
        "digest_mismatches": mismatches,
        "archive_extras": extras,
        "retransmit_plan": plan,
    }


def reconcile(collection_records, archive_records):
    """对账入口：返回确定性报告 dict（组按 tenant/shard 排序）。"""
    collection = validate_records(collection_records, "采集端")
    archive = validate_records(archive_records, "归档端")

    expected_groups = {}
    for rec in collection:
        if rec["status"] != RECEIVED_STATUS:
            continue  # 采集端未确认接收的记录不纳入对账期望
        expected_groups.setdefault((rec["tenant"], rec["shard"]), {})[rec["seq"]] = rec

    archive_groups = {}
    for rec in archive:
        archive_groups.setdefault((rec["tenant"], rec["shard"]), {})[rec["seq"]] = rec

    groups = []
    for key in sorted(set(expected_groups) | set(archive_groups)):
        expected = expected_groups.get(key, {})
        archived = archive_groups.get(key, {})
        groups.append(_reconcile_group(key, expected, archived))
    return {"groups": groups}
