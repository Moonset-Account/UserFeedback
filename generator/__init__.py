"""样例生成器：虚构仪器源站与样例文件生成（与采集器包分离，仅经公开接口交互）。"""
from .mime_builder import build_mime_entity
from .scenarios import DATASETS, SCENARIO_ORDER, SCENARIO_TITLES_CN, DEMO_SCENARIOS

__all__ = ["build_mime_entity", "DATASETS", "SCENARIO_ORDER", "SCENARIO_TITLES_CN", "DEMO_SCENARIOS"]
