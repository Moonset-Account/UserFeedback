"""本地公开源站的导出链接（令牌）状态机。

链接生命周期（全部在内存中，可重置）：
  active     短时有效，TTL 到期/被下载一次后即失效（一次性链接）
  expired    下发时即标记为已过期（模拟“下载前过期”，确定性、不依赖真实计时）
  used       下发时即标记为已消费（模拟链接已被使用）
  error_html 下载时返回 HTTP 200 + 错误 HTML（状态码正常但内容是错误页）

注意：采集器只能通过 HTTP 观察这些状态，本模块不向采集器开放任何内部接口。
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from generator.scenarios import BASE_VERSION, SCENARIOS, VERSION_STEP

ACTIVE_TTL_SECONDS = 300


@dataclass
class Token:
    token: str
    experiment_id: str
    state: str  # active / expired / used / error_html
    ttl_seconds: int
    payload_path: str
    consumed: bool = False


@dataclass
class ExperimentState:
    issue_count: int = 0
    current_version: str = BASE_VERSION
    tokens: dict[str, Token] = field(default_factory=dict)


class TokenStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.RLock()
        self._experiments: dict[str, ExperimentState] = {}

    def reset(self) -> None:
        with self._lock:
            self._experiments.clear()

    # -- 内部：按场景计划决定下一枚令牌的初始状态 -----------------------------
    def _next_plan_state(self, scenario_id: str, issue_count: int) -> str:
        plan = SCENARIOS[scenario_id].token_plan
        return plan[(issue_count - 1) % len(plan)]

    def issue(self, scenario_id: str) -> tuple[Token, str]:
        """页面请求新链接时调用，返回 (令牌, 当前业务版本)。"""
        with self._lock:
            spec = SCENARIOS[scenario_id]
            exp = self._experiments.setdefault(scenario_id, ExperimentState())
            exp.issue_count += 1

            # 版本变化场景：在第 N 次下发链接时切换业务版本
            if spec.version_bump_on_issue and exp.issue_count >= spec.version_bump_on_issue:
                exp.current_version = VERSION_STEP

            plan_state = self._next_plan_state(scenario_id, exp.issue_count)
            ttl = ACTIVE_TTL_SECONDS if plan_state == "active" else 0
            token = Token(
                token=secrets.token_urlsafe(16),
                experiment_id=scenario_id,
                state=plan_state,
                ttl_seconds=ttl,
                payload_path=str(self._data_dir / "generated" / scenario_id / "payload.zip"),
            )
            exp.tokens[token.token] = token
            return token, exp.current_version

    def get(self, token: str) -> Token | None:
        with self._lock:
            for exp in self._experiments.values():
                found = exp.tokens.get(token)
                if found is not None:
                    return found
            return None

    def consume_if_active(self, token: Token) -> bool:
        """一次性链接：有效下载后立即消费。返回是否本次成功消费。"""
        with self._lock:
            if token.state != "active" or token.consumed:
                return False
            token.consumed = True
            return True

    def current_version(self, scenario_id: str) -> str:
        with self._lock:
            exp = self._experiments.get(scenario_id)
            return exp.current_version if exp else BASE_VERSION

    def stats(self) -> dict:
        """仅供源站管理/测试诊断使用（采集器不访问该接口）。"""
        with self._lock:
            return {
                sid: {
                    "issue_count": exp.issue_count,
                    "current_version": exp.current_version,
                    "tokens": [
                        {"state": t.state, "consumed": t.consumed, "ttl": t.ttl_seconds}
                        for t in exp.tokens.values()
                    ],
                }
                for sid, exp in self._experiments.items()
            }
