"""虚构样例生成器：产出 RIP/1.0 消息流，与采集器保持单向依赖（采集器不依赖本包）。"""
from .scenarios import (
    DEMO_SCENARIO_KEYS,
    SCENARIOS,
    Scenario,
    raw_ndjson_lines,
    write_all_recordings,
    write_recording,
)

__all__ = [
    "SCENARIOS",
    "DEMO_SCENARIO_KEYS",
    "Scenario",
    "raw_ndjson_lines",
    "write_recording",
    "write_all_recordings",
]
