"""本地样例生成器：生成虚构实验流程规约与独立真值。

与 collector 严格分离：
- samples/       —— 采集器可读的“公开输入”（rpa-order/v1 JSON）。
- samples/truth/ —— 独立预期真值，仅供 pytest 断言使用；
                    采集器与执行器任何路径都不读取该目录。
全部数据为虚构的化学/生物实验步骤，不涉及真实账户或外部业务服务。
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = PROJECT_ROOT / "samples"
TRUTH_DIR = SAMPLES_DIR / "truth"
INITIAL_DIR = SAMPLES_DIR / "initial"

# 每个场景在流程编辑页上的初始（打乱）顺序——公开输入的一部分
INITIAL_ORDERS: dict[str, list[str]] = {
    "normal-titration": ["S8", "S4", "S1", "S7", "S2", "S6", "S5", "S3"],
    "fixed-conflict": ["A", "C", "B", "D"],
    "cyclic-deps": ["Z", "X", "Y"],
    "manual-swap": ["P5", "P1", "P3", "P2", "P4"],
    "full-acceptance": [
        "F09", "F04", "F01", "F07", "F10", "F03", "F08", "F02", "F06", "F05",
    ],
}


def _write_sample(payload: dict, truth: dict) -> None:
    sid = payload["scenario_id"]
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    (SAMPLES_DIR / f"{sid}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (TRUTH_DIR / f"{sid}.expected.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_samples() -> list[str]:
    """生成全部样例，返回样例 id 列表。幂等：重复生成内容一致。"""
    created: list[str] = []

    # 1) 正常场景：多依赖 + 同组 + 两个固定锚点，存在多个合法排序
    normal = {
        "protocol": "rpa-order/v1",
        "scenario_id": "normal-titration",
        "title": "虚构实验：酸碱滴定流程（正常场景）",
        "source": "local-file://samples/normal-titration.json",
        "steps": [
            {"id": "S1", "name": "穿戴防护装备", "group": "prep"},
            {"id": "S2", "name": "清洗锥形瓶", "group": "prep"},
            {"id": "S3", "name": "量取待测溶液", "group": None},
            {"id": "S4", "name": "滴加指示剂", "group": None},
            {"id": "S5", "name": "校准滴定管", "group": "equip"},
            {"id": "S6", "name": "装入标准液", "group": "equip"},
            {"id": "S7", "name": "滴定并记录读数", "group": None},
            {"id": "S8", "name": "废液回收与整理", "group": None},
        ],
        "constraints": {
            "fixed": {"S1": 0, "S8": 7},
            "depends_on": {
                "S3": ["S2"],
                "S4": ["S3"],
                "S6": ["S5"],
                "S7": ["S4", "S6"],
            },
        },
    }
    normal_truth = {
        "satisfiable": True,
        "expected_target": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"],
        "fixed": {"S1": 0, "S8": 7},
        "multiple_valid_orders": True,
        "initial_shuffled_order": [
            "S8", "S4", "S1", "S7", "S2", "S6", "S5", "S3",
        ],
    }
    _write_sample(normal, normal_truth)
    created.append("normal-titration")

    # 2) 固定步骤冲突：S3 与 S6 都要求槽位 3，且有依赖方向矛盾
    fixed_conflict = {
        "protocol": "rpa-order/v1",
        "scenario_id": "fixed-conflict",
        "title": "虚构实验：固定锚点冲突（边界场景）",
        "source": "local-file://samples/fixed-conflict.json",
        "steps": [
            {"id": "A", "name": "样品登记"},
            {"id": "B", "name": "离心处理"},
            {"id": "C", "name": "光谱测量"},
            {"id": "D", "name": "数据归档"},
        ],
        "constraints": {
            "fixed": {"B": 2, "C": 2},
            "depends_on": {"C": ["B"]},
        },
    }
    _write_sample(fixed_conflict, {
        "satisfiable": False,
        "conflict_type": "fixed",
        "must_mention": ["槽位 2", "B", "C"],
    })
    created.append("fixed-conflict")

    # 3) 循环依赖：X->Y->Z->X
    cyclic = {
        "protocol": "rpa-order/v1",
        "scenario_id": "cyclic-deps",
        "title": "虚构实验：循环依赖（边界场景）",
        "source": "local-file://samples/cyclic-deps.json",
        "steps": [
            {"id": "X", "name": "预热反应釜"},
            {"id": "Y", "name": "注入催化剂"},
            {"id": "Z", "name": "检测回流温度"},
        ],
        "constraints": {
            "depends_on": {"Y": ["X"], "Z": ["Y"], "X": ["Z"]},
        },
    }
    _write_sample(cyclic, {
        "satisfiable": False,
        "conflict_type": "cycle",
        "must_mention": ["X", "Y", "Z", "循环依赖"],
    })
    created.append("cyclic-deps")

    # 4) 执行中手动换位：正常规划，执行第 2 次移动后用户把两张卡片对调
    interference = {
        "protocol": "rpa-order/v1",
        "scenario_id": "manual-swap",
        "title": "虚构实验：执行中手动换位（鲁棒性场景）",
        "source": "local-file://samples/manual-swap.json",
        "steps": [
            {"id": "P1", "name": "配制母液", "group": "g1"},
            {"id": "P2", "name": "梯度稀释", "group": "g1"},
            {"id": "P3", "name": "接种菌株"},
            {"id": "P4", "name": "恒温培养"},
            {"id": "P5", "name": "计数与记录"},
        ],
        "constraints": {
            "fixed": {"P1": 0},
            "depends_on": {"P2": ["P1"], "P4": ["P3"], "P5": ["P4", "P2"]},
        },
        "interference": {"after_moves": 1, "swap_a": "P3", "swap_b": "P5"},
    }
    _write_sample(interference, {
        "satisfiable": True,
        "fixed": {"P1": 0},
        "replan_required": True,
        "initial_shuffled_order": ["P5", "P1", "P3", "P2", "P4"],
    })
    created.append("manual-swap")

    # 5) 完整验收场景：10 步、多组、多固定、多依赖
    full = {
        "protocol": "rpa-order/v1",
        "scenario_id": "full-acceptance",
        "title": "虚构实验：完整验收（10 步综合场景）",
        "source": "local-file://samples/full-acceptance.json",
        "steps": [
            {"id": "F01", "name": "安全检查", "group": "open"},
            {"id": "F02", "name": "开机自检", "group": "open"},
            {"id": "F03", "name": "试剂盘点", "group": "mat"},
            {"id": "F04", "name": "试剂分装", "group": "mat"},
            {"id": "F05", "name": "空白对照"},
            {"id": "F06", "name": "样品加标"},
            {"id": "F07", "name": "色谱进样"},
            {"id": "F08", "name": "质谱采集"},
            {"id": "F09", "name": "图谱解析"},
            {"id": "F10", "name": "出具报告并关机", "group": "close"},
        ],
        "constraints": {
            "fixed": {"F01": 0, "F10": 9},
            "depends_on": {
                "F04": ["F03"],
                "F05": ["F04"],
                "F06": ["F04"],
                "F07": ["F05", "F06"],
                "F08": ["F07"],
                "F09": ["F08"],
            },
        },
        "interference": {"after_moves": 3, "swap_a": "F06", "swap_b": "F09"},
    }
    _write_sample(full, {
        "satisfiable": True,
        "fixed": {"F01": 0, "F10": 9},
        "replan_required": True,
        "initial_shuffled_order": [
            "F09", "F04", "F01", "F07", "F10", "F03", "F08", "F02", "F06", "F05",
        ],
    })
    created.append("full-acceptance")

    # 初始打乱顺序（公开输入；源站据此渲染可拖动卡片）
    INITIAL_DIR.mkdir(parents=True, exist_ok=True)
    for sid, order in INITIAL_ORDERS.items():
        (INITIAL_DIR / f"{sid}.json").write_text(
            json.dumps(order, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return created


if __name__ == "__main__":
    ids = build_samples()
    print("已生成样例：", ", ".join(ids))
