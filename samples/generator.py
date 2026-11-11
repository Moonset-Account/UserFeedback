"""虚构样例数据生成器（与采集器完全分离）。

本模块负责：
1. 生成一组虚构的天文台公开观测窗口 ICS 文件到 generated/source/，
   作为本地源站（网页 /source/... 路由）提供的公开数据；
2. 生成独立真值 generated/truth/，仅供测试断言使用——采集器代码不读取、
   网页源站路由也不暴露该目录，测试只能通过公开采集接口拿到结果后再与真值比对。

所有机构、事件均为虚构，不依赖任何真实账户或外部服务。
"""

from __future__ import annotations

import json
import pathlib

# --------------------------------------------------------------------------- #
# 场景预设（网页下拉框 / run-demo 按钮 / 测试共用的唯一配置来源）
# --------------------------------------------------------------------------- #

PRESETS: dict[str, dict] = {
    "demo_normal": {
        "title": "示例一 · 正常场景：周五常规观星 + RDATE 加场",
        "file": "demo_normal.ics",
        "range_start": "2026-09-01",
        "range_end": "2026-10-11",
    },
    "demo_boundary": {
        "title": "示例二 · 边界场景：取消 + 移动 + 夏令时 + 无终止重复",
        "file": "demo_boundary.ics",
        "range_start": "2026-10-24",
        "range_end": "2026-12-07",
    },
    "acceptance_full": {
        "title": "完整验收：取消/移动/夏令时/无终止/跨日/全日/浮动/不支持规则",
        "file": "acceptance_full.ics",
        "range_start": "2026-10-24",
        "range_end": "2026-12-15",
    },
    "modify_override_v1": {
        "title": "例外修改对照（第一版）：移动到周三",
        "file": "modify_override_v1.ics",
        "range_start": "2026-11-01",
        "range_end": "2026-12-15",
    },
    "modify_override_v2": {
        "title": "例外修改对照（第二版）：改移动到周日，旧实例不得残留",
        "file": "modify_override_v2.ics",
        "range_start": "2026-11-01",
        "range_end": "2026-12-15",
    },
    "unsupported_rules": {
        "title": "不支持规则上报：HOURLY 与 BYYEARDAY 必须被报告",
        "file": "unsupported_rules.ics",
        "range_start": "2026-11-01",
        "range_end": "2026-11-10",
    },
    "malformed": {
        "title": "错误展示：残缺 VEVENT（缺 UID/DTSTART）",
        "file": "malformed.ics",
        "range_start": "2026-11-01",
        "range_end": "2026-11-10",
    },
}


# --------------------------------------------------------------------------- #
# ICS 文本（手工构造，保证行号证据可预测）
# --------------------------------------------------------------------------- #

_HEADER = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Fictional Observatory//Observation Window Demo//ZH-CN
CALSCALE:GREGORIAN
"""

_FOOTER = "END:VCALENDAR\n"

# 纽约时区：2026-11-01 06:00 UTC 由 EDT(UTC-4) 切回 EST(UTC-5)
_NY_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:STANDARD
DTSTART:19701101T020000
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
TZNAME:EST
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700308T020000
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
TZNAME:EDT
END:DAYLIGHT
END:VTIMEZONE
"""

_DEMO_NORMAL = _HEADER + """BEGIN:VEVENT
UID:fictional-planetarium-weekly-2026
SUMMARY:虚构市天文馆 · 周五公众观星夜
DTSTAMP:20260820T010000Z
DTSTART:20260904T193000
DTEND:20260904T213000
RRULE:FREQ=WEEKLY;BYDAY=FR;UNTIL=20261009T213000
RDATE:20260916T193000
END:VEVENT
""" + _FOOTER

_DEMO_BOUNDARY = _HEADER + _NY_VTIMEZONE + """BEGIN:VEVENT
UID:fictional-hilltop-stargazing-forever
SUMMARY:虚构山顶天文台 · 无终止周六观星（纽约时间）
DTSTAMP:20261001T000000Z
DTSTART;TZID=America/New_York:20261024T200000
DTEND;TZID=America/New_York:20261024T220000
RRULE:FREQ=WEEKLY;BYDAY=SA
EXDATE;TZID=America/New_York:20261031T200000
END:VEVENT
BEGIN:VEVENT
UID:fictional-hilltop-stargazing-forever
SUMMARY:虚构山顶天文台 · 设备检修，本次取消
DTSTAMP:20261102T000000Z
DTSTART;TZID=America/New_York:20261107T200000
DTEND;TZID=America/New_York:20261107T220000
RECURRENCE-ID;TZID=America/New_York:20261107T200000
SEQUENCE:1
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:fictional-hilltop-stargazing-forever
SUMMARY:虚构山顶天文台 · 流星雨加映，移至周五晚
DTSTAMP:20261105T000000Z
DTSTART;TZID=America/New_York:20261113T190000
DTEND;TZID=America/New_York:20261113T220000
RECURRENCE-ID;TZID=America/New_York:20261114T200000
SEQUENCE:1
END:VEVENT
""" + _FOOTER

