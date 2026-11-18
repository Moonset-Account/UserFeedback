"""生效选项解析。

解析优先级（题面：“配置显式值优先于该版本默认值”）：

1. 端点自身的配置层设置：先比配置层 priority（大者优先），
   同层再比作用域精确度（精确版本/精确消息类型优先于通配），
   仍并列时以条目在配置中出现的先后为准（后者覆盖前者）；
2. 该协议版本的默认值 version_defaults；
3. 内建兜底 BUILTIN_DEFAULTS —— 此时 defined=False，表示“选项未定义”。

布尔选项若由配置层显式给出 false，explicitly_disabled=True，
与走默认/兜底得到的 false 在结论中分开归因。
"""

from __future__ import annotations

from typing import Optional

from .model import (
    BUILTIN_DEFAULTS,
    AuditConfig,
    Endpoint,
    PolicySource,
    ResolvedOption,
    Setting,
)


def _candidate_rank(
    setting: Setting, layer_priority: int, version: str, message_type: str
) -> tuple[int, int, int]:
    version_exact = 1 if setting.version == version else 0
    mt_exact = 1 if setting.message_type == message_type else 0
    return layer_priority, version_exact + mt_exact, setting.index


def _matching_settings(
    endpoint: Endpoint, version: str, message_type: str, layer_priority: dict[str, int]
) -> list[Setting]:
    matched = [
        s
        for s in endpoint.settings
        if (s.version is None or s.version == version)
        and (s.message_type is None or s.message_type == message_type)
    ]
    matched.sort(
        key=lambda s: _candidate_rank(s, layer_priority[s.layer], version, message_type)
    )
    return matched


def resolve_option(
    config: AuditConfig,
    endpoint: Endpoint,
    version: str,
    message_type: str,
    option: str,
) -> ResolvedOption:
    layer_priority = {l.name: l.priority for l in config.layers}
    # 已按 (层优先级, 作用域精确度, 条目顺序) 升序；从后往前取第一个显式定义
    for setting in reversed(
        _matching_settings(endpoint, version, message_type, layer_priority)
    ):
        if option in setting.values:
            value = setting.values[option]
            return ResolvedOption(
                option=option,
                value=value,
                defined=True,
                explicitly_disabled=(value is False),
                source=PolicySource(
                    source_type="layer",
                    layer=setting.layer,
                    layer_priority=layer_priority[setting.layer],
                    version_scope=setting.version,
                    message_type_scope=setting.message_type,
                    setting_index=setting.index,
                ),
            )

    version_defaults = config.version_defaults.get(version, {})
    if option in version_defaults:
        return ResolvedOption(
            option=option,
            value=version_defaults[option],
            defined=True,
            explicitly_disabled=False,
            source=PolicySource(source_type="version_default", version_scope=version),
        )

    return ResolvedOption(
        option=option,
        value=BUILTIN_DEFAULTS[option],
        defined=False,
        explicitly_disabled=False,
        source=PolicySource(source_type="builtin_default", version_scope=version),
    )


def resolve_endpoint_options(
    config: AuditConfig, endpoint: Endpoint, version: str, message_type: str
) -> dict[str, ResolvedOption]:
    return {
        option: resolve_option(config, endpoint, version, message_type, option)
        for option in BUILTIN_DEFAULTS
    }
