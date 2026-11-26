"""测试公共辅助：合成时间轴与引擎配置。"""

from datetime import datetime, timedelta

from recovery_auth import AuthorizationEngine, EngineConfig, Ledger

BASE = datetime(2026, 1, 1, 8, 0, 0)


def t(hours=0, minutes=0):
    """以 BASE 为起点的合成时刻。"""
    return BASE + timedelta(hours=hours, minutes=minutes)


# 许可待生效期 2 小时，生效后有效 24 小时
CONFIG = EngineConfig(
    pending_period=timedelta(hours=2),
    validity_period=timedelta(hours=24),
)


def make_engine(events):
    ledger = Ledger(events)
    return AuthorizationEngine(ledger, CONFIG)
