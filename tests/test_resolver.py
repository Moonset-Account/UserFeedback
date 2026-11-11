"""命令决策解析器单测：同名消歧、禁用停止、动态菜单、作用域过滤。"""
from __future__ import annotations

import pytest

from src.rpa.errors import AmbiguousCommandError, DisabledCommandError, CommandNotFoundError
from src.rpa.menu_model import MenuSnapshot
from src.rpa.resolver import (
    AMBIGUOUS,
    DISABLED,
    DYNAMIC_PENDING,
    NOT_FOUND,
    UNIQUE,
    require_executable,
    resolve_command,
)
from src.webapp import lab_registry


def snap(context="ALPHA"):
    return MenuSnapshot.from_dict(
        lab_registry.snapshot(
            context, dict(lab_registry.INITIAL_DEVICE_STATES), None
        )
    )


def test_same_name_commands_have_distinct_full_paths():
    snapshot = snap()
    ids = {cmd.id for cmd in snapshot.commands() if cmd.label == "启动"}
    assert ids == {"thermal.start", "pump.start", "light.on"}


def test_full_path_with_scope_resolves_unique():
    decision = resolve_command(
        snap(), ["实验", "设备", "温控台", "启动"], scope="device"
    )
    assert decision.status == UNIQUE
    assert decision.node.id == "thermal.start"
    assert "唯一命中" in decision.reason


def test_suffix_path_without_scope_is_ambiguous():
    decision = resolve_command(snap(), ["启动"])
    assert decision.status == AMBIGUOUS
    candidate_ids = {c.id for c in decision.candidates}
    assert candidate_ids == {"thermal.start", "pump.start", "light.on"}
    with pytest.raises(AmbiguousCommandError):
        require_executable(snap(), ["启动"])


def test_suffix_path_with_scope_still_ambiguous_when_three_devices_share_name():
    # 三个“启动”都是 device 作用域，scope 不能消歧 -> 仍必须停止。
    decision = resolve_command(snap(), ["启动"], scope="device")
    assert decision.status == AMBIGUOUS


def test_longer_suffix_disambiguates_without_scope():
    decision = resolve_command(snap(), ["真空泵", "启动"])
    assert decision.status == UNIQUE
    assert decision.node.id == "pump.start"


def test_disabled_command_detected_in_beta():
    decision = resolve_command(
        snap("BETA"), ["实验", "设备", "真空泵", "启动"], scope="device"
    )
    assert decision.status == DISABLED
    assert decision.node.id == "pump.start"
    assert "禁用" in decision.reason
    with pytest.raises(DisabledCommandError):
        require_executable(snap("BETA"), ["实验", "设备", "真空泵", "启动"], "device")


def test_disabled_command_is_context_dependent():
    # 同一命令在 ALPHA 可用。
    decision = resolve_command(
        snap("ALPHA"), ["实验", "设备", "真空泵", "启动"], scope="device"
    )
    assert decision.status == UNIQUE


def test_unknown_path_not_found():
    decision = resolve_command(snap(), ["实验", "设备", "真空泵", "自爆"])
    assert decision.status == NOT_FOUND
    with pytest.raises(CommandNotFoundError):
        require_executable(snap(), ["实验", "设备", "真空泵", "自爆"])


def test_scope_mismatch_is_not_found():
    decision = resolve_command(
        snap(), ["实验", "设备", "温控台", "启动"], scope="view"
    )
    assert decision.status == NOT_FOUND


def test_dynamic_submenu_pending_before_expand():
    decision = resolve_command(snap("BETA"), ["实验", "实验预设", "压力循环"], "preset")
    assert decision.status == DYNAMIC_PENDING
    assert decision.node.id == "dynamic.presets"


def test_dynamic_submenu_resolves_after_loaded():
    snapshot = snap("BETA")
    # 模拟浏览器展开后动态节点加载完成，并挂入预设命令。
    from src.rpa.menu_model import MenuNode

    dynamic = next(
        n for root in snapshot.roots for n in root.all_nodes() if n.id == "dynamic.presets"
    )
    loaded = MenuNode(
        id="preset.beta.stress",
        label="压力循环",
        path=("实验", "实验预设", "压力循环"),
        scope="preset",
        kind="command",
        enabled=True,
        shortcut=None,
        dynamic_loaded=True,
    )
    # 用 dataclass 替换动态节点内容。
    object.__setattr__(dynamic, "dynamic_loaded", True)
    object.__setattr__(dynamic, "children", (loaded,))
    decision = resolve_command(snapshot, ["实验", "实验预设", "压力循环"], "preset")
    assert decision.status == UNIQUE
    assert decision.node.id == "preset.beta.stress"


def test_shortcut_conflicts_are_visible_in_snapshot():
    conflicts = lab_registry.shortcut_conflicts()
    keys = {c["shortcut"] for c in conflicts}
    assert "Ctrl+L" in keys
