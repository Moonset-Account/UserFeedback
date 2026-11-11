"""OAuth 回调配置发布前审计程序。

输入三类记录，每项都带 (颁发方 issuer, 客户端 client_id, 环境 environment, 来源 source):
  1. 客户端注册快照 (Registration)      —— 已注册的完整 HTTPS 回调 URI
  2. 应用回调清单 (CallbackConfig)      —— 应用实际部署的回调配置及匹配方式
  3. 环境例外记录 (ExceptionRecord)     —— 开发环境临时例外，绑定客户端+环境并带到期时间

审计规则:
  * 生产(prod)配置必须使用已注册的完整 HTTPS URI 做精确匹配。
  * 非精确匹配方式(prefix/regex/wildcard 等)一律判为“匹配条件过宽”。
  * 精确匹配但 URI 只在其他环境注册 -> “回调仅在其他环境注册”。
  * 任何环境都查不到注册项 -> “缺少注册证据”。
  * 开发环境未注册项可由绑定到该 (颁发方, 客户端, 环境) 且未到期的例外豁免;
    例外到期即触发告警; 配置补齐(注册项出现)后旧告警自然消失。

绑定键(binding key) = (issuer, client_id, environment)。
同名客户端在不同颁发方下是完全独立的键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Iterable, Optional


PROD = "prod"
EXACT = "exact"


# ---------------------------------------------------------------------------
# 输入记录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Registration:
    """客户端注册快照中的一项: 某颁发方下某客户端在某环境注册的完整回调 URI。"""
    issuer: str
    client_id: str
    environment: str
    redirect_uri: str
    source: str  # 配置来源行, 如 "registrations/prod.json:42"


@dataclass(frozen=True)
class CallbackConfig:
    """应用回调清单中的一项: 部署配置里的回调 URI 及其匹配方式。"""
    issuer: str
    client_id: str
    environment: str
    callback_uri: str
    match_type: str  # "exact" | "prefix" | "regex" | "wildcard" | ...
    source: str


@dataclass(frozen=True)
class ExceptionRecord:
    """环境例外记录: 允许某 (颁发方, 客户端, 环境) 暂时缺少注册证据。

    expires_at 为 None 表示缺少到期时间, 属于无效例外。
    """
    issuer: str
    client_id: str
    environment: str
    expires_at: Optional[date]
    source: str


# ---------------------------------------------------------------------------
# 审计结果
# ---------------------------------------------------------------------------

class Category(str, Enum):
    EXACT_MATCH = "精确匹配"
    OTHER_ENV_ONLY = "回调仅在其他环境注册"
    BROAD_MATCH = "匹配条件过宽"
    NO_REGISTRATION = "缺少注册证据"
    EXCEPTION_COVERED = "例外覆盖(未到期)"
    EXCEPTION_EXPIRED = "开发例外已到期"
    EXCEPTION_INVALID = "例外记录无效"


ALERT_CATEGORIES = frozenset({
    Category.OTHER_ENV_ONLY,
    Category.BROAD_MATCH,
    Category.NO_REGISTRATION,
    Category.EXCEPTION_EXPIRED,
    Category.EXCEPTION_INVALID,
})


@dataclass(frozen=True)
class Finding:
    category: Category
    binding_key: tuple          # (issuer, client_id, environment)
    callback_uri: Optional[str] # 涉及的回调 URI (例外类告警可为 None)
    config_source: str          # 触发该结论的配置来源行
    registrations: tuple        # 相关注册项 (Registration), 可能为空
    detail: str = ""

    @property
    def is_alert(self) -> bool:
        return self.category in ALERT_CATEGORIES


# ---------------------------------------------------------------------------
# 审计核心
# ---------------------------------------------------------------------------

def _key(issuer: str, client_id: str, environment: str) -> tuple:
    return (issuer, client_id, environment)


def audit(registrations: Iterable[Registration],
          callbacks: Iterable[CallbackConfig],
          exceptions: Iterable[ExceptionRecord],
          today: date) -> list[Finding]:
    """对回调清单逐项审计, 返回全部发现(含正常的精确匹配记录)。"""
    registrations = list(registrations)
    exceptions = list(exceptions)

    # 绑定键 -> 该键下已注册的 URI -> 注册项列表
    reg_by_key: dict[tuple, dict[str, list[Registration]]] = {}
    for reg in registrations:
        reg_by_key.setdefault(
            _key(reg.issuer, reg.client_id, reg.environment), {}
        ).setdefault(reg.redirect_uri, []).append(reg)

    # 绑定键 -> 例外记录列表
    exc_by_key: dict[tuple, list[ExceptionRecord]] = {}
    for exc in exceptions:
        exc_by_key.setdefault(
            _key(exc.issuer, exc.client_id, exc.environment), []).append(exc)

    findings: list[Finding] = []

    # --- 1. 例外记录自身的有效性 -------------------------------------------
    for exc in exceptions:
        key = _key(exc.issuer, exc.client_id, exc.environment)
        if exc.expires_at is None:
            findings.append(Finding(
                category=Category.EXCEPTION_INVALID,
                binding_key=key,
                callback_uri=None,
                config_source=exc.source,
                registrations=(),
                detail="例外缺少到期时间, 视为无效, 不能豁免任何回调",
            ))
        elif exc.expires_at < today:
            findings.append(Finding(
                category=Category.EXCEPTION_EXPIRED,
                binding_key=key,
                callback_uri=None,
                config_source=exc.source,
                registrations=(),
                detail=f"例外已于 {exc.expires_at.isoformat()} 到期 "
                       f"(审计日期 {today.isoformat()})",
            ))

    # --- 2. 回调清单逐项判定 ------------------------------------------------
    for cb in callbacks:
        key = _key(cb.issuer, cb.client_id, cb.environment)
        same_key_regs = reg_by_key.get(key, {})

        # 2a. 匹配方式过宽: 生产绝不允许, 其他环境同样报告
        if cb.match_type != EXACT:
            findings.append(Finding(
                category=Category.BROAD_MATCH,
                binding_key=key,
                callback_uri=cb.callback_uri,
                config_source=cb.source,
                registrations=tuple(
                    r for regs in same_key_regs.values() for r in regs),
                detail=f"匹配方式为 {cb.match_type!r}, 生产要求完整 HTTPS URI 精确匹配",
            ))
            continue

        # 2b. 精确匹配且本环境已注册该 URI
        if cb.callback_uri in same_key_regs:
            findings.append(Finding(
                category=Category.EXACT_MATCH,
                binding_key=key,
                callback_uri=cb.callback_uri,
                config_source=cb.source,
                registrations=tuple(same_key_regs[cb.callback_uri]),
                detail="回调 URI 与注册项完全一致",
            ))
            continue

        # 2c. 该 URI 在同颁发方同客户端的其他环境注册过
        other_env_regs = [
            reg for reg in registrations
            if reg.issuer == cb.issuer
            and reg.client_id == cb.client_id
            and reg.environment != cb.environment
            and reg.redirect_uri == cb.callback_uri
        ]
        if other_env_regs:
            findings.append(Finding(
                category=Category.OTHER_ENV_ONLY,
                binding_key=key,
                callback_uri=cb.callback_uri,
                config_source=cb.source,
                registrations=tuple(other_env_regs),
                detail="该 URI 仅在 "
                       + ", ".join(sorted({r.environment for r in other_env_regs}))
                       + " 环境注册, 疑似从其他环境复制配置",
            ))
            continue

        # 2d. 完全无注册证据: 非生产环境可凭未到期例外豁免
        if cb.environment != PROD:
            valid_exc = [
                e for e in exc_by_key.get(key, [])
                if e.expires_at is not None and e.expires_at >= today
            ]
            if valid_exc:
                findings.append(Finding(
                    category=Category.EXCEPTION_COVERED,
                    binding_key=key,
                    callback_uri=cb.callback_uri,
                    config_source=cb.source,
                    registrations=(),
                    detail="无注册证据, 由未到期例外豁免, 例外来源: "
                           + ", ".join(e.source for e in valid_exc),
                ))
                continue

        findings.append(Finding(
            category=Category.NO_REGISTRATION,
            binding_key=key,
            callback_uri=cb.callback_uri,
            config_source=cb.source,
            registrations=(),
            detail="任何环境下均未找到该 URI 的注册项"
                   + ("" if cb.environment == PROD else ", 且无有效例外"),
        ))

    return findings


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render_report(findings: list[Finding]) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("OAuth 回调配置发布前审计报告")
    lines.append("=" * 72)

    order = [
        Category.EXACT_MATCH,
        Category.EXCEPTION_COVERED,
        Category.OTHER_ENV_ONLY,
        Category.BROAD_MATCH,
        Category.NO_REGISTRATION,
        Category.EXCEPTION_EXPIRED,
        Category.EXCEPTION_INVALID,
    ]
    alerts = 0
    for cat in order:
        group = [f for f in findings if f.category == cat]
        if not group:
            continue
        lines.append("")
        lines.append(f"[{cat.value}] 共 {len(group)} 项")
        lines.append("-" * 72)
        for f in group:
            mark = "ALERT" if f.is_alert else "ok"
            if f.is_alert:
                alerts += 1
            issuer, client, env = f.binding_key
            lines.append(f"  ({mark}) 绑定键: issuer={issuer} client={client} env={env}")
            if f.callback_uri is not None:
                lines.append(f"        回调 URI: {f.callback_uri}")
            lines.append(f"        配置来源: {f.config_source}")
            if f.registrations:
                lines.append("        注册项:")
                for reg in f.registrations:
                    lines.append(
                        f"          - {reg.redirect_uri} "
                        f"(env={reg.environment}, 来源 {reg.source})")
            else:
                lines.append("        注册项: (无)")
            lines.append(f"        说明: {f.detail}")

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"合计 {len(findings)} 项发现, 其中告警 {alerts} 项")
    lines.append("发布门禁: " + ("阻断 —— 存在未解决告警" if alerts else "通过"))
    lines.append("=" * 72)
    return "\n".join(lines)