_ACCEPTANCE_FULL = _HEADER + _NY_VTIMEZONE + """BEGIN:VEVENT
UID:fictional-hilltop-stargazing-forever
SUMMARY:虚构山顶天文台 · 无终止周六观星（纽约时间）
DTSTAMP:20261001T000000Z
DTSTART;TZID=America/New_York:20261024T200000
DTEND;TZID=America/New_York:20261024T220000
RRULE:FREQ=WEEKLY;BYDAY=SA
EXDATE;TZID=America/New_York:20261031T200000
END:VEVENT
BEGIN:VEVENT
UID:fictional-hilltop-stargazing-forever
SUMMARY:虚构山顶天文台 · 设备检修，本次取消
DTSTAMP:20261102T000000Z
DTSTART;TZID=America/New_York:20261107T200000
DTEND;TZID=America/New_York:20261107T220000
RECURRENCE-ID;TZID=America/New_York:20261107T200000
SEQUENCE:1
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
UID:fictional-hilltop-stargazing-forever
SUMMARY:虚构山顶天文台 · 流星雨加映，移至周五晚
DTSTAMP:20261105T000000Z
DTSTART;TZID=America/New_York:20261113T190000
DTEND;TZID=America/New_York:20261113T220000
RECURRENCE-ID;TZID=America/New_York:20261114T200000
SEQUENCE:1
END:VEVENT
BEGIN:VEVENT
UID:fictional-lakeside-all-day-workshop
SUMMARY:虚构湖畔观测站 · 全日跨日摄影工作坊
DTSTAMP:20261010T000000Z
DTSTART;VALUE=DATE:20261114
DTEND;VALUE=DATE:20261117
RRULE:FREQ=MONTHLY;BYMONTHDAY=14;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:fictional-rooftop-floating-daily
SUMMARY:虚构社区屋顶 · 浮动时间每日晨间观测（源数据未给时区）
DTSTAMP:20261020T000000Z
DTSTART:20261201T063000
DTEND:20261201T071500
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:fictional-old-horology-byearday
SUMMARY:虚构旧钟馆 · 含不受支持的 BYYEARDAY 规则
DTSTAMP:20261020T000000Z
DTSTART:20261101T100000
RRULE:FREQ=YEARLY;BYYEARDAY=200
END:VEVENT
""" + _FOOTER

_MODIFY_V1 = _HEADER + """BEGIN:VEVENT
UID:fictional-mountain-modify-demo
SUMMARY:虚构东山观测点 · 周一基础观测
DTSTAMP:20261025T000000Z
DTSTART:20261102T190000
DTEND:20261102T203000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=5
END:VEVENT
BEGIN:VEVENT
UID:fictional-mountain-modify-demo
SUMMARY:虚构东山观测点 · 11月16日场次改到周三
DTSTAMP:20261101T000000Z
DTSTART:20261118T190000
DTEND:20261118T203000
RECURRENCE-ID:20261116T190000
SEQUENCE:1
END:VEVENT
""" + _FOOTER

_MODIFY_V2 = _HEADER + """BEGIN:VEVENT
UID:fictional-mountain-modify-demo
SUMMARY:虚构东山观测点 · 周一基础观测
DTSTAMP:20261025T000000Z
DTSTART:20261102T190000
DTEND:20261102T203000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=5
END:VEVENT
BEGIN:VEVENT
UID:fictional-mountain-modify-demo
SUMMARY:虚构东山观测点 · 11月16日场次改到周日（第二版修订）
DTSTAMP:20261110T000000Z
DTSTART:20261115T100000
DTEND:20261115T113000
RECURRENCE-ID:20261116T190000
SEQUENCE:2
END:VEVENT
""" + _FOOTER

