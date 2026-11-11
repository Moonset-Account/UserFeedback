"""样例生成器：生成 RPA 任务样例与独立真值（期望断言）。

分离原则：
- 本模块只负责把虚构场景写入 data/samples 与 data/ground_truth；
- src/rpa 下的采集/执行代码不导入本模块、不读取 ground_truth 目录
  （由 tests/test_isolation.py 静态扫描强制保证）；
- 真值只供 pytest 断言使用。
"""
from __future__ import annotations

import json
from pathlib import Path

PROTOCOL_ID = "rpa.command-menu.v1"

# 场景清单：id -> (任务文件名, 标题, 分类, 说明)
SCENARIOS = [
    (
        "normal-same-name",
        "task-normal-same-name.json",
        "demo-normal",
        "正常场景：同名“启动”命令按完整路径+作用域唯一命中温控台并验证可见状态",
    ),
    (
        "boundary-disabled",
        "task-boundary-disabled.json",
        "demo-boundary",
        "边界场景：BETA 上下文下真空泵“启动”被禁用，执行器必须停止且不点击",
    ),
    (
        "full-context-switch",
        "task-full-context-switch.json",
        "full",
        "完整验收：切换 ALPHA→BETA 后设备可用性与预设随上下文变化",
    ),
    (
        "full-search-reorder",
        "task-full-search-reorder.json",
        "full",
        "完整验收：同一查询在两种上下文下搜索结果顺序重排",
    ),
    (
        "full-ambiguous",
        "task-full-ambiguous.json",
        "full",
        "完整验收：仅给同名后缀且不给作用域时产生歧义，执行器停止",
    ),
    (
        "full-dynamic-preset",
        "task-full-dynamic-preset.json",
        "full",
        "完整验收：动态子菜单展开后选择预设并校验预设状态",
    ),
    (
        "full-shortcut-conflict",
        "task-full-shortcut-conflict.json",
        "full",
        "完整验收：快照中识别 Ctrl+L 快捷键冲突，RPA 仍只走完整路径",
    ),
]


def _task(name: str, steps: list[dict]) -> dict:
    return {"protocol": PROTOCOL_ID, "name": name, "steps": steps}


def _tasks() -> dict[str, dict]:
    return {
        "task-normal-same-name.json": _task(
            "示例-正常：同名命令的完整路径消歧",
            [
                {"action": "switch_context", "context": "ALPHA"},
                {"action": "snapshot_menu"},
                {
                    "action": "open_path",
                    "menu_path": ["实验", "设备", "温控台", "启动"],
                    "scope": "device",
                },
                {
                    "action": "expect_state",
                    "target": "device:温控台",
                    "equals": "加热中",
                },
                {"action": "wait_visible", "target": "device:温控台", "timeout_ms": 3000},
            ],
        ),
        "task-boundary-disabled.json": _task(
            "示例-边界：禁用目标必须停止",
            [
                {"action": "switch_context", "context": "BETA"},
                {"action": "snapshot_menu"},
                {
                    "action": "open_path",
                    "menu_path": ["实验", "设备", "真空泵", "启动"],
                    "scope": "device",
                },
                {
                    "action": "expect_state",
                    "target": "device:真空泵",
                    "equals": "运行",
                },
            ],
        ),
        "task-full-context-switch.json": _task(
            "完整-上下文切换：状态与可用性随上下文变化",
            [
                {"action": "switch_context", "context": "ALPHA"},
                {
                    "action": "open_path",
                    "menu_path": ["实验", "设备", "温控台", "启动"],
                    "scope": "device",
                },
                {"action": "expect_state", "target": "device:温控台", "equals": "加热中"},
                {"action": "switch_context", "context": "BETA"},
                {"action": "snapshot_menu"},
                {
                    "action": "open_path",
                    "menu_path": ["视图", "刷新布局"],
                    "scope": "view",
                },
                {"action": "expect_state", "target": "context", "equals": "BETA 极限实验"},
            ],
        ),
        "task-full-search-reorder.json": _task(
            "完整-搜索重排：同一查询在两种上下文排序不同",
            [
                {"action": "switch_context", "context": "ALPHA"},
                {"action": "search_commands", "query": "启动", "limit": 5},
                {"action": "switch_context", "context": "BETA"},
                {"action": "search_commands", "query": "启动", "limit": 5},
            ],
        ),
        "task-full-ambiguous.json": _task(
            "完整-歧义：同名后缀无作用域时停止",
            [
                {"action": "switch_context", "context": "ALPHA"},
                {"action": "snapshot_menu"},
                {"action": "open_path", "menu_path": ["启动"]},
            ],
        ),
        "task-full-dynamic-preset.json": _task(
            "完整-动态子菜单：展开后选择 BETA 预设",
            [
                {"action": "switch_context", "context": "BETA"},
                {
                    "action": "open_path",
                    "menu_path": ["实验", "实验预设", "压力循环"],
                    "scope": "preset",
                },
                {"action": "expect_state", "target": "preset", "equals": "BETA-压力"},
            ],
        ),
        "task-full-shortcut-conflict.json": _task(
            "完整-快捷键冲突：冲突可见且路径执行不受影响",
            [
                {"action": "switch_context", "context": "ALPHA"},
                {"action": "snapshot_menu"},
                {
                    "action": "open_path",
                    "menu_path": ["视图", "刷新布局"],
                    "scope": "view",
                },
            ],
        ),
    }


