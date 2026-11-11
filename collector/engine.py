"""采集编排：SVG -> 轴标定 -> 系列提取 -> 带误差范围的观测点。

采集器只接受 SVG 文本或文件路径，不接受、也不读取任何真值数据；
真值由测试侧独立持有，仅用于断言。
"""

from __future__ import annotations

import math
import os

from .axis import AxisCalibration, CalibrationError, calibrate
from .series import RawPoint, extract_series, merge_markers
from .svg_model import SvgDocument
from .transforms import UnsupportedFeature

# 刻度配对/像素量化带来的坐标不确定性（画布像素），
# 作为先验误差用于误差传播，而非拟合出来的“精度”。
PIXEL_UNCERTAINTY = 1.2


def _round_uncertainty(value: float, sigma: float) -> tuple[float, float, int]:
    """误差保留两位有效数字，值按误差的小数位对齐。"""
    if not math.isfinite(sigma) or sigma <= 0:
        return value, 0.0, 6
    magnitude = math.floor(math.log10(sigma))
    decimals = max(0, -magnitude + 1)
    factor = 10 ** decimals
    sigma_r = round(sigma * factor) / factor
    value_r = round(value * factor) / factor
    return value_r, sigma_r, decimals


def _convert_point(pt: RawPoint, cal_x: AxisCalibration | None,
                   cal_y: AxisCalibration | None):
    """单个画布坐标 -> 数据坐标，含误差传播与完整换算说明。"""
    result = {
        "canvas": {"x": round(pt.x, 3), "y": round(pt.y, 3)},
        "kind": pt.kind,
        "source_tag": pt.source_tag,
        "source_id": pt.source_id,
        "segment_index": pt.segment_index,
        "transform_chain": pt.transform_chain,
        "clipped": pt.clipped,
        "clip_detail": pt.clip_detail,
    }
    trace = []
    errors = []
    values = {}

    for name, coord, cal in (
        ("x", cal_x.project(pt.x, pt.y) if cal_x else pt.x, cal_x),
        ("y", cal_y.project(pt.x, pt.y) if cal_y else pt.y, cal_y),
    ):
        if cal is None:
            errors.append(f"{name.upper()} 轴标定失败，该维度无法换算")
            continue
        trace.append({
            "axis": name,
            "raw_canvas": {"x": round(pt.x, 3), "y": round(pt.y, 3)},
            "pixel_coord": round(coord, 3),
            "scale": cal.scale,
            "formula": cal.describe(coord),
        })
        try:
            cal.check_domain(coord)
            if cal.scale == "log":
                logv = cal.k * coord + cal.b
                if logv <= 0:
                    raise CalibrationError(
                        f"{name.upper()} 坐标 {coord:.2f}px 对应 "
                        f"log10(v)={logv:.4g} ≤ 0，真实对数轴不可能出现"
                        "该点（对数轴取值必须为正），拒绝给出数值"
                    )
                value = cal.log_base ** logv
                # 误差传播：σ_log = |k| σ_pix，再按指数传播
                sigma_log = abs(cal.k) * PIXEL_UNCERTAINTY
                sigma = abs(value * math.log(cal.log_base) * sigma_log)
                lo = cal.log_base ** (logv - sigma_log)
                hi = cal.log_base ** (logv + sigma_log)
                v_r, s_r, dec = _round_uncertainty(value, sigma)
                values[name] = {
                    "value": v_r,
                    "uncertainty": s_r,
                    "interval": [round(lo, dec), round(hi, dec)],
                    "scale": "log",
                }
            else:
                value = cal.k * coord + cal.b
                sigma = abs(cal.k) * PIXEL_UNCERTAINTY
                v_r, s_r, dec = _round_uncertainty(value, sigma)
                values[name] = {
                    "value": v_r,
                    "uncertainty": s_r,
                    "interval": [round(value - sigma, dec),
                                 round(value + sigma, dec)],
                    "scale": "linear",
                }
        except CalibrationError as exc:
            errors.append(str(exc))
            trace[-1]["blocked"] = str(exc)

    result["trace"] = trace
    result["values"] = values
    result["errors"] = errors
    result["recoverable"] = not errors and not pt.clipped
    return result


def _axis_payload(cal: AxisCalibration | None, error: str | None):
    if cal is None:
        return {"available": False, "error": error}
    return {
        "available": True,
        "scale": cal.scale,
        "slope_sign": cal.direction,
        "reversed": (cal.orientation == "x" and cal.direction < 0)
        or (cal.orientation == "y" and cal.direction > 0),
        "direction_text": (
            "反向轴：数据值沿画布 X 增大方向减小"
            if cal.orientation == "x" and cal.direction < 0
            else "反向轴：数据值沿画布 Y 增大方向增大（视觉上自下而上减小）"
            if cal.orientation == "y" and cal.direction > 0
            else "数据值沿画布坐标增大方向" + (
                "增大" if cal.orientation == "x" else "减小（视觉上自下而上增大）"
            )
        ),
        "fit_residual": cal.residual,
        "domain_pixels": [cal.domain_min, cal.domain_max],
        "notes": cal.notes,
        "ticks": [
            {
                "canvas_coord": round(t.axis_coord, 3),
                "label": t.label_text,
                "parsed_value": t.value,
                "parse_note": t.parse_note,
            }
            for t in cal.ticks
        ],
        "formula": (
            f"log10(v) = {cal.k:.8g}*px + {cal.b:.8g}"
            if cal.scale == "log"
            else f"v = {cal.k:.8g}*px + {cal.b:.8g}"
        ),
    }


