"""模拟实验台的菜单注册表（纯虚构数据）。

该模块是“被测系统”的一部分：Flask 通过公开只读接口 /lab/api/snapshot
把菜单结构提供给浏览器页面与 RPA 采集器。它不是真值文件，
真值（期望断言）单独存放于 data/ground_truth，且采集器代码不得导入本目录之外的真值。
"""
from __future__ import annotations

from copy import deepcopy

# 两种实验上下文（虚构）
CONTEXTS = ("ALPHA", "BETA")
CONTEXT_LABELS = {"ALPHA": "ALPHA 标准实验", "BETA": "BETA 极限实验"}

# 设备初始可见状态（虚构）
INITIAL_DEVICE_STATES = {
    "温控台": "待机",
    "真空泵": "停机",
    "光源": "关闭",
}

# 命令执行后的设备可见状态
DEVICE_COMMAND_RESULTS = {
    "thermal.start": ("温控台", "加热中"),
    "thermal.stop": ("温控台", "待机"),
    "pump.start": ("真空泵", "运行"),
    "pump.stop": ("真空泵", "停机"),
    "light.on": ("光源", "开启"),
    "light.off": ("光源", "关闭"),
}

# 动态子菜单：每个上下文可用的实验预设（虚构），切换上下文后内容不同。
DYNAMIC_PRESETS = {
    "ALPHA": [
        {"id": "preset.alpha.baseline", "label": "基线标定", "preset_key": "ALPHA-基线"},
        {"id": "preset.alpha.thermal-ramp", "label": "升温曲线", "preset_key": "ALPHA-升温"},
        {"id": "preset.alpha.steady", "label": "稳态保持", "preset_key": "ALPHA-稳态"},
    ],
    "BETA": [
        {"id": "preset.beta.stress", "label": "压力循环", "preset_key": "BETA-压力"},
        {"id": "preset.beta.thermal-limit", "label": "极限温控", "preset_key": "BETA-极限温"},
    ],
}

# 禁用规则：(上下文, 命令 id) -> 禁用原因
DISABLE_RULES = {
    ("BETA", "pump.start"): "BETA 极限实验下真空泵联锁未解除",
    ("ALPHA", "light.on"): "ALPHA 标准实验中光源由预设自动管理",
    ("BETA", "thermal.stop"): "BETA 极限实验运行中禁止停止温控台",
}

# 搜索排序：上下文亲和度（数值越大越优先），用于演示搜索重排。
# 分值刻意拉开（步长 30），确保亲和度差异能压过标签前缀基础分（100）。
CONTEXT_AFFINITY = {
    "ALPHA": {
        "thermal.start": 200,
        "thermal.stop": 170,
        "preset.alpha.baseline": 140,
        "preset.alpha.thermal-ramp": 110,
        "pump.start": 50,
        "light.on": 20,
    },
    "BETA": {
        "light.on": 200,
        "light.off": 170,
        "preset.beta.stress": 140,
        "preset.beta.thermal-limit": 110,
        "thermal.start": 60,
        "pump.start": 20,
    },
}


def _cmd(node_id, label, scope, *, shortcut=None):
    return {
        "id": node_id,
        "label": label,
        "kind": "command",
        "scope": scope,
        "shortcut": shortcut,
    }


def _group(label, children, *, scope=None):
    return {"id": None, "label": label, "kind": "group", "scope": scope, "children": children}


def _dynamic(label):
    return {"id": "dynamic.presets", "label": label, "kind": "dynamic", "scope": "preset", "children": []}


def build_registry() -> list[dict]:
    """构造静态菜单树（id/path 在 Flask 侧补全，见 _annotate）。"""
    return [
        _group("实验", [
            _group("设备", [
                _group("温控台", [
                    _cmd("thermal.start", "启动", "device", shortcut="Ctrl+T"),
                    _cmd("thermal.stop", "停止", "device"),
                ]),
                _group("真空泵", [
                    # 与 thermal.start 同名“启动”，但作用域与完整路径不同。
                    _cmd("pump.start", "启动", "device", shortcut="Ctrl+P"),
                    _cmd("pump.stop", "停止", "device"),
                ]),
                _group("光源", [
                    _cmd("light.on", "启动", "device", shortcut="Ctrl+L"),
                    _cmd("light.off", "关闭", "device"),
                ]),
            ], scope="device"),
            _dynamic("实验预设"),
        ]),
        _group("视图", [
            _cmd("view.refresh", "刷新布局", "view", shortcut="Ctrl+L"),  # 与 light.on 快捷键冲突
            _cmd("view.fullscreen", "全屏", "view", shortcut="F11"),
            _cmd("view.reset", "重置视图", "view"),
        ], scope="view"),
        _group("帮助", [
            _cmd("help.about", "关于本实验台", "help", shortcut="F1"),
            _cmd("help.docs", "操作手册", "help"),
        ], scope="help"),
    ]


def _annotate(nodes, prefix):
    for node in nodes:
        node["path"] = prefix + [node["label"]]
        if node["id"] is None:
            node["id"] = "group." + ".".join(node["path"])
        if node["kind"] == "dynamic":
            node["dynamic_loaded"] = False
        if "children" in node:
            _annotate(node["children"], node["path"])


def annotated_registry() -> list[dict]:
    registry = build_registry()
    _annotate(registry, [])
    return registry


def shortcut_conflicts() -> list[dict]:
    """返回快捷键冲突清单（页面与采集器均可读）。"""
    registry = annotated_registry()
    by_shortcut: dict[str, list[dict]] = {}

    def walk(nodes):
        for node in nodes:
            if node.get("shortcut"):
                by_shortcut.setdefault(node["shortcut"], []).append(
                    {"id": node["id"], "path": node["path"], "shortcut": node["shortcut"]}
                )
            walk(node.get("children", []))

    walk(registry)
    return [
        {"shortcut": key, "commands": entries}
        for key, entries in sorted(by_shortcut.items())
        if len(entries) > 1
    ]


def snapshot(context: str, device_states: dict[str, str], current_preset: str | None) -> dict:
    """生成完整快照：静态菜单（按上下文标注 enabled）+ 运行状态。"""
    if context not in CONTEXTS:
        raise ValueError(f"未知上下文: {context}")
    registry = deepcopy(annotated_registry())
    disabled_ids = {cmd_id for (ctx, cmd_id) in DISABLE_RULES if ctx == context}

    def mark(nodes):
        for node in nodes:
            if node["kind"] == "command":
                node["enabled"] = node["id"] not in disabled_ids
                node["disabled_reason"] = (
                    DISABLE_RULES.get((context, node["id"])) if not node["enabled"] else None
                )
            if node["kind"] == "dynamic":
                node["dynamic_loaded"] = False
            mark(node.get("children", []))

    mark(registry)
    return {
        "protocol": "lab.snapshot.v1",
        "context": context,
        "context_label": CONTEXT_LABELS[context],
        "menu": registry,
        "shortcuts": shortcut_conflicts(),
        "states": {
            "devices": dict(device_states),
            "current_preset": current_preset,
        },
        "presets": DYNAMIC_PRESETS[context],
        "disable_rules": [
            {"command_id": cmd_id, "reason": reason}
            for (ctx, cmd_id), reason in DISABLE_RULES.items()
            if ctx == context
        ],
        "search_affinity": CONTEXT_AFFINITY[context],
    }
