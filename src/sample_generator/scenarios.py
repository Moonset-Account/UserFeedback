"""RIP/1.0 虚构样例生成器（与采集器分离的独立组件）。

全部数据均为虚构，不依赖任何真实账户或外部业务服务。
生成器只负责产出协议消息流（HTTP NDJSON / NDJSON 文件），
采集器只能通过规定的公开接口读取，不能 import 本模块取“真值”。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rip_collector.parser import PROTOCOL_VERSION


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    group: str  # demo | acceptance
    delay_seconds: float
    drop_connection: bool
    messages: tuple[dict, ...] = field(default_factory=tuple)

    def lines(self) -> list[str]:
        return [json.dumps(m, ensure_ascii=False) for m in self.messages]


def _initial(data: dict) -> dict:
    return {"type": "initial", "seq": 0, "version": PROTOCOL_VERSION, "data": data}


# ----------------------------------------------------------------------
# 场景 1：正常流（演示用）—— 含子先于父、列表乱序插入、局部错误、重复补丁
# ----------------------------------------------------------------------
def _demo_normal() -> Scenario:
    msgs = [
        _initial({
            "id": "P-1001",
            "title": "虚构材料：纳米多孔电极在低温储能中的行为研究",
            "authors": [],
            "references": [],
        }),
        # 列表插入（先到 index=1 的，再到 index=0，制造乱序）
        {"type": "insert", "seq": 1, "path": "/references", "index": 1,
         "item": {"id": "REF-2", "title": "虚构综述：多孔电极二十年"},
         "item_id": "REF-2"},
        {"type": "insert", "seq": 2, "path": "/references", "index": 0,
         "item": {"id": "REF-1", "title": "虚构专著：电化学界面原理"},
         "item_id": "REF-1"},
        # 子补丁先于父节点：/metrics 尚不存在
        {"type": "patch", "seq": 3, "path": "/metrics/views", "value": 1280},
        # 局部字段错误（虚构上游超时）
        {"type": "error", "seq": 4, "path": "/funding",
         "error": {"code": "UPSTREAM_TIMEOUT", "message": "经费来源上游超时（虚构）"}},
        # 重复补丁：同 seq 再次到达，应被去重
        {"type": "insert", "seq": 2, "path": "/references", "index": 0,
         "item": {"id": "REF-1", "title": "虚构专著：电化学界面原理（重复到达）"},
         "item_id": "REF-1"},
        # 父节点 /metrics 被创建，seq=3 的挂起补丁应自动重放
        {"type": "patch", "seq": 5, "path": "/metrics",
         "value": {"downloads": 342, "keywords": ["低温储能", "纳米多孔"]}},
        # 作者列表追加
        {"type": "insert", "seq": 6, "path": "/authors", "index": 0,
         "item": {"name": "林知微（虚构）", "orcid": "0000-0000-0000-0001"},
         "item_id": "AUTH-1"},
        {"type": "insert", "seq": 7, "path": "/authors", "index": 1,
         "item": {"name": "沈既明（虚构）", "orcid": "0000-0000-0000-0002"},
         "item_id": "AUTH-2"},
        # 错误恢复：funding 字段拿到真实值
        {"type": "patch", "seq": 8, "path": "/funding",
         "value": {"agency": "虚构自然科学基金", "grant": "VCG-2026-0001"}},
        {"type": "end", "seq": 9},
    ]
    return Scenario(
        key="demo_normal",
        title="正常流：乱序插入 + 子先于父 + 错误恢复 + 重复补丁",
        description="完整收到 end；引用列表乱序插入后按 (index, seq) 收敛；"
                    "/metrics/views 早于 /metrics 到达并自动重放；funding 先报错后恢复；"
                    "重复的 seq=2 只生效一次。",
        group="demo", delay_seconds=0.06, drop_connection=False,
        messages=tuple(msgs),
    )


# ----------------------------------------------------------------------
# 场景 2：边界流（演示用）—— 未收到结束标志，连接被源站提前掐断
# ----------------------------------------------------------------------
def _demo_edge() -> Scenario:
    normal = _demo_normal()
    cut = [m for m in normal.messages if m["type"] != "end"]
    # 再留一个永远等不到父节点的挂起补丁，突出“暂存树不完整”
    cut.append({"type": "patch", "seq": 10, "path": "/peer_review/verdict",
                "value": "小修后录用（虚构）"})
    return Scenario(
        key="demo_edge",
        title="边界流：连接中断，未收到结束标志",
        description="源站在发送 end 之前提前关闭连接，且 /peer_review 父节点始终未创建；"
                    "采集器必须标记 interrupted，暂存树不得标成完整。",
        group="demo", delay_seconds=0.06, drop_connection=True,
        messages=tuple(cut),
    )


# ----------------------------------------------------------------------
# 场景 3：列表插入完全乱序 + 同项重复
# ----------------------------------------------------------------------
def _out_of_order() -> Scenario:
    msgs = [
        _initial({"id": "P-2002", "references": [], "tags": []}),
    ]
    refs = [
        ("REF-A", "虚构引文甲", 2),
        ("REF-B", "虚构引文乙", 0),
        ("REF-C", "虚构引文丙", 3),
        ("REF-D", "虚构引文丁", 1),
    ]
    seq = 1
    # 故意按 index 2,0,3,1 的顺序发送
    for item_id, title, idx in refs:
        msgs.append({"type": "insert", "seq": seq, "path": "/references",
                     "index": idx, "item": {"id": item_id, "title": title},
                     "item_id": item_id})
        seq += 1
    # 同一 item_id 再次插入（不同 index、不同 seq），必须去重
    msgs.append({"type": "insert", "seq": seq, "path": "/references",
                 "index": 0, "item": {"id": "REF-B", "title": "虚构引文乙（重复）"},
                 "item_id": "REF-B"})
    seq += 1
    # tags 列表也追加两项，验证多列表互不干扰
    msgs.append({"type": "insert", "seq": seq, "path": "/tags", "index": 0,
                 "item": "开放获取（虚构）", "item_id": "TAG-1"})
    seq += 1
    msgs.append({"type": "insert", "seq": seq, "path": "/tags", "index": 1,
                 "item": "预印本（虚构）", "item_id": "TAG-2"})
    seq += 1
    msgs.append({"type": "end", "seq": seq})
    return Scenario(
        key="out_of_order",
        title="列表乱序插入与位置漂移",
        description="四个引用按 2/0/3/1 的乱序下标插入，另有一次同 item_id 重复插入；"
                    "每次发布都按 (index, seq) 排序并按 item_id 去重重建列表。",
        group="acceptance", delay_seconds=0.03, drop_connection=False,
        messages=tuple(msgs),
    )


# ----------------------------------------------------------------------
# 场景 4：重复补丁（seq 级去重）与重复列表项
# ----------------------------------------------------------------------
def _duplicates() -> Scenario:
    msgs = [
        _initial({"id": "P-3003", "references": [], "metrics": {}}),
        {"type": "patch", "seq": 1, "path": "/metrics",
         "value": {"views": 100}},
        # 完全相同的 seq=1 再发两遍
        {"type": "patch", "seq": 1, "path": "/metrics",
         "value": {"views": 999}},
        {"type": "insert", "seq": 2, "path": "/references", "index": 0,
         "item": {"id": "REF-X", "title": "虚构引文 X"}, "item_id": "REF-X"},
        {"type": "insert", "seq": 3, "path": "/references", "index": 1,
         "item": {"id": "REF-X", "title": "虚构引文 X 换壳重复"},
         "item_id": "REF-X"},
        {"type": "insert", "seq": 4, "path": "/references", "index": 2,
         "item": {"id": "REF-Y", "title": "虚构引文 Y"}, "item_id": "REF-Y"},
        {"type": "end", "seq": 5},
    ]
    return Scenario(
        key="duplicates",
        title="重复补丁与重复列表项",
        description="seq=1 的补丁到达三次（仅首次生效，views 保持 100）；"
                    "REF-X 以不同 seq 插入两次（按 item_id 去重，保留先到者）。",
        group="acceptance", delay_seconds=0.03, drop_connection=False,
        messages=tuple(msgs),
    )


# ----------------------------------------------------------------------
# 场景 5：部分字段出错，含错误恢复与永久错误
# ----------------------------------------------------------------------
def _field_errors() -> Scenario:
    msgs = [
        _initial({"id": "P-4004", "authors": []}),
        {"type": "error", "seq": 1, "path": "/funding",
         "error": {"code": "UPSTREAM_TIMEOUT", "message": "经费服务超时（虚构）"}},
        {"type": "error", "seq": 2, "path": "/license",
         "error": {"code": "NOT_FOUND", "message": "许可证信息缺失（虚构）"}},
        {"type": "insert", "seq": 3, "path": "/authors", "index": 0,
         "item": {"name": "顾长衡（虚构）"}, "item_id": "AUTH-9"},
        # funding 错误恢复
        {"type": "patch", "seq": 4, "path": "/funding",
         "value": {"agency": "虚构科学院", "grant": "VCG-2026-0009"}},
        # license 再报一次不同错误（覆盖旧错误，保留最新来源 seq）
        {"type": "error", "seq": 5, "path": "/license",
         "error": {"code": "FORBIDDEN", "message": "许可证字段无权访问（虚构）"}},
        {"type": "end", "seq": 6},
    ]
    return Scenario(
        key="field_errors",
        title="部分字段出错与错误恢复",
        description="funding 与 license 两个字段独立报错；funding 随后被真实补丁恢复，"
                    "license 保留到结束并携带来源 seq=5；其他字段不受影响。",
        group="acceptance", delay_seconds=0.03, drop_connection=False,
        messages=tuple(msgs),
    )


# ----------------------------------------------------------------------
# 场景 6：非法路径（语法错误、越界、类型不符）
# ----------------------------------------------------------------------
def _illegal_paths() -> Scenario:
    msgs = [
        _initial({"id": "P-5005", "metrics": {"views": 1}, "references": []}),
        # 非根路径但语法错误
        {"type": "patch", "seq": 1, "path": "metrics/views", "value": 2},
        # 数组越界 patch（patch 不能凭空插元素）
        {"type": "patch", "seq": 2, "path": "/references/3",
         "value": {"id": "REF-BAD"}},
        # 向非数组节点执行 insert
        {"type": "insert", "seq": 3, "path": "/metrics", "index": 0,
         "item": {"bad": True}, "item_id": "BAD-1"},
        # 下标不是数字
        {"type": "patch", "seq": 4, "path": "/references/-", "value": 1},
        # 合法补丁穿插其中，证明流不被非法消息打断
        {"type": "patch", "seq": 5, "path": "/metrics/views", "value": 7},
        {"type": "insert", "seq": 6, "path": "/references", "index": 0,
         "item": {"id": "REF-OK", "title": "合法引文（虚构）"},
         "item_id": "REF-OK"},
        # error 路径为空：在解析层即判定形状非法（path 必须以 / 开头）
        {"type": "error", "seq": 7, "path": "",
         "error": {"code": "BAD", "message": "空路径（形状非法）"}},
        {"type": "end", "seq": 8},
    ]
    return Scenario(
        key="illegal_paths",
        title="非法路径与坏消息",
        description="混入语法错误、数组越界、类型不符等 5 条非法消息，均被拒绝并留证，"
                    "合法消息照常生效，流最终正常结束。",
        group="acceptance", delay_seconds=0.03, drop_connection=False,
        messages=tuple(msgs),
    )


# ----------------------------------------------------------------------
# 场景 7：综合压力 —— 乱序 + 重复 + 错误 + 非法 + 坏 JSON + 结束时仍有挂起
# ----------------------------------------------------------------------
def _comprehensive() -> Scenario:
    msgs = [
        _initial({"id": "P-6006", "references": [], "authors": []}),
        # 坏 JSON 行由生成器特殊注入（见 lines 之外的 raw_lines）
        {"type": "insert", "seq": 1, "path": "/references", "index": 1,
         "item": {"id": "REF-2", "title": "虚构综合引文二"}, "item_id": "REF-2"},
        {"type": "insert", "seq": 2, "path": "/references", "index": 0,
         "item": {"id": "REF-1", "title": "虚构综合引文一"}, "item_id": "REF-1"},
        {"type": "insert", "seq": 2, "path": "/references", "index": 0,
         "item": {"id": "REF-1", "title": "重复的综合引文一"}, "item_id": "REF-1"},
        # 子先于父（/dataset 不存在），且到结束都不会创建
        {"type": "patch", "seq": 3, "path": "/dataset/doi",
         "value": "10.0000/fictional-6006"},
        {"type": "error", "seq": 4, "path": "/license",
         "error": {"code": "UPSTREAM_500", "message": "许可证上游 500（虚构）"}},
        {"type": "patch", "seq": 5, "path": "references", "value": "非法路径"},
        {"type": "insert", "seq": 6, "path": "/authors", "index": 0,
         "item": {"name": "裴明烛（虚构）"}, "item_id": "AUTH-C"},
        # 第二个挂起项：/peer_review 同样不会创建
        {"type": "error", "seq": 7, "path": "/peer_review/decision",
         "error": {"code": "PENDING", "message": "评审未返回（虚构）"}},
        {"type": "end", "seq": 8},
    ]
    scenario = Scenario(
        key="comprehensive",
        title="综合场景：乱序/重复/错误/非法/坏行/结束未决",
        description="一条坏 JSON 行、一条非法路径、一条重复插入、两条永远等不到父节点的"
                    "挂起消息；收到 end 但仍有未解析项，状态为 ended_unresolved，"
                    "暂存树不标记完整。",
        group="acceptance", delay_seconds=0.03, drop_connection=False,
        messages=tuple(msgs),
    )
    return scenario


def raw_ndjson_lines(scenario: Scenario) -> list[str]:
    """综合场景在 seq=4 与 seq=5 之间插入一行坏 JSON。"""
    lines = scenario.lines()
    if scenario.key == "comprehensive":
        # 找到 seq=5 那一行的位置，在其前面插坏行
        for idx, line in enumerate(lines):
            if '"seq": 5' in line:
                lines.insert(idx, '{"type": "patch", "seq": 90, broken json here')
                break
    return lines


SCENARIOS: dict[str, Scenario] = {
    s.key: s for s in (
        _demo_normal(),
        _demo_edge(),
        _out_of_order(),
        _duplicates(),
        _field_errors(),
        _illegal_paths(),
        _comprehensive(),
    )
}

DEMO_SCENARIO_KEYS = ["demo_normal", "demo_edge"]


def write_recording(scenario: Scenario, directory: str | Path) -> Path:
    """把场景写成 NDJSON 文件（文件传输的公开输入）。"""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{scenario.key}.ndjson"
    path.write_text("\n".join(raw_ndjson_lines(scenario)) + "\n", encoding="utf-8")
    return path


def write_all_recordings(directory: str | Path) -> dict[str, Path]:
    return {key: write_recording(s, directory) for key, s in SCENARIOS.items()}