_UNSUPPORTED = _HEADER + """BEGIN:VEVENT
UID:fictional-hourly-weather-camera
SUMMARY:虚构气象相机 · 每小时连拍（HOURLY 不在支持子集）
DTSTAMP:20261020T000000Z
DTSTART:20261101T000000Z
RRULE:FREQ=HOURLY;COUNT=24
END:VEVENT
BEGIN:VEVENT
UID:fictional-solstice-special
SUMMARY:虚构至日特别场 · BYYEARDAY 规则（不支持）
DTSTAMP:20261020T000000Z
DTSTART;VALUE=DATE:20260101
RRULE:FREQ=YEARLY;BYYEARDAY=355
END:VEVENT
""" + _FOOTER

_MALFORMED = _HEADER + """BEGIN:VEVENT
SUMMARY:虚构残缺事件 · 缺少 UID 与 DTSTART
DTSTAMP:20261020T000000Z
RRULE:FREQ=WEEKLY
END:VEVENT
BEGIN:VEVENT
UID:fictional-valid-single-window
SUMMARY:虚构合法单场观测（同文件中仍应正常采集）
DTSTAMP:20261020T000000Z
DTSTART:20261105T180000
DTEND:20261105T200000
END:VEVENT
""" + _FOOTER

ICS_FILES: dict[str, str] = {
    "demo_normal.ics": _DEMO_NORMAL,
    "demo_boundary.ics": _DEMO_BOUNDARY,
    "acceptance_full.ics": _ACCEPTANCE_FULL,
    "modify_override_v1.ics": _MODIFY_V1,
    "modify_override_v2.ics": _MODIFY_V2,
    "unsupported_rules.ics": _UNSUPPORTED,
    "malformed.ics": _MALFORMED,
}


# --------------------------------------------------------------------------- #
# 独立真值：手工列出预期实例（身份、来源、状态），测试拿采集结果逐字段比对
# --------------------------------------------------------------------------- #

def _truth_demo_normal() -> dict:
    fridays = ["2026-09-04", "2026-09-11", "2026-09-18", "2026-09-25", "2026-10-02", "2026-10-09"]
    instances = [
        {
            "recurrence_id": f"float:{d}T19:30:00",
            "start": f"{d}T19:30:00", "end": f"{d}T21:30:00",
            "origin": "master", "status": "CONFIRMED", "floating": True,
        } for d in fridays
    ]
    instances.append({
        "recurrence_id": "float:2026-09-16T19:30:00",
        "start": "2026-09-16T19:30:00", "end": "2026-09-16T21:30:00",
        "origin": "rdate", "status": "CONFIRMED", "floating": True,
    })
    return {"expect_count": 7, "instances": sorted(instances, key=lambda x: x["start"]),
            "expect_issues": []}


def _truth_demo_boundary() -> dict:
    # 20:00 纽约本地：11/1 之前为 EDT（UTC-4），之后为 EST（UTC-5）
    sats_edt = ["2026-10-24"]  # 10-24 在 EDT 期内
    sats_est = ["2026-10-31", "2026-11-07", "2026-11-14", "2026-11-21",
                "2026-11-28", "2026-12-05"]
    instances = []
    for d in sats_edt:
        instances.append({
            "recurrence_id": f"dt:{d}T20:00:00|off:-240",
            "start": f"{d}T20:00:00", "origin": "master",
            "start_utc": f"{d}T00:00:00+00:00", "tzid": "America/New_York",
        })
    # 10-31 被 EXDATE 排除，不在实例中
    for d in sats_est[1:]:  # 跳过 10-31
        instances.append({
            "recurrence_id": f"dt:{d}T20:00:00|off:-300",
            "start": f"{d}T20:00:00", "origin": "master",
            "start_utc": f"{d}T01:00:00+00:00", "tzid": "America/New_York",
        })
    # 取消：11-07 仍以 CANCELLED 实例保留痕迹
    instances.append({
        "recurrence_id": "dt:2026-11-07T20:00:00|off:-300",
        "start": "2026-11-07T20:00:00", "origin": "override_cancel",
        "status": "CANCELLED", "tzid": "America/New_York",
    })
    # 移动：11-14 的身份挂在原 RECURRENCE-ID 上，开始时间变为 11-13 19:00
    instances.append({
        "recurrence_id": "dt:2026-11-14T20:00:00|off:-300",
        "start": "2026-11-13T19:00:00", "end": "2026-11-13T22:00:00",
        "origin": "override_move", "status": "CONFIRMED",
        "tzid": "America/New_York",
    })
    # master 列表里去掉被例外接管的两个键
    instances = [i for i in instances if i["recurrence_id"] not in (
        "dt:2026-11-07T20:00:00|off:-300",
        "dt:2026-11-14T20:00:00|off:-300",
    ) or i["origin"].startswith("override")]
    return {
        "expect_count": 6,
        "instances": sorted(instances, key=lambda x: x["start"]),
        "expect_issues": [],
        "expect_excluded": ["dt:2026-10-31T20:00:00|off:-240"],
        "dst_check": {
            "before": {"start": "2026-10-24T20:00:00", "utc": "2026-10-25T00:00:00+00:00"},
            "after": {"start": "2026-11-21T20:00:00", "utc": "2026-11-22T01:00:00+00:00"},
        },
    }