def collect(source: str | bytes, source_name: str = "input.svg",
            file_size: int | None = None) -> dict:
    """执行一次完整采集，返回可 JSON 序列化的结果字典。"""
    result = {
        "source": {
            "name": source_name,
            "size_bytes": file_size,
        },
        "status": "ok",
        "protocol": PROTOCOL_SUBSET,
        "warnings": [],
        "errors": [],
        "axes": {},
        "observations": [],
        "clipped_points": [],
        "breaks": [],
        "skipped_elements": [],
        "summary": {},
    }

    try:
        doc = SvgDocument(source)
    except UnsupportedFeature as exc:
        result["status"] = "error"
        result["errors"].append(f"SVG 解析被协议子集拒绝：{exc}")
        return result
    except Exception as exc:  # XML 语法错误等
        result["status"] = "error"
        result["errors"].append(f"SVG 文档无法解析：{exc}")
        return result

    result["warnings"].extend(doc.warnings)
    nodes = doc.build()
    result["canvas"] = {
        "width": doc.root.get("width"),
        "height": doc.root.get("height"),
        "viewBox": doc.root.get("viewBox"),
    }

    cal_x = cal_y = None
    err_x = err_y = None
    try:
        cal_x = calibrate(nodes, "x", result["warnings"])
    except CalibrationError as exc:
        err_x = str(exc)
        result["errors"].append(f"X 轴：{exc}")
    try:
        cal_y = calibrate(nodes, "y", result["warnings"])
    except CalibrationError as exc:
        err_y = str(exc)
        result["errors"].append(f"Y 轴：{exc}")

    result["axes"] = {
        "x": _axis_payload(cal_x, err_x),
        "y": _axis_payload(cal_y, err_y),
    }

    series_list = merge_markers(extract_series(nodes))
    obs_idx = 0
    for series in series_list:
        for skip in series.skipped:
            entry = {"element": series.source_tag,
                     "id": series.source_id, "reason": skip}
            if entry not in result["skipped_elements"]:
                result["skipped_elements"].append(entry)
        for brk in series.breaks:
            result["breaks"].append({
                "series_id": series.source_id,
                "series_tag": series.source_tag,
                "detail": brk,
            })
        for pt in series.points:
            converted = _convert_point(pt, cal_x, cal_y)
            converted["index"] = obs_idx
            if pt.clipped:
                converted["explanation"] = (
                    f"该点画布坐标 ({pt.x:.1f}, {pt.y:.1f}) 落在裁剪区域外"
                    f"（{pt.clip_detail}），屏幕上不可见，"
                    "仅登记位置，不产生数值观测"
                )
                result["clipped_points"].append(converted)
            else:
                result["observations"].append(converted)
                obs_idx += 1

    blocked = sum(1 for o in result["observations"] if not o["recoverable"])
    result["summary"] = {
        "visible_points": len(result["observations"]),
        "recoverable_points": len(result["observations"]) - blocked,
        "blocked_points": blocked,
        "clipped_points": len(result["clipped_points"]),
        "breaks": len(result["breaks"]),
        "skipped_elements": len(result["skipped_elements"]),
    }
    if result["errors"] or blocked:
        result["status"] = "partial"
    if not result["observations"] and not result["clipped_points"]:
        result["status"] = "error"
        result["errors"].append("未发现任何可识别的数据图形元素")
    return result


def collect_file(path: str) -> dict:
    with open(path, "rb") as fh:
        data = fh.read()
    return collect(
        data.decode("utf-8"),
        source_name=os.path.basename(path),
        file_size=len(data),
    )


PROTOCOL_SUBSET = {
    "name": "SVG 1.1 观测采集子集（本项目声明）",
    "supported": [
        "元素: svg/g/line/polyline/polygon/path(仅M,L,H,V,Z)/circle/ellipse/"
        "rect/text/tspan/defs/clipPath/use",
        "transform: matrix/translate/scale/rotate(含旋转中心)，支持任意嵌套",
        "viewBox: none 与 xMin/xMid/xMax + YMin/YMid/YMid 的 meet/slice",
        "clipPath: clipPathUnits=userSpaceOnUse 且子形状为 rect；"
        "多层 clip-path 按嵌套交集处理",
        "轴: 线性轴、以10为底的对数轴、反向轴；刻度由可见短线+文本标签识别",
        "标签数字: 整数/小数/千分位/科学计数法/百分号/常见单位前后缀(仅剥离)",
        "可见性: display:none、visibility:hidden|collapse、opacity:0 视为隐藏",
    ],
    "unsupported": [
        "曲线/弧线命令 C/S/Q/T/A（遇到即跳过该路径并给出原因）",
        "skewX/skewY 斜切变换、非 px 长度单位（em/pt/% 等）",
        "objectBoundingBox 单位或非矩形 clipPath（相关元素标记不可信并跳过）",
        "10^n 上标、分数、日期等非数值刻度标签（该刻度不参与标定）",
        "CSS 类选择器驱动的外部样式表可见性（仅识别内联 style 与表现属性）",
    ],
}
