"""公开输入协议：本地曲线校准任务 JSON（协议子集声明与校验）。

协议子集
========
本项目“解析协议”仅使用成熟开源生态的 **HTTP/1.1 + JSON** 子集：

* 传输：HTTP/1.1，仅访问本地源站（Flask 内置 WSGI 服务器），
  采集器通过 requests 2.x 发起 ``GET`` 获取任务规格；
* 编码：``Content-Type: application/json; charset=utf-8``，无重定向、
  无鉴权、无压缩、无 Cookie/会话依赖；
* 文档格式：JSON Schema draft-07 子集（object/array/number/string/boolean、
  required、enum、minimum/maximum、const），由本模块手工校验，不引入额外依赖；
* 版本字段 ``schema_version`` 当前固定为 ``"curve-task/v1"``。

采集器只允许经此公开接口、公开文件或浏览器 DOM 交互取得数据；
``data/truth`` 下的独立真值仅供测试断言，采集器不得读取。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "curve-task/v1"
ALLOWED_GROUPS = ("quick", "acceptance")


class TaskValidationError(ValueError):
    """任务规格不符合 curve-task/v1 公开协议。"""


@dataclass(frozen=True)
class Domain:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    x_grid: float
    y_grid: float
    inverted_y: bool
    zoom: float
    pan_x: float
    pan_y: float


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    group: str
    scenario_note: str
    domain: Domain
    initial_points: list[tuple[float, float]]
    target_points: list[tuple[float, float]]
    max_attempts_per_point: int
    tolerance_x: float
    tolerance_y: float


def _require(obj: Any, key: str, typ: type | tuple[type, ...]) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise TaskValidationError(f"缺少必填字段: {key}")
    val = obj[key]
    if not isinstance(val, typ):
        raise TaskValidationError(f"字段 {key} 类型错误，期望 {typ}")
    return val


def _as_point(raw: Any, name: str) -> tuple[float, float]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise TaskValidationError(f"{name} 必须是长度为 2 的 [x, y] 数组")
    x, y = raw
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise TaskValidationError(f"{name} 坐标必须是数值")
    if isinstance(x, bool) or isinstance(y, bool):
        raise TaskValidationError(f"{name} 坐标不能是布尔值")
    return float(x), float(y)


def parse_task(raw: dict[str, Any]) -> Task:
    """按 curve-task/v1 子集校验并解析一份任务规格。"""
    if _require(raw, "schema_version", str) != SCHEMA_VERSION:
        raise TaskValidationError(f"仅支持协议版本 {SCHEMA_VERSION}")
    task_id = _require(raw, "task_id", str)
    title = _require(raw, "title", str)
    group = _require(raw, "group", str)
    if group not in ALLOWED_GROUPS:
        raise TaskValidationError(f"group 必须是 {ALLOWED_GROUPS} 之一")
    note = raw.get("scenario_note", "")
    if not isinstance(note, str):
        raise TaskValidationError("scenario_note 必须是字符串")

    d = _require(raw, "domain", dict)
    domain = Domain(
        x_min=float(_require(d, "x_min", (int, float))),
        x_max=float(_require(d, "x_max", (int, float))),
        y_min=float(_require(d, "y_min", (int, float))),
        y_max=float(_require(d, "y_max", (int, float))),
        x_grid=float(_require(d, "x_grid", (int, float))),
        y_grid=float(_require(d, "y_grid", (int, float))),
        inverted_y=bool(_require(d, "inverted_y", bool)),
        zoom=float(_require(d, "zoom", (int, float))),
        pan_x=float(_require(d, "pan_x", (int, float))),
        pan_y=float(_require(d, "pan_y", (int, float))),
    )
    if not (domain.x_min < domain.x_max and domain.y_min < domain.y_max):
        raise TaskValidationError("轴域最小值必须小于最大值")
    if domain.x_grid <= 0 or domain.y_grid <= 0:
        raise TaskValidationError("网格步长必须为正数")
    if not (0.25 <= domain.zoom <= 4.0):
        raise TaskValidationError("zoom 仅支持 [0.25, 4.0] 区间")

    init_raw = _require(raw, "initial_points", list)
    target_raw = _require(raw, "target_points", list)
    if not (1 <= len(init_raw) == len(target_raw) <= 12):
        raise TaskValidationError("初始点与目标点数量必须一致且在 1..12 之间")
    initial_points = [_as_point(p, f"initial_points[{i}]") for i, p in enumerate(init_raw)]
    target_points = [_as_point(p, f"target_points[{i}]") for i, p in enumerate(target_raw)]

    max_attempts = int(_require(raw, "max_attempts_per_point", int))
    if not 1 <= max_attempts <= 100:
        raise TaskValidationError("max_attempts_per_point 必须在 1..100")
    tol_x = float(_require(raw, "tolerance_x", (int, float)))
    tol_y = float(_require(raw, "tolerance_y", (int, float)))
    if tol_x < 0 or tol_y < 0:
        raise TaskValidationError("容差不能为负数")
    return Task(
        task_id=task_id,
        title=title,
        group=group,
        scenario_note=note,
        domain=domain,
        initial_points=initial_points,
        target_points=target_points,
        max_attempts_per_point=max_attempts,
        tolerance_x=tol_x,
        tolerance_y=tol_y,
    )


def editor_view(task: Task) -> dict[str, Any]:
    """返回只包含曲线编辑器所需字段的公开视图（不含目标点与容差，避免页面泄题）。"""
    d = task.domain
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task.task_id,
        "domain": {
            "x_min": d.x_min,
            "x_max": d.x_max,
            "y_min": d.y_min,
            "y_max": d.y_max,
            "x_grid": d.x_grid,
            "y_grid": d.y_grid,
            "inverted_y": d.inverted_y,
            "zoom": d.zoom,
            "pan_x": d.pan_x,
            "pan_y": d.pan_y,
        },
        "initial_points": [[x, y] for x, y in task.initial_points],
    }