def _truth_modify(v2: bool) -> dict:
    mondays = ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"]
    instances = [{
        "recurrence_id": f"float:{d}T19:00:00",
        "start": f"{d}T19:00:00", "origin": "master",
    } for d in mondays if d != "2026-11-16"]
    if v2:
        instances.append({
            "recurrence_id": "float:2026-11-16T19:00:00",
            "start": "2026-11-15T10:00:00", "end": "2026-11-15T11:30:00",
            "origin": "override_move",
        })
        moved_start = "2026-11-15T10:00:00"
    else:
        instances.append({
            "recurrence_id": "float:2026-11-16T19:00:00",
            "start": "2026-11-18T19:00:00", "end": "2026-11-18T20:30:00",
            "origin": "override_move",
        })
        moved_start = "2026-11-18T19:00:00"
    return {
        "expect_count": 5,
        "instances": sorted(instances, key=lambda x: x["start"]),
        "moved_instance_start": moved_start,
        "moved_recurrence_id": "float:2026-11-16T19:00:00",
        "forbidden_starts": ["2026-11-18T19:00:00"] if v2 else ["2026-11-15T10:00:00"],
    }


def _truth_unsupported() -> dict:
    return {
        "expect_count": 0,
        "instances": [],
        "expect_issue_codes": ["RRULE_HOURLY", "RRULE_BY_YEAR_DAY"],
    }


def _truth_malformed() -> dict:
    return {
        "expect_count": 1,
        "instances": [{
            "uid": "fictional-valid-single-window",
            "start": "2026-11-05T18:00:00", "origin": "master",
        }],
        "expect_issue_codes": ["INVALID_CALENDAR"],
    }


def _truth_acceptance() -> dict:
    return {
        "expect_issue_codes": ["RRULE_BY_YEAR_DAY"],
        "uids": [
            "fictional-hilltop-stargazing-forever",
            "fictional-lakeside-all-day-workshop",
            "fictional-rooftop-floating-daily",
        ],
        "all_day_instance": {
            "uid": "fictional-lakeside-all-day-workshop",
            "first_start": "2026-11-14", "first_end": "2026-11-17", "all_day": True,
        },
        "floating_instance": {
            "uid": "fictional-rooftop-floating-daily",
            "first_start": "2026-12-01T06:30:00", "floating": True, "start_utc": None,
        },
    }


def build_truth() -> dict[str, dict]:
    return {
        "demo_normal": _truth_demo_normal(),
        "demo_boundary": _truth_demo_boundary(),
        "acceptance_full": _truth_acceptance(),
        "modify_override_v1": _truth_modify(v2=False),
        "modify_override_v2": _truth_modify(v2=True),
        "unsupported_rules": _truth_unsupported(),
        "malformed": _truth_malformed(),
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def generate(base_dir: str | pathlib.Path) -> dict:
    """生成 ICS 源文件与独立真值，返回生成的文件清单。"""
    base = pathlib.Path(base_dir)
    source_dir = base / "source"
    truth_dir = base / "truth"
    source_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    written = {"ics": [], "truth": []}
    for name, content in ICS_FILES.items():
        path = source_dir / name
        path.write_text(content, encoding="utf-8")
        written["ics"].append(str(path))

    truth = build_truth()
    for key, value in truth.items():
        path = truth_dir / f"{key}.json"
        path.write_text(
            json.dumps({"preset": PRESETS[key], "truth": value},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written["truth"].append(str(path))
    return written


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成虚构天文台观测窗口样例数据")
    parser.add_argument("--out", default="generated", help="输出目录（默认 generated/）")
    args = parser.parse_args()
    result = generate(args.out)
    print(f"已生成 {len(result['ics'])} 个 ICS 源文件与 {len(result['truth'])} 份独立真值：")
    for p in result["ics"]:
        print("  ICS   ", p)
    for p in result["truth"]:
        print("  TRUTH ", p)
