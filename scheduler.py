"""核心排班逻辑：保留名额 + 商户组同班上限 + 顺延补位。

规则（与需求一一对应）：
1. 先从最高风险组按入队时间填保留名额；每个商户组有可配置的同班上限，
   保留名额阶段占用也计入组上限。
2. 保留名额未用完的部分并入普通名额。
3. 普通名额优先补上一班因组上限被推迟的记录（顺延队列），
   再按风险等级（高到低）和入队时间（早到晚）排。
4. 已被前一班领取的交易不再进入后续批次（claimed 过滤）。
5. 因组上限落选的记录进入 deferred_cap 顺延队列；因名额耗尽早落选的
   记录留在 pending 待复核队列。
"""

REASON_RESERVED = "reserved_high_risk"      # 保留名额：最高风险组按入队时间
REASON_CARRYOVER = "carryover_deferred"     # 上一班因组上限被推迟，优先补位
REASON_GENERAL = "general_risk_time"        # 普通名额：按风险+入队时间

QUEUE_DEFERRED = "deferred_cap"   # 因组上限被推迟，下一班优先补位
QUEUE_PENDING = "pending"         # 名额耗尽早落选，留在待复核队列

REQUIRED_FIELDS = ("txn_id", "risk_level", "merchant_group", "enqueued_at")


def _check_record(rec):
    for field in REQUIRED_FIELDS:
        if field not in rec:
            raise ValueError("record missing field: %s" % field)
    if not isinstance(rec["risk_level"], int):
        raise ValueError("risk_level must be int, txn=%s" % rec.get("txn_id"))


def _sort_key_backlog(rec):
    # 风险高优先，同风险按入队时间，再按 txn_id 保证确定性
    return (-rec["risk_level"], rec["enqueued_at"], rec["txn_id"])


def _sort_key_time(rec):
    return (rec["enqueued_at"], rec["txn_id"])


def _with_queue(rec, queue):
    out = {k: rec[k] for k in REQUIRED_FIELDS}
    out["queue"] = queue
    return out


def plan_shift(records, reserved_slots, general_slots, group_caps=None,
               carryover=None, claimed=None):
    """计算一个班次的入选名单与落选去向。

    参数：
        records: 本班待复核交易（dict 列表，含 REQUIRED_FIELDS）。
        reserved_slots: 保留名额数（最高风险组专用）。
        general_slots: 普通名额数。
        group_caps: {商户组: 同班上限}，未列出的组不限。
        carryover: 上一班因组上限被推迟的记录。
        claimed: 已被之前班次领取的 txn_id 集合。

    返回 dict：
        selected: 入选记录（含 reason）。
        deferred_cap: 因组上限落选、进入顺延队列的记录。
        pending: 因名额耗尽落选、留在待复核队列的记录。
        excluded_claimed / excluded_duplicate: 被过滤的 txn_id。
        slots: 名额使用汇总（含未用完并入普通名额的保留名额数）。
    """
    if reserved_slots < 0 or general_slots < 0:
        raise ValueError("slots must be >= 0")
    group_caps = dict(group_caps or {})
    for group, cap in group_caps.items():
        if cap < 0:
            raise ValueError("group cap must be >= 0: %s" % group)
    carryover = list(carryover or [])
    claimed = set(claimed or [])

    for rec in list(records) + carryover:
        _check_record(rec)

    excluded_claimed = []
    excluded_duplicate = []
    pool = []
    seen = set()
    for rec in records:
        txn = rec["txn_id"]
        if txn in claimed:
            excluded_claimed.append(txn)  # 已被前一班领取，不再进入本批
        elif txn in seen:
            excluded_duplicate.append(txn)
        else:
            seen.add(txn)
            pool.append(rec)
    # 顺延队列里若混入已领取记录，同样过滤
    carryover_pool = [r for r in carryover if r["txn_id"] not in claimed]

    cap_remaining = dict(group_caps)
    selected = []
    deferred = []
    pending = []

    def cap_ok(rec):
        cap = cap_remaining.get(rec["merchant_group"])
        return cap is None or cap > 0

    def take(rec, reason):
        entry = {k: rec[k] for k in REQUIRED_FIELDS}
        entry["reason"] = reason
        selected.append(entry)
        group = rec["merchant_group"]
        if group in cap_remaining:
            cap_remaining[group] -= 1

    # 阶段一：保留名额 —— 当前最高风险组，按入队时间填
    reserved_left = reserved_slots
    if pool and reserved_left > 0:
        top_risk = max(r["risk_level"] for r in pool)
        top_group = sorted((r for r in pool if r["risk_level"] == top_risk),
                           key=_sort_key_time)
        for rec in top_group:
            if reserved_left == 0:
                break
            if not cap_ok(rec):
                continue  # 组上限已满，留给普通阶段判定去向
            take(rec, REASON_RESERVED)
            reserved_left -= 1
    chosen = {s["txn_id"] for s in selected}
    pool = [r for r in pool if r["txn_id"] not in chosen]

    # 保留名额未用完的部分并入普通名额
    general_left = general_slots + reserved_left

    # 阶段二：上一班因组上限被推迟的记录优先补位（按入队时间）
    for rec in sorted(carryover_pool, key=_sort_key_time):
        if general_left == 0 or not cap_ok(rec):
            deferred.append(_with_queue(rec, QUEUE_DEFERRED))
            continue
        take(rec, REASON_CARRYOVER)
        general_left -= 1

    # 阶段三：普通名额按风险（高到低）+ 入队时间（早到晚）
    # 组上限已满的组即使还有名额也进不了，记为顺延；上限未满但名额耗尽记为 pending
    for rec in sorted(pool, key=_sort_key_backlog):
        if not cap_ok(rec):
            deferred.append(_with_queue(rec, QUEUE_DEFERRED))
            continue
        if general_left == 0:
            pending.append(_with_queue(rec, QUEUE_PENDING))
            continue
        take(rec, REASON_GENERAL)
        general_left -= 1

    return {
        "selected": selected,
        "deferred_cap": deferred,
        "pending": pending,
        "excluded_claimed": excluded_claimed,
        "excluded_duplicate": excluded_duplicate,
        "slots": {
            "reserved": reserved_slots,
            "general": general_slots,
            "reserved_unused_merged": reserved_left,
            "general_unfilled": general_left,
        },
    }
