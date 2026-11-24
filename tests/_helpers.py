"""测试共用的合成配置构造工具。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from region_preflight import PreflightEngine, from_dict  # noqa: E402


def run_config(raw: dict):
    """从字典构建配置并执行预检，返回 PreflightResult。"""
    return PreflightEngine(from_dict(raw)).run()


def base_config(pipeline: dict, *, approvals=None, data_types=None, home="cn-east"):
    """组装一份最小合法配置，默认单数据类型 order、仅核准本区。"""
    data_types = data_types or ["order"]
    approvals = approvals or {"order": [home]}
    return {
        "name": "test-pipeline",
        "home_region": home,
        "data_types": data_types,
        "approvals": approvals,
        "pipeline": pipeline,
    }


def node(node_id, action, *, step=None, region=None, data_types=None,
         next=None, on=None):
    """便捷构造步骤节点字典。"""
    raw: dict = {"id": node_id, "step": step or node_id, "action": action}
    if region is not None:
        raw["region"] = region
    if data_types is not None:
        raw["data_types"] = data_types
    if next is not None:
        raw["next"] = next
    if on is not None:
        raw["on"] = on
    return raw