def _ground_truth() -> dict[str, dict]:
    return {
        "task-normal-same-name.json": {
            "expect_status": "success",
            "must_click_node_id": "thermal.start",
            "must_observe": {"device:温控台": "加热中"},
            "decision_status": "unique",
            "forbidden_click_node_ids": ["pump.start", "light.on"],
        },
        "task-boundary-disabled.json": {
            "expect_status": "failed",
            "expect_error_code": "COMMAND_DISABLED",
            "blocked_node_id": "pump.start",
            "must_not_observe": {"device:真空泵": "运行"},
            "no_click_allowed": True,
        },
        "task-full-context-switch.json": {
            "expect_status": "success",
            "must_click_node_ids": ["thermal.start", "view.refresh"],
            "must_observe": {"context": "BETA 极限实验", "device:温控台": "加热中"},
        },
        "task-full-search-reorder.json": {
            "expect_status": "success",
            "search_step_count": 2,
            "alpha_top_id": "thermal.start",
            "beta_top_id": "light.on",
            "orders_must_differ": True,
        },
        "task-full-ambiguous.json": {
            "expect_status": "failed",
            "expect_error_code": "AMBIGUOUS_COMMAND",
            "ambiguous_candidate_ids": ["thermal.start", "pump.start", "light.on"],
            "no_click_allowed": True,
        },
        "task-full-dynamic-preset.json": {
            "expect_status": "success",
            "must_click_node_id": "preset.beta.stress",
            "must_observe": {"preset": "BETA-压力"},
        },
        "task-full-shortcut-conflict.json": {
            "expect_status": "success",
            "must_click_node_id": "view.refresh",
            "snapshot_shortcut_conflict": "Ctrl+L",
        },
    }


def ensure_generated(samples_dir: Path, ground_truth_dir: Path) -> None:
    samples_dir.mkdir(parents=True, exist_ok=True)
    ground_truth_dir.mkdir(parents=True, exist_ok=True)
    tasks = _tasks()
    truths = _ground_truth()
    for filename, document in tasks.items():
        (samples_dir / filename).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    for filename, truth in truths.items():
        out = {
            "task_file": filename,
            "note": "独立真值，仅供 tests 断言；采集器/执行器禁止读取本目录",
            "expectations": truth,
        }
        (ground_truth_dir / filename.replace(".json", ".truth.json")).write_text(
            json.dumps(out, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def list_scenarios() -> list[dict]:
    return [
        {"id": sid, "file": filename, "group": group, "description": desc}
        for sid, filename, group, desc in SCENARIOS
    ]
