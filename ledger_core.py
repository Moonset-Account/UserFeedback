"""电子证物交接事件台账 —— 核心重建逻辑(仅使用 Python 3 标准库)。

支持五类事件:
  intake          入库        建立初始保管人
  transfer_init   移交发起    必须由当前保管人发起,并指定接收人
  receive_confirm 接收确认    必须对应同一次待确认移交,且由指定接收人确认;
                              确认成功才改变保管人
  revoke          撤销        由移交发起人撤销待确认移交
  review          复核        仅记录,不改变保管状态

规则要点:
  * 事件按证物编号分组,按流水序号(seq)排序;输入文件可分批、乱序。
  * 内容完全相同的重复事件只应用一次(来源位置全部保留)。
  * 同一 seq 出现两种不同内容:都保留、标明冲突,并视为核对中断点。
  * 序号断档 / 冲突 / 结构残缺均构成"中断":只确认到最后一条可连续核对的
    事件,其后的原始记录位置列入 needs_review,留待人工补查。
  * 业务规则校验失败(如非保管人发起移交)的事件记为 rejected,不应用,
    但该记录本身已核对,不影响连续性。
"""

import json

EVENT_TYPES = ("intake", "transfer_init", "receive_confirm", "revoke", "review")

# 各事件类型除 evidence_id/seq/type 外的必填字段
_REQUIRED_FIELDS = {
    "intake": ("custodian",),
    "transfer_init": ("transfer_id", "from", "to"),
    "receive_confirm": ("transfer_id", "by"),
    "revoke": ("transfer_id", "by"),
    "review": ("reviewer",),
}


class Location:
    """一条原始记录在导出文件中的位置。"""

    __slots__ = ("path", "line")

    def __init__(self, path, line):
        self.path = path
        self.line = line

    def as_dict(self):
        return {"file": self.path, "line": self.line}

    def __repr__(self):
        return "Location(%r, %r)" % (self.path, self.line)


# ---------------------------------------------------------------- 读取

def _validate_envelope(obj):
    """校验事件信封(能据此分组排序的最小结构)。返回错误串或 None。"""
    if not isinstance(obj, dict):
        return "not_a_json_object"
    eid = obj.get("evidence_id")
    if not isinstance(eid, str) or not eid:
        return "missing_or_invalid_evidence_id"
    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return "missing_or_invalid_seq"
    etype = obj.get("type")
    if etype not in EVENT_TYPES:
        return "missing_or_unknown_type"
    return None


def _validate_event_body(ev):
    """校验事件正文必填字段。返回错误串或 None。"""
    for field_name in _REQUIRED_FIELDS[ev["type"]]:
        value = ev.get(field_name)
        if not isinstance(value, str) or not value:
            return "missing_or_invalid_field:%s" % field_name
    return None


def load_records(paths):
    """读取全部批次导出文件(JSONL,每行一个事件)。

    返回 (events, invalid_records):
      events          [(event_dict, Location), ...]
      invalid_records 无法入档的原始行(JSON 解析失败或信封不合法),
                      含文件与行号,需人工处理。
    """
    events = []
    invalid = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                text = raw.strip()
                if not text:
                    continue
                loc = Location(path, lineno)
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as exc:
                    invalid.append({
                        "location": loc.as_dict(),
                        "reason": "json_parse_error: %s" % exc,
                        "raw": text,
                    })
                    continue
                err = _validate_envelope(obj)
                if err:
                    invalid.append({
                        "location": loc.as_dict(),
                        "reason": err,
                        "raw": obj,
                    })
                    continue
                events.append((obj, loc))
    return events, invalid


# ---------------------------------------------------------------- 状态机

def _new_state():
    return {"custodian": None, "transfers": {}, "reviews": []}


def _apply(state, ev):
    """把一条已核对的事件应用到保管状态。返回 (ok, reason)。"""
    etype = ev["type"]

    if etype == "intake":
        if state["custodian"] is not None:
            return False, "duplicate_intake"
        state["custodian"] = ev["custodian"]
        return True, None

    if etype == "transfer_init":
        if state["custodian"] is None:
            return False, "no_current_custodian"
        if ev["from"] != state["custodian"]:
            return False, "initiator_not_current_custodian"
        tid = ev["transfer_id"]
        if tid in state["transfers"]:
            return False, "transfer_id_reused"
        if any(t["status"] == "pending" for t in state["transfers"].values()):
            return False, "another_transfer_pending"
        state["transfers"][tid] = {
            "transfer_id": tid,
            "from": ev["from"],
            "to": ev["to"],
            "status": "pending",
            "initiated_seq": ev["seq"],
        }
        return True, None

    if etype == "receive_confirm":
        tr = state["transfers"].get(ev["transfer_id"])
        if tr is None or tr["status"] != "pending":
            return False, "no_matching_pending_transfer"
        if ev["by"] != tr["to"]:
            return False, "confirmer_not_designated_receiver"
        tr["status"] = "confirmed"
        state["custodian"] = tr["to"]
        return True, None

    if etype == "revoke":
        tr = state["transfers"].get(ev["transfer_id"])
        if tr is None or tr["status"] != "pending":
            return False, "no_matching_pending_transfer"
        if ev["by"] != tr["from"]:
            return False, "revoker_not_initiator"
        tr["status"] = "revoked"
        return True, None

    # review:仅记录,不改变保管状态
    state["reviews"].append({
        "seq": ev["seq"],
        "reviewer": ev["reviewer"],
        "note": ev.get("note"),
    })
    return True, None


