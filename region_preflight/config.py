"""配置解析与校验。

配置采用嵌套步骤树（JSON/Python dict 均可），结构见 README。
解析阶段完成：

* 动作注册表合并与未知动作拦截；
* 节点 ID 唯一性、引用结构、数据类型声明、故障切换条件等校验；
* 地域继承——未显式写 ``region`` 的子配置按父配置判定，根节点缺省
  取顶层 ``home_region``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .actions import DEFAULT_ACTIONS, ActionSpec
from .errors import PreflightConfigError
from .models import PipelineConfig, StepNode


def _build_actions(raw: Any) -> dict[str, ActionSpec]:
    actions = dict(DEFAULT_ACTIONS)
    if raw is None:
        return actions
    if not isinstance(raw, dict):
        raise PreflightConfigError("顶层 actions 必须是对象/字典")
    for name, spec in raw.items():
        if not isinstance(name, str) or not name:
            raise PreflightConfigError("动作名必须是非空字符串")
        if not isinstance(spec, dict) or "persists" not in spec:
            raise PreflightConfigError(
                f"动作 {name} 的配置必须是包含 persists 布尔值的对象"
            )
        persists = spec["persists"]
        if not isinstance(persists, bool):
            raise PreflightConfigError(f"动作 {name} 的 persists 必须是布尔值")
        description = spec.get("description", "")
        if not isinstance(description, str):
            raise PreflightConfigError(f"动作 {name} 的 description 必须是字符串")
        actions[name] = ActionSpec(name, persists, description)
    return actions


def _build_node(
    raw: Any,
    *,
    actions: dict[str, ActionSpec],
    known_data_types: set[str],
    seen_ids: set[str],
    parent_region: str | None,
    parent_id: str | None,
    path_hint: str,
) -> StepNode:
    if not isinstance(raw, dict):
        raise PreflightConfigError(f"{path_hint}：节点必须是对象")

    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise PreflightConfigError(f"{path_hint}：缺少非空字符串字段 id")
    if node_id in seen_ids:
        raise PreflightConfigError(f"节点 id 重复：{node_id}")
    seen_ids.add(node_id)

    step = raw.get("step", node_id)
    if not isinstance(step, str) or not step:
        raise PreflightConfigError(f"节点 {node_id}：step 必须是非空字符串")

    action = raw.get("action")
    if not isinstance(action, str) or not action:
        raise PreflightConfigError(f"节点 {node_id}：缺少非空字符串字段 action")
    if action not in actions:
        raise PreflightConfigError(
            f"节点 {node_id}：未知动作 {action}，请在顶层 actions 中声明"
        )

    # 地域继承：未显式声明 region 的子配置按父配置判定。
    explicit_region = raw.get("region")
    if explicit_region is None:
        if parent_region is None:
            raise PreflightConfigError(
                f"节点 {node_id}：根节点必须显式声明 region 或由 home_region 兜底"
            )
        region = parent_region
        inherited = True
    else:
        if not isinstance(explicit_region, str) or not explicit_region:
            raise PreflightConfigError(f"节点 {node_id}：region 必须是非空字符串")
        region = explicit_region
        inherited = False

    data_types = raw.get("data_types", [])
    if not isinstance(data_types, list) or not all(
        isinstance(d, str) and d for d in data_types
    ):
        raise PreflightConfigError(
            f"节点 {node_id}：data_types 必须是非空字符串组成的列表"
        )
    unknown = [d for d in data_types if d not in known_data_types]
    if unknown:
        raise PreflightConfigError(
            f"节点 {node_id}：data_types 含未在 data_types 顶层声明的类型 {unknown}"
        )

    failovers_raw = raw.get("on", {})
    if not isinstance(failovers_raw, dict):
        raise PreflightConfigError(f"节点 {node_id}：on 必须是条件到子树的映射对象")
    failovers: dict[str, StepNode] = {}
    for condition, child_raw in failovers_raw.items():
        if not isinstance(condition, str) or not condition:
            raise PreflightConfigError(
                f"节点 {node_id}：故障切换条件必须是非空字符串"
            )
        failovers[condition] = _build_node(
            child_raw,
            actions=actions,
            known_data_types=known_data_types,
            seen_ids=seen_ids,
            parent_region=region,
            parent_id=node_id,
            path_hint=f"{path_hint}.on[{condition}]",
        )

    next_node: StepNode | None = None
    if "next" in raw and raw["next"] is not None:
        next_node = _build_node(
            raw["next"],
            actions=actions,
            known_data_types=known_data_types,
            seen_ids=seen_ids,
            parent_region=region,
            parent_id=node_id,
            path_hint=f"{path_hint}.next",
        )

    return StepNode(
        node_id=node_id,
        step=step,
        action=action,
        region=region,
        data_types=list(data_types),
        next=next_node,
        failovers=failovers,
        region_inherited=inherited,
        parent_id=parent_id,
    )


def from_dict(raw: Any) -> PipelineConfig:
    """从普通字典构建并校验一份流水线配置。"""
    if not isinstance(raw, dict):
        raise PreflightConfigError("配置根必须是对象/字典")

    name = raw.get("name", "pipeline")
    if not isinstance(name, str) or not name:
        raise PreflightConfigError("顶层 name 必须是非空字符串")

    home_region = raw.get("home_region")
    if not isinstance(home_region, str) or not home_region:
        raise PreflightConfigError("顶层 home_region 必须是非空字符串")

    data_types = raw.get("data_types", [])
    if (
        not isinstance(data_types, list)
        or not data_types
        or not all(isinstance(d, str) and d for d in data_types)
    ):
        raise PreflightConfigError(
            "顶层 data_types 必须是非空字符串组成的非空列表"
        )
    if len(set(data_types)) != len(data_types):
        raise PreflightConfigError("顶层 data_types 存在重复条目")

    approvals_raw = raw.get("approvals", {})
    if not isinstance(approvals_raw, dict):
        raise PreflightConfigError("顶层 approvals 必须是数据类型到区域列表的映射")
    approvals: dict[str, list[str]] = {}
    for dtype in data_types:
        regions = approvals_raw.get(dtype)
        if not isinstance(regions, list) or not regions or not all(
            isinstance(r, str) and r for r in regions
        ):
            raise PreflightConfigError(
                f"approvals.{dtype} 必须是非空字符串组成的列表（法务核准区域）"
            )
        if len(set(regions)) != len(regions):
            raise PreflightConfigError(f"approvals.{dtype} 存在重复区域")
        approvals[dtype] = list(regions)

    actions = _build_actions(raw.get("actions"))

    root_raw = raw.get("pipeline")
    if not isinstance(root_raw, dict):
        raise PreflightConfigError("顶层 pipeline 必须是步骤树的根节点对象")

    # 根节点未显式写 region 时，按 home_region 兜底（等价于继承顶层默认）。
    if "region" not in root_raw or root_raw.get("region") is None:
        root_raw = {**root_raw, "region": home_region}

    root = _build_node(
        root_raw,
        actions=actions,
        known_data_types=set(data_types),
        seen_ids=set(),
        parent_region=None,
        parent_id=None,
        path_hint="pipeline",
    )

    return PipelineConfig(
        name=name,
        home_region=home_region,
        approvals=approvals,
        root=root,
        actions=actions,
    )


def load_config(path: str | Path) -> PipelineConfig:
    """从 JSON 文件加载配置。文件内容视为不可信输入，仅做结构解析。"""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return from_dict(raw)
