"""从本地 JSON 文件 / 字典加载操作图与外部证据。

本模块不访问网络；路径由调用方显式传入。所有 JSON 数据都视为不可信输入，
解析失败统一抛出包含路径信息的异常。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .errors import GraphValidationError

PathLike = Union[str, os.PathLike[str]]
GraphSource = Union[Mapping[str, Any], list[Any], PathLike]
EvidenceSource = Union[Mapping[str, Any], PathLike, None]


def _read_json(path: PathLike) -> Any:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise GraphValidationError(f"无法读取 JSON 文件 {p}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GraphValidationError(
            f"JSON 解析失败 {p}: 第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}",
            pos=str(p),
        ) from exc


def load_operation_graph(source: GraphSource) -> Any:
    """加载操作图。

    ``source`` 可以是已经解析好的 mapping / list，也可以是本地 JSON 文件路径。
    支持三种顶层形态：

    1. 单个节点对象（含 ``op``）；
    2. ``{"root": <节点或定义名>, "definitions": {...}}``；
    3. 操作描述数组（等价于一个隐式 ``seq``，由 analyzer 包装）。

    这里只做最外层形态检查；节点级 schema 由 :func:`budget_guard.analyze`
    在遍历中校验，以便给出准确的操作位置。
    """
    if isinstance(source, Mapping):
        graph: Any = dict(source)
    elif isinstance(source, list):
        graph = list(source)
    elif isinstance(source, (str, os.PathLike)):
        graph = _read_json(source)
    else:
        raise GraphValidationError(
            "操作图必须是 dict、list 或 JSON 文件路径，"
            f"得到 {type(source).__name__}"
        )
    if not isinstance(graph, (Mapping, list)):
        raise GraphValidationError(
            f"操作图顶层必须是 JSON 对象或数组，得到 {type(graph).__name__}"
        )
    return graph


def load_evidence(source: EvidenceSource = None) -> dict[str, Any]:
    """加载外部证据。

    接受：

    * ``None`` -> 空证据；
    * mapping -> 其拷贝；
    * JSON 文件路径 -> 解析后的对象（顶层必须是对象）。

    证据条目的具体合法性在分析引用到该键时判定，以便产生带操作位置的 Issue。
    """
    if source is None:
        return {}
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, (str, os.PathLike)):
        data = _read_json(source)
        if not isinstance(data, Mapping):
            raise GraphValidationError("证据文件顶层必须是 JSON 对象")
        return dict(data)
    raise GraphValidationError(
        f"证据必须是 dict、JSON 路径或 None，得到 {type(source).__name__}"
    )


def evidence_limit(entry: Any) -> tuple[Optional[int], Optional[str]]:
    """把证据条目归一化为非负整数上界。

    合法形式：

    * 非负整数（bool 除外）；
    * ``{"limit"|"max"|"upper_bound": <非负整数>, "source"?: ...}``。

    返回 ``(limit, error_message)``；成功时 ``error_message is None``。
    """
    if isinstance(entry, bool):
        return None, "布尔值不能作为整数上界"
    if isinstance(entry, int):
        if entry < 0:
            return None, f"上界不能为负数: {entry}"
        return entry, None
    if isinstance(entry, Mapping):
        for key in ("limit", "max", "upper_bound", "bound"):
            if key in entry:
                return evidence_limit(entry[key])
        return None, "证据对象缺少 limit/max/upper_bound 字段"
    return None, f"不支持的证据取值类型 {type(entry).__name__}"
