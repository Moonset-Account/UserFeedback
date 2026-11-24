"""发布预检：数据地域与故障切换路径核对组件。"""

from .actions import ActionSpec, DEFAULT_ACTIONS
from .config import PipelineConfig, from_dict, load_config
from .errors import PreflightConfigError
from .models import (
    ConditionTrigger,
    CrossRegionLanding,
    DataReport,
    Finding,
    PreflightResult,
    StepNode,
    TransitRecord,
)
from .preflight import PreflightEngine
from .report import render_text, to_dict

__all__ = [
    "ActionSpec",
    "DEFAULT_ACTIONS",
    "PipelineConfig",
    "StepNode",
    "PreflightEngine",
    "PreflightResult",
    "DataReport",
    "Finding",
    "CrossRegionLanding",
    "TransitRecord",
    "ConditionTrigger",
    "PreflightConfigError",
    "from_dict",
    "load_config",
    "render_text",
    "to_dict",
]
