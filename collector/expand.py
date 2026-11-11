"""有界时间区间内的 iCalendar 重复事件展开。

支持的协议子集（RFC 5545），超出子集的规则一律通过 Issue 上报、不静默吞掉：

支持：
- VEVENT 的 DTSTART（DATE / 浮动 DATE-TIME / 带 TZID 的 DATE-TIME / UTC DATE-TIME）；
- DTEND 或 DURATION（含全日事件跨天、定时事件跨日时长）；
- RRULE：FREQ=DAILY/WEEKLY/MONTHLY/YEARLY，INTERVAL，COUNT 或 UNTIL（二者互斥），
  BYDAY（可带序号，如 2MO/-1FR），BYMONTHDAY，BYMONTH，BYSETPOS，WKST；
- RDATE：DATE 与 DATE-TIME 值；
- EXDATE：DATE 与 DATE-TIME 值；
- RECURRENCE-ID 例外：移动（新 DTSTART）与取消（STATUS=CANCELLED），
  同一 RECURRENCE-ID 以 SEQUENCE 最大者为准；
- VTIMEZONE 定义的显式时区（含夏令时偏移变化）。

不支持（遇到即产生 warning/error，跳过对应部件，继续处理其余内容）：
- FREQ=SECONDLY/MINUTELY/HOURLY；
- BYYEARDAY、BYWEEKNO、BYHOUR、BYMINUTE、BYSECOND；
- RFC 7529 的 RSCALE/SKIP；
- RDATE 的 PERIOD 值；RECURRENCE-ID 的 RANGE 参数；
- 单个 VEVENT 上多个 RRULE；COUNT 与 UNTIL 同时出现。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from dateutil.rrule import rrule, rruleset
from dateutil import rrule as rr_const

from .errors import Issue, IssueCode
from .timeutil import classify, instance_key, isoformat_local, normalize_timezone, normalize_utc, tzid_of

WEEKDAYS = {
    "MO": rr_const.MO, "TU": rr_const.TU, "WE": rr_const.WE,
    "TH": rr_const.TH, "FR": rr_const.FR, "SA": rr_const.SA, "SU": rr_const.SU,
}

# 本采集器支持的 RRULE 键（其余键出现即上报不支持）
SUPPORTED_RRULE_KEYS = {
    "freq", "interval", "count", "until", "wkst",
    "bysetpos", "bymonth", "bymonthday", "byday",
}
SUPPORTED_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}

DEFAULT_MAX_INSTANCES = 5000  # 查询范围的展开规模硬上限


# --------------------------------------------------------------------------- #
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class EventIndex:
    """一个 VEVENT 在原始 ICS 文本中的行号证据（行号从 1 开始）。"""
    begin_line: int
    end_line: int
    property_lines: dict[str, int] = field(default_factory=dict)


@dataclass
class ParsedEvent:
    uid: str
    sequence: int
    status: str | None
    dtstart: dt.datetime | dt.date
    dtend: dt.datetime | dt.date
    duration: dt.timedelta
    kind: str                       # all_day / floating / tz_aware
    tzid: str | None
    summary: str
    rrule_prop: object | None       # icalendar vRecur
    rdates: list
    exdates: list
    recurrence_id: dt.datetime | dt.date | None
    rule_invalid: bool                 # 出现子集外/非法重复规则，规则整体失效
    index: EventIndex
    raw: object                     # 原始 icalendar Component，证据用


@dataclass
class ExpandedInstance:
    uid: str
    instance_id: str                # 全局实例身份：UID + RECURRENCE-ID 键
    recurrence_id: str              # 该实例对应的原始重复实例键（墙上时间）
    start: str
    end: str
    start_utc: str | None
    end_utc: str | None
    all_day: bool
    floating: bool
    tzid: str | None
    summary: str
    status: str
    origin: str                     # master / rdate / override_move / override_cancel
    origin_explanation: str
    duration_days_or_seconds: object
    source_lines: dict
    evidence: dict

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "instance_id": self.instance_id,
            "recurrence_id": self.recurrence_id,
            "start": self.start,
            "end": self.end,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "all_day": self.all_day,
            "floating": self.floating,
            "tzid": self.tzid,
            "summary": self.summary,
            "status": self.status,
            "origin": self.origin,
            "origin_explanation": self.origin_explanation,
            "duration": self.duration_days_or_seconds,
            "source_lines": self.source_lines,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------- #
# 行号索引（把折叠后的属性映射回原始文本行）
# ---------------------------------------------------------------------------

def build_line_index(ics_text: str) -> dict[str, EventIndex]:
    """扫描原始 ICS 文本，返回 {UID: EventIndex}，用于来源证据。"""
    indexes: dict[str, EventIndex] = {}
    lines = ics_text.splitlines()
    current_begin: int | None = None
    current_props: dict[str, int] = {}
    current_uid: str | None = None

    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            current_begin = lineno
            current_props = {}
            current_uid = None
        elif line == "END:VEVENT" and current_begin is not None:
            if current_uid:
                indexes[current_uid] = EventIndex(
                    begin_line=current_begin,
                    end_line=lineno,
                    property_lines=dict(current_props),
                )
            current_begin = None
            current_uid = None
            current_props = {}
        elif current_begin is not None and ":" in line:
            name = line.split(":", 1)[0].split(";", 1)[0].upper()
            if name not in current_props:  # 同名属性记首行
                current_props[name] = lineno
            if name == "UID":
                current_uid = line.split(":", 1)[1].strip()
    return indexes


# --------------------------------------------------------------------------- #
# 组件解析
# ---------------------------------------------------------------------------

def _event_duration(component, dtstart, kind: str) -> tuple[dt.datetime | dt.date, dt.timedelta]:
    """按 RFC 5545 优先级计算 DTEND：显式 DTEND > DURATION > 类型默认值。"""
    if "DTEND" in component:
        dtend = component.get("DTEND").dt
        return dtend, dtend - dtstart
    if "DURATION" in component:
        duration = component.get("DURATION").dt
        return dtstart + duration, duration
    if kind == "all_day":
        # VALUE=DATE 的事件默认持续 1 天（DTEND 为次日，半开区间）
        return dtstart + dt.timedelta(days=1), dt.timedelta(days=1)
    return dtstart, dt.timedelta(0)


def _all_props(component, name: str) -> list:
    """取出同名的全部属性。

    icalendar 5 的 Component 是 CaselessDict：单值时 get() 返回属性对象，
    同名多次 add 时返回属性对象列表。
    """
    if name not in component:
        return []
    value = component.get(name)
    return value if isinstance(value, list) else [value]


def _flatten_values(prop) -> list:
    """拍平 RDATE/EXDATE 的取值。

    icalendar 5 的 vDDDLists 把值放在 .dts（一组 vDDDTypes，取 .dt 得原生对象）；
    多值属性经 _all_props 拿到的是一组 vDDDLists。
    已经是原生 date/datetime/Period 的值直接保留。
    """
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    result: list = []
    for item in props:
        dts = getattr(item, "dts", None)
        if dts is not None:
            result.extend(getattr(v, "dt", v) for v in dts)
            continue
        values = getattr(item, "dt", None)
        if values is not None:
            if isinstance(values, list):
                result.extend(values)
            else:
                result.append(values)
        else:
            result.append(item)
    return result


def parse_events(calendar, indexes: dict[str, EventIndex],
                 issues: list[Issue]) -> list[ParsedEvent]:
    events: list[ParsedEvent] = []
    for component in list(calendar.walk()):
        cname = component.name
        if cname == "VCALENDAR":
            continue
        if cname in ("VTIMEZONE", "STANDARD", "DAYLIGHT"):
            # 时区定义组件：icalendar 已据此解析 TZID，不属于采集对象也不算异常
            continue
        if cname != "VEVENT":
            issues.append(Issue.make(
                IssueCode.UNSUPPORTED_COMPONENT,
                f"组件 {cname} 不在采集范围内（本采集器只处理 VEVENT），已跳过",
            ))
            continue
        if "UID" not in component or "DTSTART" not in component:
            issues.append(Issue.make(
                IssueCode.INVALID_CALENDAR,
                "VEVENT 缺少 UID 或 DTSTART，无法确定实例身份，已跳过",
            ))
            continue

        uid = str(component.get("UID"))
        idx = indexes.get(uid, EventIndex(-1, -1, {}))
        try:
            dtstart_prop = component.get("DTSTART")
            dtstart = dtstart_prop.dt
        except Exception as exc:
            issues.append(Issue.make(
                IssueCode.BAD_DATETIME,
                f"DTSTART 无法解析：{type(exc).__name__}: {exc}",
                uid=uid, line=idx.property_lines.get("DTSTART"),
            ))
            continue

        kind = classify(dtstart)
        if kind == "tz_aware":
            # 换成 zoneinfo，保证后续 dateutil 生成的每个日期 DST 偏移都正确
            dtstart = normalize_timezone(dtstart)
        dtend, duration = _event_duration(component, dtstart, kind)
        if kind == "tz_aware" and isinstance(dtend, dt.datetime) and dtend.tzinfo is not None:
            dtend = normalize_timezone(dtend)
        status = str(component.get("STATUS")) if "STATUS" in component else None
        seq = int(component.get("SEQUENCE", 0) or 0)

        rrules = _all_props(component, "RRULE")
        rule_invalid = False
        if len(rrules) > 1:
            issues.append(Issue.make(
                IssueCode.RRULE_MULTIPLE,
                f"单个 VEVENT 含 {len(rrules)} 条 RRULE，本采集器只支持单规则，全部重复规则已跳过",
                uid=uid, line=idx.property_lines.get("RRULE"), property_name="RRULE",
            ))
            rrule_prop = None
            rule_invalid = True
        else:
            rrule_prop = rrules[0] if rrules else None

        rid = component.get("RECURRENCE-ID").dt if "RECURRENCE-ID" in component else None
        if isinstance(rid, dt.datetime) and rid.tzinfo is not None:
            rid = normalize_timezone(rid)
        events.append(ParsedEvent(
            uid=uid,
            sequence=seq,
            status=status,
            dtstart=dtstart,
            dtend=dtend,
            duration=duration,
            kind=kind,
            tzid=tzid_of(dtstart_prop),
            summary=str(component.get("SUMMARY", "")) or uid,
            rrule_prop=rrule_prop,
            rdates=_flatten_values(_all_props(component, "RDATE")),
            exdates=_flatten_values(_all_props(component, "EXDATE")),
            recurrence_id=rid,
            rule_invalid=rule_invalid,
            index=idx,
            raw=component,
        ))
    return events


# --------------------------------------------------------------------------- #
# RRULE 子集校验与构造
# ---------------------------------------------------------------------------

def _byday_to_dateutil(byday):
    """把 BYDAY 转成 dateutil weekday：'FR' / 'FR' 枚举 / (-1, weekday) 均可。"""
    out = []
    for item in byday:
        if isinstance(item, tuple):  # (序号, weekday)，如 (-1, FR)
            ordinal, weekday = item
            out.append(weekday(ordinal))
        elif isinstance(item, str):
            token = str(item).upper()
            prefix, weekday_token = token[:-2], token[-2:]
            weekday = WEEKDAYS[weekday_token]
            out.append(weekday(int(prefix)) if prefix else weekday)
        else:
            # dateutil weekday 或 icalendar vWeekday 枚举
            name = str(item).split(".")[-1].upper()
            out.append(WEEKDAYS[name])
    return out


def _to_tuple(value) -> tuple:
    if isinstance(value, (list, tuple)):
        return tuple(int(v) for v in value)
    return (int(value),)


def _scalar(value):
    """icalendar vRecur 的单值部件也可能包成列表，取出标量。"""
    if isinstance(value, (list, tuple)):
        return value[0]
    return value


def build_rrule(event: ParsedEvent, range_lo, range_hi,
                issues: list[Issue]) -> rrule | None:
    """把 icalendar vRecur 转成 dateutil.rrule，遇到子集外特性即上报。"""
    recur = event.rrule_prop
    line = event.index.property_lines.get("RRULE")
    recur_dict = dict(recur)
    lower = {str(k).lower(): v for k, v in recur_dict.items()}

    unsupported = {k for k in lower if k not in SUPPORTED_RRULE_KEYS}
    if unsupported:
        for key in sorted(unsupported):
            mapped = {
                "byyearday": IssueCode.RRULE_BY_YEAR_DAY,
                "byweekno": IssueCode.RRULE_BY_WEEK_NO,
                "rscale": IssueCode.RRULE_RSCALE,
                "skip": IssueCode.RRULE_SKIP,
            }.get(key, IssueCode.RRULE_BAD_PART)
            issues.append(Issue.make(
                mapped, f"RRULE 部件 {key.upper()} 不在支持子集内，该重复规则已跳过",
                uid=event.uid, line=line, property_name="RRULE",
            ))
        return None

    # icalendar 5 的 freq 是枚举且包在列表里，按字符串值映射
    freq_name = str(_scalar(lower.get("freq"))).split(".")[-1].upper()
    if freq_name not in SUPPORTED_FREQS:
        code = {
            "SECONDLY": IssueCode.RRULE_SECONDLY,
            "MINUTELY": IssueCode.RRULE_MINUTELY,
            "HOURLY": IssueCode.RRULE_HOURLY,
        }.get(freq_name, IssueCode.RRULE_BAD_FREQ)
        issues.append(Issue.make(
            code, f"RRULE FREQ={freq_name or '(缺失)'} 不受支持，该重复规则已跳过",
            uid=event.uid, line=line, property_name="RRULE",
        ))
        return None

    if "count" in lower and "until" in lower:
        issues.append(Issue.make(
            IssueCode.RRULE_COUNT_UNTIL_BOTH,
            "RRULE 同时声明 COUNT 与 UNTIL（RFC 5545 禁止），该重复规则已跳过",
            uid=event.uid, line=line, property_name="RRULE",
        ))
        return None

    kwargs: dict = {"dtstart": event.dtstart,
                    "interval": int(_scalar(lower.get("interval", 1)) or 1)}
    if "count" in lower:
        kwargs["count"] = int(_scalar(lower["count"]))
    if "until" in lower:
        until = _scalar(lower["until"])
        if event.kind == "all_day" and isinstance(until, dt.datetime):
            until = until.date()
        kwargs["until"] = until
    if "wkst" in lower:
        wkst_name = str(_scalar(lower["wkst"])).split(".")[-1].upper()[:2]
        kwargs["wkst"] = WEEKDAYS.get(wkst_name, rr_const.MO)
    if "bysetpos" in lower:
        kwargs["bysetpos"] = _to_tuple(lower["bysetpos"])
    if "bymonth" in lower:
        kwargs["bymonth"] = _to_tuple(lower["bymonth"])
    if "bymonthday" in lower:
        kwargs["bymonthday"] = _to_tuple(lower["bymonthday"])
    if "byday" in lower:
        # dateutil 的构造参数名是 byweekday；BYDAY 的序号语义由 weekday(n) 承载
        kwargs["byweekday"] = _byday_to_dateutil(lower["byday"])

    freq_map = {
        "DAILY": rr_const.DAILY, "WEEKLY": rr_const.WEEKLY,
        "MONTHLY": rr_const.MONTHLY, "YEARLY": rr_const.YEARLY,
    }
    try:
        return rrule(freq_map[freq_name], **kwargs)
    except (TypeError, ValueError) as exc:
        issues.append(Issue.make(
            IssueCode.RRULE_BAD_PART,
            f"RRULE 无法构造：{type(exc).__name__}: {exc}",
            uid=event.uid, line=line, property_name="RRULE",
        ))
        return None


# --------------------------------------------------------------------------- #
# 展开主流程
# ---------------------------------------------------------------------------

def _duration_payload(duration: dt.timedelta, all_day: bool) -> dict:
    if all_day:
        return {"days": duration.days}
    return {"seconds": int(duration.total_seconds())}


def _make_instance(event: ParsedEvent, start, origin: str, explanation: str,
                   status: str | None, override_seq: int | None) -> ExpandedInstance:
    is_all_day = event.kind == "all_day"
    # dateutil 对 DATE 型 dtstart 仍可能返回 datetime，统一截回 date，保持全日语义
    if is_all_day and isinstance(start, dt.datetime):
        start = start.date()
    end = start + event.duration
    rid_key = instance_key(start)
    return ExpandedInstance(
        uid=event.uid,
        instance_id=f"{event.uid}::{rid_key}",
        recurrence_id=rid_key,
        start=isoformat_local(start) if not is_all_day else start.isoformat(),
        end=isoformat_local(end) if not is_all_day else end.isoformat(),
        start_utc=normalize_utc(start) if isinstance(start, dt.datetime) else None,
        end_utc=normalize_utc(end) if isinstance(end, dt.datetime) else None,
        all_day=is_all_day,
        floating=event.kind == "floating",
        tzid=event.tzid,
        summary=event.summary,
        status=status or ("CONFIRMED" if event.status is None else event.status),
        origin=origin,
        origin_explanation=explanation,
        duration_days_or_seconds=_duration_payload(event.duration, is_all_day),
        source_lines=event.index.property_lines,
        evidence={
            "event_begin_line": event.index.begin_line,
            "event_end_line": event.index.end_line,
            "override_sequence": override_seq,
            "tzid": event.tzid,
        },
    )


def expand_calendar(calendar, ics_text: str, range_lo, range_hi,
                    max_instances: int = DEFAULT_MAX_INSTANCES) -> dict:
    """在 [range_lo, range_hi) 内展开整个 VCALENDAR。"""
    issues: list[Issue] = []
    indexes = build_line_index(ics_text)
    events = parse_events(calendar, indexes, issues)

    masters = [e for e in events if e.recurrence_id is None]
    overrides = [e for e in events if e.recurrence_id is not None]

    # 例外按 RECURRENCE-ID 归组，SEQUENCE 最大者生效（修改例外后旧定义被覆盖）
    override_map: dict[str, ParsedEvent] = {}
    for ov in overrides:
        key = instance_key(ov.recurrence_id)
        old = override_map.get(key)
        if old is None or ov.sequence >= old.sequence:
            override_map[key] = ov

    instances: list[ExpandedInstance] = []
    excluded: list[dict] = []
    produced = 0

    def guard_limit() -> bool:
        nonlocal produced
        produced += 1
        if produced > max_instances:
            issues.append(Issue.make(
                IssueCode.RANGE_LIMIT,
                f"展开实例数超过查询范围上限 {max_instances}，已截断；请收窄查询范围",
            ))
            return True
        return False

    for master in masters:
        if master.rule_invalid:
            # 多 RRULE 等情形：规则已整体判废，不回退成 DTSTART 单实例
            continue
        rule = build_rrule(master, range_lo, range_hi, issues) if master.rrule_prop is not None else None
        if master.rrule_prop is not None and rule is None:
            continue  # 不支持的 RRULE 已上报；退化为只保留 DTSTART 单实例会误导，故跳过规则

        # 1) 主规则候选（RRULE；无 RRULE 时主事件只有 DTSTART 一次）
        master_starts: list = []
        if rule is not None:
            # dateutil 要求查询边界与 dtstart 同类型、同 aware 性质
            if isinstance(rule._dtstart, dt.datetime):
                search_lo = _as_datetime(range_lo) - master.duration
                search_hi = _as_datetime(range_hi)
                if rule._dtstart.tzinfo is not None:
                    tz = rule._dtstart.tzinfo
                    search_lo = search_lo.replace(tzinfo=tz)
                    search_hi = search_hi.replace(tzinfo=tz)
            else:
                search_lo = _as_date(range_lo)
                if master.duration.days:
                    search_lo = search_lo - dt.timedelta(days=master.duration.days)
                search_hi = _as_date(range_hi)
            rule_set = rruleset()
            rule_set.rrule(rule)
            for cand in rule_set.between(search_lo, search_hi, inc=True):
                master_starts.append(cand)
                if guard_limit():
                    break
        else:
            master_starts = [master.dtstart]

        # 2) EXDATE：从主规则候选中剔除，并单独留证
        exdate_keys: set[str] = set()
        for exd in master.exdates:
            key = instance_key(exd)
            exdate_keys.add(key)
            excluded.append({
                "uid": master.uid,
                "recurrence_id": key,
                "excluded_at": isoformat_local(exd),
                "line": master.index.property_lines.get("EXDATE"),
            })

        # 3) RDATE 额外日期（PERIOD 值明确不支持）
        rdate_starts: list = []
        for rdv in master.rdates:
            type_name = type(rdv).__name__
            if type_name == "Period" or hasattr(rdv, "start") and hasattr(rdv, "end") and not isinstance(rdv, (dt.datetime, dt.date)):
                issues.append(Issue.make(
                    IssueCode.RDATE_PERIOD,
                    "RDATE 使用 PERIOD 值，本采集器只支持 DATE/DATE-TIME，该值已跳过",
                    uid=master.uid, line=master.index.property_lines.get("RDATE"),
                    property_name="RDATE",
                ))
                continue
            if not isinstance(rdv, (dt.datetime, dt.date)):
                issues.append(Issue.make(
                    IssueCode.RDATE_BAD_VALUE,
                    f"RDATE 值类型 {type_name} 无法解析，已跳过",
                    uid=master.uid, line=master.index.property_lines.get("RDATE"),
                    property_name="RDATE",
                ))
                continue
            rdate_starts.append(rdv)

        # 4) RECURRENCE-ID 例外替换/取消
        for key, ov in override_map.items():
            if ov.uid != master.uid:
                continue
            rid_prop = ov.raw.get("RECURRENCE-ID")
            rid_line = ov.index.property_lines.get("RECURRENCE-ID")
            range_param = rid_prop.params.get("RANGE") if hasattr(rid_prop, "params") else None
            if range_param is not None:
                issues.append(Issue.make(
                    IssueCode.RECURRENCE_ID_RANGE,
                    f"RECURRENCE-ID 带 RANGE={range_param} 参数，不受支持，该例外已跳过",
                    uid=master.uid, line=rid_line, property_name="RECURRENCE-ID",
                ))
                continue
            is_cancel = (ov.status or "").upper() == "CANCELLED"
            if is_cancel:
                instances.append(_make_instance(
                    ov, ov.recurrence_id, "override_cancel",
                    f"该实例被 RECURRENCE-ID={isoformat_local(ov.recurrence_id)} 的例外 VEVENT 取消"
                    f"（STATUS=CANCELLED，SEQUENCE={ov.sequence}）",
                    status="CANCELLED", override_seq=ov.sequence,
                ))
                continue
            # 移动：以例外事件自己的 DTSTART 为准，身份仍挂在被替换的 RECURRENCE-ID 上
            moved = _make_instance(
                ov, ov.dtstart, "override_move",
                f"主规则在 {isoformat_local(ov.recurrence_id)} 的实例被 RECURRENCE-ID 例外移动到 "
                f"{isoformat_local(ov.dtstart)}（SEQUENCE={ov.sequence}）",
                status=ov.status, override_seq=ov.sequence,
            )
            moved.recurrence_id = key
            moved.instance_id = f"{ov.uid}::{key}"
            instances.append(moved)

        # 5) 组装普通实例：主规则减去 EXDATE 与被例外接管的键，再加 RDATE
        replaced_keys = {
            key for key, ov in override_map.items() if ov.uid == master.uid
        }
        for start in master_starts:
            key = instance_key(start)
            if key in exdate_keys:
                continue
            if key in replaced_keys:
                continue  # 被 RECURRENCE-ID 例外接管（移动或取消），不残留主规则旧实例
            instances.append(_make_instance(
                master, start, "master",
                "由 RRULE 主规则（或无重复事件的 DTSTART）展开的常规实例",
                status=None, override_seq=None,
            ))

        for start in rdate_starts:
            key = instance_key(start)
            if key in exdate_keys:
                continue
            if key in replaced_keys:
                continue
            instances.append(_make_instance(
                master, start, "rdate",
                f"由 RDATE 额外日期 {isoformat_local(start)} 加入的附加实例",
                status=None, override_seq=None,
            ))

    # 孤儿 RECURRENCE-ID（UID 找不到主事件）
    master_uids = {m.uid for m in masters}
    for key, ov in override_map.items():
        if ov.uid not in master_uids:
            issues.append(Issue.make(
                IssueCode.NO_MASTER,
                f"RECURRENCE-ID 例外 {key} 找不到 UID={ov.uid} 的主事件，已作为孤立例外跳过",
                uid=ov.uid, line=ov.index.property_lines.get("RECURRENCE-ID"),
                property_name="RECURRENCE-ID",
            ))

    # 6) 查询范围过滤（按半开区间相交判断；取消实例保留以便时间轴展示取消痕迹）
    def in_range(inst: ExpandedInstance) -> bool:
        start = _parse_iso(inst.start, inst.all_day)
        end = _parse_iso(inst.end, inst.all_day)
        if inst.all_day:
            lo = range_lo.date() if isinstance(range_lo, dt.datetime) else range_lo
            hi = range_hi.date() if isinstance(range_hi, dt.datetime) else range_hi
        else:
            lo = range_lo if isinstance(range_lo, dt.datetime) else dt.datetime.combine(range_lo, dt.time.min)
            hi = range_hi if isinstance(range_hi, dt.datetime) else dt.datetime.combine(range_hi, dt.time.min)
        return start < hi and end > lo

    filtered = [i for i in instances if in_range(i)]

    # 7) 身份去重：同 UID + 同 RECURRENCE-ID 只保留一条（不同 UID 即使开始时间相同也都保留）
    deduped: dict[str, ExpandedInstance] = {}
    for inst in filtered:
        deduped.setdefault(inst.instance_id, inst)

    ordered = sorted(deduped.values(), key=lambda i: (i.start, i.uid))
    return {
        "instances": [i.to_dict() for i in ordered],
        "excluded": excluded,
        "issues": [i.to_dict() for i in issues],
        "stats": {
            "candidates_produced": produced,
            "in_range": len(ordered),
            "cancelled": sum(1 for i in ordered if i.status == "CANCELLED"),
            "master_events": len(masters),
            "override_events": len(overrides),
            "excluded_count": len(excluded),
        },
    }


def _parse_iso(value: str, all_day: bool):
    if all_day:
        # 全日事件统一截成日期（其序列化形式可能带 T00:00:00）
        return dt.date.fromisoformat(value[:10])
    return dt.datetime.fromisoformat(value)


def _as_datetime(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.combine(value, dt.time.min)


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    return value
