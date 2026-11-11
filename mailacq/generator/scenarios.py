"""五个虚构场景的邮件内容定义。

场景总览（对应验收四类情形 + 一个正常基线）：
1. normal_basic   正常基线：嵌套 mixed + alternative、编码主题、编码文件名
2. dup_message_id 同一 Message-ID 对应两封不同邮件
3. uid_reset      UIDVALIDITY 代际重置后 UID 复用
4. alt_conflict   纯文本与 HTML 替代版本批号不一致
5. bad_attachment 附件声明 base64 但负载损坏（解码错误）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import mime_builder as mb

FROM = "lab-system@example.test"
TO = "lab-reader@example.test"
DATE = "Wed, 11 Nov 2026 09:30:00 +0800"


@dataclass
class SpecMessage:
    uid: int
    message_id: str
    subject: str
    subject_encoding: str  # "B" 或 "Q"
    eml: bytes
    expected_batch: str | None = None
    expected_attachment_numbers: list[float] = field(default_factory=list)
    expected_attachment_error: bool = False
    expected_html_batch: str | None = None  # 与纯文本不同时用于冲突断言


@dataclass
class Generation:
    uidvalidity: int
    messages: list[SpecMessage]


@dataclass
class Scenario:
    key: str
    title: str
    description: str
    generations: list[Generation]
    ground_truth: dict


def _msg(uid: int, message_id: str, subject: str, enc: str, body, extras,
         date_suffix: int = 0) -> mb.MIMEMultipart:
    return mb.build_message(
        subject=subject,
        subject_encoding=enc,
        from_=FROM,
        to=TO,
        date=f"Wed, 11 Nov 2026 09:{30 + date_suffix:02d}:00 +0800",
        message_id=message_id,
        body=body,
        extra_parts=extras,
    )


def _normal_basic() -> Scenario:
    text1 = (
        "实验通知\n实验批号: EXP-2026-0001\n"
        "本批样品已完成初测，请查收附件读数。\n"
    )
    html1 = (
        "<html><body><h2>实验通知</h2>"
        "<p>实验批号：<b>EXP-2026-0001</b></p>"
        "<p>本批样品已完成初测，请查收附件读数。</p></body></html>"
    )
    csv1 = "样品编号,读数\nS001,12.5\nS002,8\nS003,3.14\n"
    m1 = _msg(
        1, "<EXP-2026-0001@lab.example.test>",
        "实验通知 EXP-2026-0001 已发布", "B",
        mb.alternative(text1, html1),
        [mb.csv_attachment("读数-EXP-2026-0001.csv", csv1)],
    )

    text2 = (
        "Notification de l'experience\n"
        "Lot experimental: EXP-2026-0002\n"
        "Mesures en piece jointe.\n"
    )
    html2 = (
        "<html><body><p>Lot experimental: <b>EXP-2026-0002</b></p>"
        "<p>Mesures en piece jointe.</p></body></html>"
    )
    csv2 = "measure,reading\nM01,-4.2\nM02,100\nM03,0.001\n"
    m2 = _msg(
        2, "<EXP-2026-0002@lab.example.test>",
        "Expedition EXP-2026-0002 notifiee", "Q",
        mb.alternative(text2, html2),
        [mb.csv_attachment("mesures-EXP-2026-0002.csv", csv2)],
        date_suffix=1,
    )

    gens = [Generation(1234567890, [
        SpecMessage(1, m1["Message-ID"], m1["Subject"], "B", mb.to_bytes(m1),
                    "EXP-2026-0001", [12.5, 8.0, 3.14]),
        SpecMessage(2, m2["Message-ID"], m2["Subject"], "Q", mb.to_bytes(m2),
                    "EXP-2026-0002", [-4.2, 100.0, 0.001]),
    ])]
    truth = {
        "scenario": "normal_basic",
        "messages": 2,
        "identity_keys": 2,
        "uidvalidity_values": [1234567890],
        "generation_switches": 0,
        "duplicate_message_id_groups": 0,
        "attachment_decode_errors": 0,
        "alternative_conflicts": 0,
        "per_message": {
            "1234567890/1": {"batch": "EXP-2026-0001",
                             "attachment_numbers": [12.5, 8.0, 3.14]},
            "1234567890/2": {"batch": "EXP-2026-0002",
                             "attachment_numbers": [-4.2, 100.0, 0.001]},
        },
        "decoded_subjects": [
            "实验通知 EXP-2026-0001 已发布",
            "Expedition EXP-2026-0002 notifiee",
        ],
    }
    return Scenario(
        "normal_basic", "正常基线（编码主题 / 嵌套 mixed / alternative / 编码文件名）",
        "两封正常实验通知，主题分别使用 Base64 与 Q 编码，正文含纯文本与 HTML 两个替代版本，"
        "附件文件名为编码中文/法文，CSV 内为虚构读数。",
        gens, truth,
    )


def _dup_message_id() -> Scenario:
    mid = "<DUP-MID-0001@lab.example.test>"
    t1 = "实验批号: EXP-2026-0101\n重复 Message-ID 场景第一封。\n"
    h1 = "<html><body>实验批号：<b>EXP-2026-0101</b></body></html>"
    t2 = "实验批号: EXP-2026-0102\n重复 Message-ID 场景第二封，内容不同。\n"
    h2 = "<html><body>实验批号：<b>EXP-2026-0102</b></body></html>"
    m1 = _msg(1, mid, "重复MID通知 EXP-2026-0101", "B",
              mb.alternative(t1, h1), None)
    m2 = _msg(2, mid, "重复MID通知 EXP-2026-0102", "B",
              mb.alternative(t2, h2), None, date_suffix=1)
    gens = [Generation(2222000001, [
        SpecMessage(1, mid, m1["Subject"], "B", mb.to_bytes(m1), "EXP-2026-0101"),
        SpecMessage(2, mid, m2["Subject"], "B", mb.to_bytes(m2), "EXP-2026-0102"),
    ])]
    truth = {
        "scenario": "dup_message_id",
        "messages": 2,
        "identity_keys": 2,
        "uidvalidity_values": [2222000001],
        "generation_switches": 0,
        "duplicate_message_id_groups": 1,
        "duplicate_message_id": mid,
        "attachment_decode_errors": 0,
        "alternative_conflicts": 0,
        "per_message": {
            "2222000001/1": {"batch": "EXP-2026-0101"},
            "2222000001/2": {"batch": "EXP-2026-0102"},
        },
    }
    return Scenario(
        "dup_message_id", "同一 Message-ID 对应两封不同邮件",
        "两封邮件共享 Message-ID " + mid + "，但 UID、主题与批号不同；"
        "采集器必须以 UIDVALIDITY/UID 作为身份键，不得按 Message-ID 合并。",
        gens, truth,
    )


def _uid_reset() -> Scenario:
    t_a = "实验批号: EXP-2026-0201\n代际重置前的旧邮件。\n"
    h_a = "<html><body>实验批号：<b>EXP-2026-0201</b></body></html>"
    t_b = "实验批号: EXP-2026-0202\n代际重置后的新邮件，UID 重新从 1 开始。\n"
    h_b = "<html><body>实验批号：<b>EXP-2026-0202</b></body></html>"
    t_c = "实验批号: EXP-2026-0203\n同代际的第二封邮件。\n"
    h_c = "<html><body>实验批号：<b>EXP-2026-0203</b></body></html>"
    m_a = _msg(1, "<UIDRESET-A@lab.example.test>", "代际A EXP-2026-0201", "B",
               mb.alternative(t_a, h_a), None)
    m_b = _msg(1, "<UIDRESET-B@lab.example.test>", "代际B EXP-2026-0202", "B",
               mb.alternative(t_b, h_b), None)
    m_c = _msg(2, "<UIDRESET-C@lab.example.test>", "代际B EXP-2026-0203", "B",
               mb.alternative(t_c, h_c), None, date_suffix=1)
    gens = [
        Generation(3333000001, [
            SpecMessage(1, m_a["Message-ID"], m_a["Subject"], "B",
                        mb.to_bytes(m_a), "EXP-2026-0201"),
        ]),
        Generation(3333000002, [
            SpecMessage(1, m_b["Message-ID"], m_b["Subject"], "B",
                        mb.to_bytes(m_b), "EXP-2026-0202"),
            SpecMessage(2, m_c["Message-ID"], m_c["Subject"], "B",
                        mb.to_bytes(m_c), "EXP-2026-0203"),
        ]),
    ]
    truth = {
        "scenario": "uid_reset",
        "messages": 3,
        "identity_keys": 3,
        "uidvalidity_values": [3333000001, 3333000002],
        "generation_switches": 1,
        "reused_uid": 1,
        "duplicate_message_id_groups": 0,
        "attachment_decode_errors": 0,
        "alternative_conflicts": 0,
        "per_message": {
            "3333000001/1": {"batch": "EXP-2026-0201"},
            "3333000002/1": {"batch": "EXP-2026-0202"},
            "3333000002/2": {"batch": "EXP-2026-0203"},
        },
    }
    return Scenario(
        "uid_reset", "UIDVALIDITY 代际重置后 UID 复用",
        "同一邮箱两代数据都出现 UID=1，但 UIDVALIDITY 不同、邮件内容不同；"
        "采集器必须识别为两封独立邮件并记录一次代际切换。",
        gens, truth,
    )


def _alt_conflict() -> Scenario:
    text = "实验批号: EXP-2026-0301\n纯文本版本中的批号。\n"
    html = ("<html><body><p>实验批号：<b>EXP-2026-0399</b></p>"
            "<p>HTML 版本中的批号与纯文本不一致。</p></body></html>")
    m = _msg(1, "<ALTCONF-0001@lab.example.test>",
             "替代版本冲突 EXP-2026-0301", "B",
             mb.alternative(text, html), None)
    gens = [Generation(4444000001, [
        SpecMessage(1, m["Message-ID"], m["Subject"], "B", mb.to_bytes(m),
                    expected_batch="EXP-2026-0301",
                    expected_html_batch="EXP-2026-0399"),
    ])]
    truth = {
        "scenario": "alt_conflict",
        "messages": 1,
        "identity_keys": 1,
        "uidvalidity_values": [4444000001],
        "generation_switches": 0,
        "duplicate_message_id_groups": 0,
        "attachment_decode_errors": 0,
        "alternative_conflicts": 1,
        "per_message": {
            "4444000001/1": {
                "selected_batch": "EXP-2026-0301",
                "selected_content_type": "text/plain",
                "html_batch": "EXP-2026-0399",
            },
        },
    }
    return Scenario(
        "alt_conflict", "纯文本与 HTML 内容不一致",
        "multipart/alternative 的两个版本给出不同批号；采集器选用 text/plain 作为提取来源，"
        "同时记录 HTML 版本差异作为冲突警告，两个版本的 MIME part 均保留证据。",
        gens, truth,
    )


def _bad_attachment() -> Scenario:
    text1 = "实验批号: EXP-2026-0401\n附件损坏，但正文仍可解析。\n"
    html1 = "<html><body>实验批号：<b>EXP-2026-0401</b></body></html>"
    broken_csv = "样品,读数\nB01,9.9\nB02,2.2\n"
    m1 = _msg(
        1, "<BADATT-0001@lab.example.test>", "坏附件 EXP-2026-0401", "B",
        mb.alternative(text1, html1),
        [mb.broken_base64_attachment("损坏读数-EXP-2026-0401.csv", broken_csv)],
    )

    text2 = "实验批号: EXP-2026-0402\n附件正常。\n"
    html2 = "<html><body>实验批号：<b>EXP-2026-0402</b></body></html>"
    csv2 = "reading\n7.75\n1.0\n"
    m2 = _msg(
        2, "<BADATT-0002@lab.example.test>", "正常附件 EXP-2026-0402", "B",
        mb.alternative(text2, html2),
        [mb.csv_attachment("正常读数-EXP-2026-0402.csv", csv2)],
        date_suffix=1,
    )
    gens = [Generation(5555000001, [
        SpecMessage(1, m1["Message-ID"], m1["Subject"], "B", mb.to_bytes(m1),
                    "EXP-2026-0401", expected_attachment_error=True),
        SpecMessage(2, m2["Message-ID"], m2["Subject"], "B", mb.to_bytes(m2),
                    "EXP-2026-0402", [7.75, 1.0]),
    ])]
    truth = {
        "scenario": "bad_attachment",
        "messages": 2,
        "identity_keys": 2,
        "uidvalidity_values": [5555000001],
        "generation_switches": 0,
        "duplicate_message_id_groups": 0,
        "attachment_decode_errors": 1,
        "alternative_conflicts": 0,
        "per_message": {
            "5555000001/1": {"batch": "EXP-2026-0401", "attachment_error": True},
            "5555000001/2": {"batch": "EXP-2026-0402",
                             "attachment_numbers": [7.75, 1.0]},
        },
    }
    return Scenario(
        "bad_attachment", "坏附件（base64 声明与负载不符）",
        "第一封邮件附件声明 Content-Transfer-Encoding: base64，实际负载并非合法 base64；"
        "采集器记录解码错误并继续完成正文提取，第二封邮件附件正常，任务整体不中断。",
        gens, truth,
    )


SCENARIO_BUILDERS = {
    "normal_basic": _normal_basic,
    "dup_message_id": _dup_message_id,
    "uid_reset": _uid_reset,
    "alt_conflict": _alt_conflict,
    "bad_attachment": _bad_attachment,
}

# “运行示例”按钮使用的小样例：一个正常 + 一个边界
DEMO_SCENARIOS = ["normal_basic", "bad_attachment"]
ALL_SCENARIOS = list(SCENARIO_BUILDERS.keys())


def build_scenario(key: str) -> Scenario:
    return SCENARIO_BUILDERS[key]()
