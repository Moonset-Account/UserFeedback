"""确定性虚构实验数据集（全部为编造内容，不含任何真实账户/业务信息）。

刻意覆盖序列化边界：标签/编号中含逗号、斜杠、中文、空格与保留字符，
并准备空标签、多标签、不同 status/level/score 的条目用于筛选验证。
"""

from __future__ import annotations


def build_experiments() -> list[dict]:
    """返回固定顺序的虚构实验条目（确定性，无随机数）。"""
    rows = [
        # id, name, owner, tags, code, score, level, status, archived, ref
        ("EXP-001", "量子相干性观测", "林之夏", ["光学", "低温"],
         "QX-100", 92, "A", "running", False, "lab/qx/100"),
        ("EXP-002", "湍流模拟,第三轮", "周远航", ["流体", "模拟"],
         "TB-21,7", 78, "B", "running", False, "lab/tb/217"),
        ("EXP-003", "膜蛋白折叠路径", "苏清晏", ["生物", "冷冻电镜"],
         "MB/330", 65, "B", "paused", False, "lab/mb/330"),
        ("EXP-004", "超导体临界电流", "林之夏", ["材料", "低温", "电学"],
         "SC-440", 88, "A", "running", False, "lab/sc/440"),
        ("EXP-005", "暗物质候选体筛查", "霍知行", ["粒子", "天文"],
         "DM-505", 54, "C", "failed", True, "lab/dm/505"),
        ("EXP-006", "钙钛矿稳定性;长测", "沈知微", ["材料", "光伏"],
         "PV-612", 71, "B", "running", False, "lab/pv/612"),
        ("EXP-007", "酶催化动力学(初测)", "苏清晏", ["生物", "化学"],
         "EN-707", 83, "A", "running", False, "lab/en/707"),
        ("EXP-008", "深空通信时延仿真", "周远航", ["通信", "模拟"],
         "DS/820", 47, "C", "failed", False, "lab/ds/820"),
        ("EXP-009", "聚变等离子体约束", "霍知行", ["等离子体", "高温"],
         "FU-901", 95, "A", "running", False, "lab/fu/901"),
        ("EXP-010", "城市热岛遥感", "叶知秋", ["遥感", "气象"],
         "RS-110", 60, "B", "paused", False, "lab/rs/110"),
        ("EXP-011", "气溶胶扩散;边界层", "叶知秋", ["气象", "化学"],
         "AE-118", 58, "C", "paused", True, "lab/ae/118"),
        ("EXP-012", "拓扑绝缘体输运", "林之夏", ["材料", "电学"],
         "TI/126", 90, "A", "running", False, "lab/ti/126"),
        # 含逗号与斜杠的标签，刻意制造序列化编码边界
        ("EXP-013", "微流控芯片,批次/二", "沈知微", ["流体", "芯片,微流", "批次/二"],
         "MF-133", 74, "B", "running", False, "lab/mf/133"),
        ("EXP-014", "电池热失控复现", "沈知微", ["材料", "储能"],
         "BT-141", 39, "C", "failed", True, "lab/bt/141"),
        ("EXP-015", "引力波噪声剔除", "霍知行", ["天文", "信号"],
         "GW/152", 86, "A", "running", False, "lab/gw/152"),
        ("EXP-016", "蛋白质组学比对", "苏清晏", ["生物", "组学"],
         "PO-160", 69, "B", "running", False, "lab/po/160"),
        ("EXP-017", "激光诱导击穿光谱", "林之夏", ["光学", "光谱"],
         "LI-177", 81, "A", "paused", False, "lab/li/177"),
        ("EXP-018", "黑碳气溶胶:城市样", "叶知秋", ["气象", "化学", "城市"],
         "BC-184", 52, "C", "failed", False, "lab/bc/184"),
        ("EXP-019", "钠离子循环测试", "沈知微", ["材料", "储能", "长测"],
         "NA/193", 77, "B", "running", False, "lab/na/193"),
        ("EXP-020", "量子纠错码仿真", "周远航", ["量子", "模拟", "信息"],
         "QE-201", 97, "A", "running", False, "lab/qe/201"),
        ("EXP-021", "珊瑚共生菌培养", "苏清晏", ["生物", "海洋"],
         "CO-215", 63, "B", "paused", True, "lab/co/215"),
        ("EXP-022", "毫米波信道建模", "周远航", ["通信", "信道"],
         "MM/224", 44, "C", "failed", False, "lab/mm/224"),
        ("EXP-023", "高温合金蠕变", "林之夏", ["材料", "高温", "力学"],
         "HA-233", 85, "A", "running", False, "lab/ha/233"),
        ("EXP-024", "固氮酶机理(复核)", "苏清晏", ["生物", "化学"],
         "NF-240", 72, "B", "running", False, "lab/nf/240"),
    ]
    keys = ("id", "name", "owner", "tags", "code", "score",
            "level", "status", "archived", "ref")
    return [dict(zip(keys, row)) for row in rows]


def by_id(experiments: list[dict], exp_id: str) -> dict | None:
    for row in experiments:
        if row["id"] == exp_id:
            return row
    return None