# ---------------------------------------------------------------- 重建

def _canonical(ev):
    return json.dumps(ev, sort_keys=True, ensure_ascii=False)


def process_evidence(evidence_id, records):
    """重建单件证物的台账。records: [(event_dict, Location), ...]"""
    # 1) 按 seq 分组;同 seq 按内容去重,内容不同则记冲突
    by_seq = {}
    for ev, loc in records:
        by_seq.setdefault(ev["seq"], []).append((ev, loc))

    seq_entries = {}   # seq -> [ {"event":..., "locations":[Location,...]}, ... ]
    conflicts = []
    for seq, items in by_seq.items():
        groups = {}
        for ev, loc in items:
            key = _canonical(ev)
            if key not in groups:
                groups[key] = {"event": ev, "locations": []}
            groups[key]["locations"].append(loc)
        distinct = list(groups.values())
        distinct.sort(key=lambda g: (g["locations"][0].path, g["locations"][0].line))
        seq_entries[seq] = distinct
        if len(distinct) > 1:
            conflicts.append({
                "seq": seq,
                "variants": [
                    {"event": g["event"],
                     "locations": [l.as_dict() for l in g["locations"]]}
                    for g in distinct
                ],
            })
    conflicts.sort(key=lambda c: c["seq"])

    # 2) 从 seq=1 起连续核对;断档/冲突/残缺即中断,后续留待人工复核
    state = _new_state()
    applied, rejected, needs_review = [], [], []
    confirmed_through = 0
    interruption = None
    expected = 1

    def hold(seq, reason):
        for g in seq_entries[seq]:
            needs_review.append({
                "seq": seq,
                "reason": reason,
                "event": g["event"],
                "locations": [l.as_dict() for l in g["locations"]],
            })

    for seq in sorted(seq_entries):
        if interruption is not None:
            hold(seq, "after_interruption")
            continue
        if seq != expected:
            interruption = {"reason": "sequence_gap",
                            "expected_seq": expected, "found_seq": seq}
            hold(seq, "after_interruption")
            continue
        distinct = seq_entries[seq]
        if len(distinct) > 1:
            interruption = {"reason": "conflicting_contents", "seq": seq}
            hold(seq, "conflicting_contents")
            continue
        g = distinct[0]
        ev = g["event"]
        locs = [l.as_dict() for l in g["locations"]]
        body_err = _validate_event_body(ev)
        if body_err:
            interruption = {"reason": "malformed_event: %s" % body_err, "seq": seq}
            needs_review.append({
                "seq": seq,
                "reason": "malformed_event: %s" % body_err,
                "event": ev,
                "locations": locs,
            })
            continue
        ok, reason = _apply(state, ev)
        if ok:
            applied.append({"seq": seq, "type": ev["type"], "locations": locs})
        else:
            rejected.append({"seq": seq, "type": ev["type"], "reason": reason,
                             "event": ev, "locations": locs})
        confirmed_through = seq
        expected = seq + 1

    pending = [dict(t) for t in state["transfers"].values()
               if t["status"] == "pending"]
    pending.sort(key=lambda t: t["initiated_seq"])
    if pending:
        status = "in_transfer"
    elif state["custodian"] is not None:
        status = "in_custody"
    else:
        status = "unknown"

    return {
        "evidence_id": evidence_id,
        "status": status,
        "current_custodian": state["custodian"],
        "confirmed_through_seq": confirmed_through,
        "interrupted": interruption is not None,
        "interruption": interruption,
        "pending_transfers": pending,
        "applied_events": applied,
        "rejected_events": rejected,
        "conflicts": conflicts,
        "needs_review": needs_review,
        "reviews": state["reviews"],
    }


def build_report(events, invalid_records):
    """汇总全部证物的台账报告。"""
    by_evidence = {}
    for ev, loc in events:
        by_evidence.setdefault(ev["evidence_id"], []).append((ev, loc))
    items = [process_evidence(eid, recs)
             for eid, recs in sorted(by_evidence.items())]
    return {
        "evidence_count": len(items),
        "evidence": items,
        "invalid_records": invalid_records,
    }
