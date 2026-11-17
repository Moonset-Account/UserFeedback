#!/usr/bin/env python3
"""本地核验入口（合成数据 + 真实断言）。

用法::

    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py            # 运行全部用例

任一断言失败、用例入口缺失、未实现、零断言或整组跳过均以非零码退出；
成功不是“打印文字”，而是每条断言都真实执行并通过。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from doc_rewrite.analyzer import (  # noqa: E402
    CONFIRMED,
    REASON_MISSING_PRE_EVIDENCE,
    REASON_NEW_ARCHIVE,
    TENTATIVE,
    RewriteAnalyzer,
)
from doc_rewrite.events import event_from_dict  # noqa: E402
from doc_rewrite.policy import Policy  # noqa: E402

SAMPLE_DIR = ROOT / "samples"

CASES: dict[str, dict[str, str]] = {
    "C01": {
        "file": "C01_identity_chain.json",
        "title": "文件身份（重命名/硬链接）与派生链去重",
    },
    "C02": {
        "file": "C02_qualification_archive.json",
        "title": "关闭确认才计风险；新建归档保留源文档排除",
    },
    "C03": {
        "file": "C03_watermark_out_of_order.json",
        "title": "乱序、水位线、暂定/确认与双阈值",
    },
    "C04": {
        "file": "C04_unknown_conflict.json",
        "title": "缺写前证据与身份冲突分表；最小支持集合",
    },
}


@dataclass
class Failure:
    name: str
    detail: str


class Checker:
    """断言收集器：只允许通过/失败，不允许跳过；零断言视为失败。"""

    def __init__(self, case: str) -> None:
        self.case = case
        self.passed = 0
        self.failed = 0
        self.failures: list[Failure] = []
        self.observations: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(Failure(name, detail))

    def eq(self, name: str, actual: Any, expected: Any) -> None:
        ok = actual == expected
        self.check(
            name,
            ok,
            f"expected={expected!r} observed={actual!r}" if not ok else "",
        )

    def note(self, text: str) -> None:
        self.observations.append(text)

    @property
    def total(self) -> int:
        return self.passed + self.failed


def _load(case: str) -> tuple[dict[str, Any], Policy, list[Event]]:
    meta = CASES[case]
    path = SAMPLE_DIR / meta["file"]
    if not path.exists():
        raise FileNotFoundError(f"样本缺失: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
    policy = Policy.from_dict(doc.get("policy"))
    events = [event_from_dict(item) for item in doc["events"]]
    return doc, policy, events


def _replay(
    policy: Policy,
    events: list[Event],
    watermark: Optional[float] = None,
    drop_seqs: Optional[set[int]] = None,
    extra_events: Optional[list[Event]] = None,
    policy_override: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if policy_override:
        merged = {
            "window_seconds": policy.window_seconds,
            "min_documents": policy.min_documents,
            "min_distinct_dirs": policy.min_distinct_dirs,
            "high_entropy_threshold": policy.high_entropy_threshold,
            "registered_categories": set(policy.registered_categories),
            "unknown_category": policy.unknown_category,
            "archive_categories": set(policy.archive_categories),
            "archive_path_suffixes": set(policy.archive_path_suffixes),
            "dir_depth": policy.dir_depth,
        }
        merged.update(policy_override)
        used_policy = Policy.from_dict(merged)
    else:
        used_policy = policy
    analyzer = RewriteAnalyzer(used_policy)
    drop_seqs = drop_seqs or set()
    for event in events:
        if event.seq in drop_seqs:
            continue
        # 显式水位线由测试参数统一控制时，忽略样本内的 watermark 事件，
        # 避免流内高水位线让显式低水位线退化成无效回退。
        if watermark is not None and event.etype.value == "watermark":
            continue
        analyzer.ingest(event)
    for event in extra_events or []:
        analyzer.ingest(event)
    if watermark is not None:
        analyzer.advance_watermark(watermark)
    return analyzer.snapshot()


def _records_by_file(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["file_id"]: r for r in snapshot["affected_records"]}


def _first_finding(snapshot: dict[str, Any]) -> dict[str, Any]:
    assert snapshot["findings"], "预期至少一个发现"
    return snapshot["findings"][0]


# ----------------------------------------------------------------------
# C01
# ----------------------------------------------------------------------
def run_c01() -> Checker:
    case = "C01"
    doc, policy, events = _load(case)
    snapshot = _replay(policy, events, watermark=100)
    chk = Checker(case)

    chk.eq("事件总数与样本一致", snapshot["stats"]["events_ingested"], 12)
    chk.eq("受影响不同文档数=2（F1/F2）", snapshot["stats"]["affected_documents"], 2)
    chk.eq("受影响记录数=2（链内同文件多次写入合并）",
           snapshot["stats"]["affected_records"], 2)

    records = _records_by_file(snapshot)
    chk.eq("F1 产生一条风险记录", set(records), {"F1", "F2"})
    f1 = records["F1"]
    chk.eq("F1 派生链根为 P0", f1["chain_root"], "P0")
    chk.eq("F1 进程世代随记录输出（P1=1, P2=2）", f1["generations"], [1, 2])
    chk.eq("F1 同链三次写入只计一份文档（仅 1 次携带登记类写前证据）",
           f1["modification_count"], 1)
    chk.eq("F1 总写入事件数为 3（多次修改事实不丢失）",
           len([e for e in _load(case)[2]
                if e.etype.value == "write" and e.file_id == "F1"]), 3)
    chk.eq("F1 风险路径跟随重命名", f1["path"], "/home/u/docs/renamed.docx")
    chk.eq("F1 目录按最终路径统计", f1["directory"], "/home/u/docs")
    chk.eq("F1 最小支持集合=最早登记类写入+关闭确认（后续写入不混入）",
           f1["support_event_seqs"], [4, 9])
    chk.eq("F1 身份轨迹单独记录 rename+hardlink",
           f1["identity_event_seqs"], [5, 6])
    f2 = records["F2"]
    chk.eq("F2 链根同样回溯到 P0（同派生链不同文件仍分别计）",
           f2["chain_root"], "P0")
    chk.eq("F2 目录独立", f2["directory"], "/home/u/sheets")

    findings = snapshot["findings"]
    chk.eq("产生一个发现", len(findings), 1)
    finding = findings[0]
    chk.eq("发现已确认（水位线 100 越过最晚关闭 40 + 窗口 60）",
           finding["status"], CONFIRMED)
    chk.eq("发现文档数=2", finding["document_count"], 2)
    chk.eq("发现独立目录数=2", finding["distinct_directories"], 2)
    chk.eq("发现覆盖两个目录", finding["directories"],
           ["/home/u/docs", "/home/u/sheets"])
    chk.eq("发现链根集合", finding["chain_roots"], ["P0"])
    chk.eq("发现最小支持集合为各记录并集",
           finding["support_event_seqs"], [4, 9, 11, 12])

    # 暂定 -> 确认的生命周期：水位线停在 30（确认就绪时刻为 40+60=100）
    pre = _replay(policy, events, watermark=30)
    chk.eq("窗口未闭合前发现暂定", pre["findings"][0]["status"], TENTATIVE)
    chk.eq("暂定发现文档计数不变",
           pre["findings"][0]["document_count"], 2)

    # 无身份冲突、无未知项
    chk.eq("无身份冲突", snapshot["stats"]["identity_conflicts"], 0)
    chk.eq("无缺证据未知文件", snapshot["stats"]["unknown_files"], 0)

    # 最小性：逐一剔除支持集合中的事件，事实必须被削弱
    support = finding["support_event_seqs"]
    for seq in support:
        reduced = _replay(policy, events, watermark=100, drop_seqs={seq})
        weakened = (
            reduced["stats"]["affected_documents"] < 2
            or not reduced["findings"]
            or reduced["findings"][0]["support_size"] < finding["support_size"]
        )
        chk.check(f"最小性：剔除 seq={seq} 后事实被削弱", weakened,
                  f"剔除 seq={seq} 后发现仍原样成立，说明它不是最小支持事件")

    # 非最小事件反例：seq=7、seq=8 都是高熵未知阶段的 churn 写入
    # （无登记类写前证据），本就不在最小支持集合内；逐一剔除后发现不变。
    for churn_seq in (7, 8):
        reduced = _replay(policy, events, watermark=100,
                          drop_seqs={churn_seq})
        chk.eq(f"剔除 churn 写入 seq={churn_seq} 不改变最小支持集合",
               reduced["findings"][0]["support_event_seqs"],
               finding["support_event_seqs"])
        chk.eq(f"剔除 churn 写入 seq={churn_seq} 不影响登记类修改计数",
               _records_by_file(reduced)["F1"]["modification_count"],
               f1["modification_count"])

    # 身份事件不属于最小支持集合：剔除 rename 不影响改写成立，只影响轨迹
    reduced = _replay(policy, events, watermark=100, drop_seqs={5})
    chk.eq("剔除 rename 后发现仍成立（身份事件不在最小集合）",
           len(reduced["findings"]), 1)
    chk.eq("剔除 rename 后身份轨迹相应缩短",
           _records_by_file(reduced)["F1"]["identity_event_seqs"], [6])

    chk.note(f"seed=sample/{CASES[case]['file']}；受影响文档 F1(3 次写入合并为 1 份)/F2；"
             f"期望 1 个确认发现，观察 {len(findings)} 个，状态 {finding['status']}")
    return chk


# ----------------------------------------------------------------------
# C02
# ----------------------------------------------------------------------
def run_c02() -> Checker:
    case = "C02"
    doc, policy, events = _load(case)
    snapshot = _replay(policy, events, watermark=200)
    chk = Checker(case)

    records = _records_by_file(snapshot)
    chk.eq("仅 F1/F4 计风险（F2 低熵、F3 未关闭、F5 源文档、F6 归档）",
           set(records), {"F1", "F4"})
    chk.eq("受影响文档数=2", snapshot["stats"]["affected_documents"], 2)
    finding = _first_finding(snapshot)
    chk.eq("发现状态确认", finding["status"], CONFIRMED)
    chk.eq("发现文档数=2", finding["document_count"], 2)

    # F2：低熵未知 —— 关闭确认了但不高熵，不进风险；
    # 作为“熵无法证实高”的未定项保留可见，区别于缺写前证据。
    all_fids = {r["file_id"] for r in snapshot["affected_records"]}
    unknown_by_fid = {u["file_id"]: u for u in snapshot["unknown_files"]}
    unknown_fids = set(unknown_by_fid)
    excluded_fids = {e["file_id"] for e in snapshot["excluded"]}
    chk.check("F2 低熵未知不计风险", "F2" not in all_fids)
    chk.check("F2 进入未定清单而非静默丢弃", "F2" in unknown_by_fid)
    chk.eq("F2 原因是熵不达标/不可证实（不是缺写前证据）",
           unknown_by_fid["F2"]["reason"], "entropy_inconclusive")
    chk.check("F2 有登记类写前证据，因此不标记为 missing_pre_evidence",
           unknown_by_fid["F2"]["reason"] != "missing_pre_evidence")

    # F3：写入但从未关闭 —— 不提前计数
    chk.check("F3 未关闭不计数", "F3" not in all_fids and "F3" not in unknown_fids)
    # 构造 F1 只写不关的对照：剔除 F1 的关闭事件（保留水位线与其他文件）
    pre_close = _replay(
        policy,
        [e for e in events if not (e.file_id == "F1"
                                   and e.etype.value == "close")],
        watermark=200,
    )
    pre_records = _records_by_file(pre_close)
    chk.check("F1 只写不关时 F1 不产生风险记录", "F1" not in pre_records)
    chk.eq("F1 只写不关时全局无发现", pre_close["stats"]["findings_total"], 0)
    # F4 仍然合格，说明“关闭确认”是逐文件判定而非全局开关
    chk.check("F1 缺关闭不影响 F4 的独立判定", "F4" in pre_records)

    # 熵边界：把阈值抬到 7.9（F1 下界 7.9 仍算，F4 下界 7.6 被剔除）
    strict = _replay(policy, events, watermark=200,
                     policy_override={"high_entropy_threshold": 7.9})
    chk.eq("阈值 7.9 时仅 F1 仍满足高熵",
            {r["file_id"] for r in strict["affected_records"]}, {"F1"})
    chk.eq("阈值 7.9 时文档不足，无发现", strict["stats"]["findings_total"], 0)

    # 归档：F6 新建、archive 类别、源 F5 仍存在 -> 排除且原因正确
    chk.check("F6 进入排除清单", "F6" in excluded_fids)
    f6 = next(e for e in snapshot["excluded"] if e["file_id"] == "F6")
    chk.eq("F6 排除原因为新建归档且保留源", f6["reason"], REASON_NEW_ARCHIVE)
    chk.eq("F6 记录保留的源文档", f6["archived_source"], "F5")
    chk.check("源文档 F5 未被计为改写", "F5" not in all_fids)

    # 反例：源文档若已不存在（归档未真正保留源），不能套用归档排除
    dropped_source = _replay(policy, events, watermark=200,
                             drop_seqs={13, 14})
    # F5 消失后 F6 不再满足“保留源文档”；F6 是 new_file 且无登记类写前证据
    f6_after = next((e for e in dropped_source["excluded"]
                     if e["file_id"] == "F6"), None)
    chk.check("源不在场时不给出 new_archive 排除",
              f6_after is None or f6_after["reason"] != REASON_NEW_ARCHIVE)

    chk.eq("无身份冲突", snapshot["stats"]["identity_conflicts"], 0)
    chk.eq("无缺写前证据类未知文件（唯一未知项 F2 是熵不达标）",
           sum(1 for u in snapshot["unknown_files"]
               if u["reason"] == REASON_MISSING_PRE_EVIDENCE), 0)

    chk.note("seed=sample/C02_qualification_archive.json；期望风险 {F1,F4}，"
             f"观察 {sorted(all_fids)}；排除 {sorted(excluded_fids)}；"
             f"未定 {sorted(unknown_fids)}")
    return chk


# ----------------------------------------------------------------------
# C03
# ----------------------------------------------------------------------
def run_c03() -> Checker:
    case = "C03"
    doc, policy, events = _load(case)
    late_events = [event_from_dict(item) for item in doc["late_events"]]
    chk = Checker(case)

    last_snapshot: Optional[dict[str, Any]] = None
    tentative_support: Optional[list[int]] = None

    for cp in doc["checkpoints"]:
        target = cp["after_delivered"]
        delivered = events[:target]
        # 每个检查点使用全新分析器：真实模拟“截至该送达进度 + 该水位线”的
        # 在线状态，而不是在同一实例上反复移动水位线。
        snap = _replay(policy, delivered, watermark=cp["watermark"])
        last_snapshot = snap
        expect = cp["expect"]
        label = f"检查点(已送达 {target}, wm={cp['watermark']})"
        for key, wanted in expect.items():
            if key == "finding_document_count":
                chk.eq(f"{label} 发现文档数",
                       snap["findings"][0]["document_count"], wanted)
            elif key == "finding_distinct_directories":
                chk.eq(f"{label} 发现独立目录数",
                       snap["findings"][0]["distinct_directories"], wanted)
            elif key == "findings_tentative":
                chk.eq(f"{label} 暂定发现数",
                       snap["stats"]["findings_tentative"], wanted)
            elif key == "findings_confirmed":
                chk.eq(f"{label} 确认发现数",
                       snap["stats"]["findings_confirmed"], wanted)
            else:
                chk.eq(f"{label} {key}", snap["stats"].get(key), wanted)
        if snap["findings"] and tentative_support is None:
            tentative_support = list(snap["findings"][0]["support_event_seqs"])

    assert last_snapshot is not None
    # 迟到事件的真实在线形态：先送达全部事件并把水位线推到 120，
    # 随后一条 ts=35 的写入“补到达”——送达瞬间它已落在水位线之后。
    lc = doc["late_checkpoint"]
    online = RewriteAnalyzer(policy)
    for event in events[: lc["after_delivered"]]:
        online.ingest(event)
    online.advance_watermark(lc["watermark_before_straggler"])
    before_late = online.snapshot()
    before_support = list(before_late["findings"][0]["support_event_seqs"])
    for straggler in late_events:
        online.ingest(straggler)
    late_snap = online.snapshot()
    chk.eq("补到达事件被识别为迟到", late_snap["stats"]["late_events"], 1)
    chk.eq("迟到写入不产生重复受影响记录（同链同文件合并）",
           late_snap["stats"]["affected_records"],
           lc["expect"]["affected_records"])
    chk.eq("迟到写入后受影响文档数不变",
           late_snap["stats"]["affected_documents"], 4)
    chk.eq("迟到写入并入同链修改计数，不新增发现",
           late_snap["stats"]["findings_total"], lc["expect"]["findings_total"])
    chk.eq("确认状态不被迟到事件推翻",
           late_snap["findings"][0]["status"], CONFIRMED)
    if lc["expect"]["support_seqs_unchanged"]:
        chk.eq("发现最小支持集合不纳入迟到写入（关闭已在之前确认）",
               late_snap["findings"][0]["support_event_seqs"], before_support)
    f1_late = _records_by_file(late_snap)["F1"]
    chk.eq("迟到写入体现在 F1 的修改次数上（事实不丢失）",
           f1_late["modification_count"], 2)

    chk.check("F4（关闭 90）落在窗口外，未并入发现",
              "F4" not in set(late_snap["findings"][0]["file_ids"]))
    chk.eq("F4 自身仍是受影响记录（窗口外不等于无风险）",
           late_snap["stats"]["affected_records"], 4)

    # 乱序事实还原：先到关闭、后到写入，最终仍能配对出风险
    ordered_seqs = [e.seq for e in events]
    chk.check("样本送达顺序确实乱序（首条 seq=3）", ordered_seqs[0] == 3)

    # 双阈值反例（各自全新分析器，避免实例间状态污染）
    for variant in doc["threshold_variants"]:
        snap = _replay(policy, events, watermark=120,
                       policy_override=variant["override"])
        chk.eq(f"阈值反例 {variant['override']} 无发现",
               snap["stats"]["findings_total"], variant["expect_findings"])
        chk.check(f"阈值反例 {variant['override']} 受影响记录仍被保留",
                  snap["stats"]["affected_records"] == 4)

    # 目录阈值的正向对照：降到 1 时只靠单一目录也能触发发现
    relaxed = _replay(policy, events, watermark=120,
                      policy_override={"min_distinct_dirs": 1})
    chk.eq("目录阈值降到 1 时发现恢复",
           relaxed["stats"]["findings_total"], 1)

    # 水位线回退保护
    analyzer = RewriteAnalyzer(policy)
    for event in events:
        analyzer.ingest(event)
    analyzer.advance_watermark(120)
    analyzer.advance_watermark(60)
    snap = analyzer.snapshot()
    chk.eq("水位线不回退（回退尝试被计数）",
           snap["stats"]["watermark_regressions"], 1)
    chk.eq("回退尝试后水位线保持 120", snap["watermark"], 120)

    chk.note("seed=sample/C03_watermark_out_of_order.json；乱序首达 seq=3；"
             "暂定->确认在 wm=115；迟到写入 1 条（wm=120 后补到达）不改变发现")
    return chk


# ----------------------------------------------------------------------
# C04
# ----------------------------------------------------------------------
def run_c04() -> Checker:
    case = "C04"
    doc, policy, events = _load(case)
    snapshot = _replay(policy, events, watermark=100)
    chk = Checker(case)

    records = _records_by_file(snapshot)
    # F1、F2、F4 具备登记类写前证据；F3 缺证据；F5 缺证据
    chk.eq("具备写前证据的文件进入风险记录",
           set(records), {"F1", "F2", "F4"})

    unknowns = {u["file_id"]: u for u in snapshot["unknown_files"]}
    chk.eq("缺写前证据文件单列（F3、F5）", set(unknowns), {"F3", "F5"})
    chk.eq("未知原因统一为 missing_pre_evidence",
           {u["reason"] for u in unknowns.values()},
           {REASON_MISSING_PRE_EVIDENCE})
    chk.eq("F3 观察到零个登记类写前类别",
           unknowns["F3"]["pre_categories_seen"], [])
    chk.eq("F3 保留现场信息：写入事件数", unknowns["F3"]["write_events"], 1)
    chk.eq("F3 保留现场信息：涉及进程", unknowns["F3"]["observed_pids"], ["P1"])
    chk.check("缺证据文件不混入风险计数",
              not ({"F3", "F5"} & set(records)))

    # 身份冲突：F4 与 F5 占用同一路径，绝不按路径归并
    conflicts = snapshot["identity_conflicts"]
    chk.eq("身份冲突数量=1", len(conflicts), 1)
    conflict = conflicts[0]
    chk.eq("冲突路径", conflict["path"], "/d2/conflict.docx")
    chk.eq("冲突双方是不同 file_id（不按路径猜测归并）",
           conflict["distinct_file_ids"], ["F4", "F5"])
    chk.check("F4 与 F5 仍各自保留独立身份",
              "F4" in records and "F5" in unknowns)

    # 发现：F1、F2、F4 三个合格文档，目录 /d1、/d2
    finding = _first_finding(snapshot)
    chk.eq("发现文档数=3（F3/F5 缺证据不计入）",
           finding["document_count"], 3)
    chk.eq("发现独立目录数=2", finding["distinct_directories"], 2)
    chk.eq("发现 file_ids", finding["file_ids"], ["F1", "F2", "F4"])
    chk.eq("发现状态确认", finding["status"], CONFIRMED)
    chk.eq("最小支持事件集合（每文件最早登记类写入+关闭确认，共 6 个）",
           finding["support_event_seqs"], [3, 4, 6, 7, 12, 13])
    chk.eq("support_size 与集合一致", finding["support_size"], 6)

    # 最小性：集合中每个事件去除后，发现都无法原样成立
    for seq in finding["support_event_seqs"]:
        reduced = _replay(policy, events, watermark=100, drop_seqs={seq})
        weakened = (
            not reduced["findings"]
            or reduced["findings"][0]["document_count"] < 3
            or reduced["findings"][0]["support_size"] < finding["support_size"]
        )
        chk.check(f"最小性：剔除 seq={seq} 后发现被削弱", weakened,
                  f"seq={seq} 可被去除而发现不变，不满足最小支持集合")

    # 缺证据 vs 身份冲突是两张独立表
    chk.check("未知表与冲突表不重叠（身份冲突不自动变成未知归类依据）",
              all(u["file_id"] not in set(conflict["distinct_file_ids"])
                  or u["reason"] == REASON_MISSING_PRE_EVIDENCE
                  for u in snapshot["unknown_files"]))
    chk.eq("F5 同时出现在未知表，但冲突仍只在身份冲突表登记一次",
           len(conflicts), 1)

    # spawn 父声明冲突：同一 pid 两次 spawn 给出不同 ppid，登记冲突且不覆盖
    spawn_conflict = _replay(
        policy,
        events + [
            event_from_dict({"seq": 98, "ts": 58, "etype": "spawn",
                             "pid": "PX", "ppid": "P0", "gen": 3}),
            event_from_dict({"seq": 100, "ts": 60, "etype": "spawn",
                             "pid": "PX", "ppid": "OTHER", "gen": 3}),
        ],
        watermark=100,
    )
    spawn_entries = [
        c for c in spawn_conflict["identity_conflicts"]
        if c["reason"] == "spawn_parent_conflict"
    ]
    chk.eq("spawn 父声明冲突被单独登记", len(spawn_entries), 1)
    chk.eq("spawn 冲突定位到具体 pid", spawn_entries[0]["path"], "spawn:PX")
    # 原始派生链结论不被冲突声明改写
    chk.eq("冲突声明不覆盖既有父子关系，原发现不变",
           spawn_conflict["stats"]["findings_total"], 1)

    # 无 spawn 信息的进程自成链根，不与 P0 链混并
    orphan = _replay(
        Policy.from_dict({
            "window_seconds": 60.0, "min_documents": 1, "min_distinct_dirs": 1,
        }),
        [
            event_from_dict({"seq": 1, "ts": 1, "etype": "write",
                             "file_id": "O1", "path": "/od/x.doc", "pid": "ORPH",
                             "pre_category": "document",
                             "post_category": "unknown",
                             "entropy": {"low": 7.9, "high": 8.0}}),
            event_from_dict({"seq": 2, "ts": 2, "etype": "close",
                             "file_id": "O1", "path": "/od/x.doc", "pid": "ORPH",
                             "post_category": "unknown",
                             "entropy": {"low": 7.9, "high": 8.0}}),
        ],
        watermark=100,
    )
    chk.eq("无父信息进程自身即链根",
           orphan["affected_records"][0]["chain_root"], "ORPH")
    chk.eq("孤儿链独立产生发现", orphan["stats"]["findings_total"], 1)

    chk.note("seed=sample/C04_unknown_conflict.json；风险 {F1,F2,F4}，"
             "未知 {F3,F5}，冲突路径 /d2/conflict.docx 双 file_id")
    return chk


RUNNERS: dict[str, Callable[[], Checker]] = {
    "C01": run_c01,
    "C02": run_c02,
    "C03": run_c03,
    "C04": run_c04,
}


def list_cases() -> int:
    for case_id in sorted(CASES):
        print(f"{case_id}\t{CASES[case_id]['title']}\t{samples_rel(case_id)}")
    return 0


def samples_rel(case_id: str) -> str:
    return str((SAMPLE_DIR / CASES[case_id]["file"]).relative_to(ROOT))


def run_case(case_id: str) -> dict[str, Any]:
    if case_id not in RUNNERS:
        raise KeyError(case_id)
    try:
        checker = RUNNERS[case_id]()
    except Exception as exc:  # 入口缺失/未实现/运行错误都算失败
        return {
            "case": case_id,
            "assertions": 0,
            "passed": 0,
            "failed": 1,
            "skipped": 0,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "sample": samples_rel(case_id),
            "seed": samples_rel(case_id),
            "summary": {"expected": "用例可执行且断言通过", "observed": str(exc)},
        }
    status = "passed" if checker.failed == 0 and checker.total > 0 else "failed"
    return {
        "case": case_id,
        "assertions": checker.total,
        "passed": checker.passed,
        "failed": checker.failed,
        "skipped": 0,
        "status": status,
        "sample": samples_rel(case_id),
        "seed": samples_rel(case_id),
        "failures": [
            {"assertion": f.name, "detail": f.detail} for f in checker.failures
        ],
        "observations": checker.observations,
        "summary": {
            "expected": "全部真实断言通过且断言数 > 0",
            "observed": f"passed={checker.passed} failed={checker.failed} "
                        f"total={checker.total}",
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="文档批量改写检测本地核验")
    parser.add_argument("--list", action="store_true", help="列出核验用例")
    parser.add_argument("--case", choices=sorted(CASES), help="运行指定用例")
    args = parser.parse_args(argv)

    if args.list:
        return list_cases()

    targets = [args.case] if args.case else sorted(CASES)
    results = [run_case(case_id) for case_id in targets]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))

    # 非零退出：任一失败、零断言、整组跳过（本框架不提供跳过能力）。
    for result in results:
        if result["status"] != "passed" or result["assertions"] == 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
