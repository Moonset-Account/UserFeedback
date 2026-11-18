"""低权限基线的三项核对。

* 启动身份：配置要求的 user/group 与进程实测身份是否一致；
* 写入范围：进程实际写入路径是否落在配置允许的写入路径之内；
* 权限继承：配置/进程身份是否命中平台禁止的高权限身份。

配置自身缺陷标记为 ``config`` 来源；进程现场与配置不一致标记为
``runtime`` 来源。
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from .models import (
    CHECK_IDENTITY,
    CHECK_INHERITANCE,
    CHECK_WRITE_SCOPE,
    Finding,
    SOURCE_CONFIG,
    SOURCE_RUNTIME,
)

# 平台兜底的高风险身份：即使平台默认值未声明也视为禁止继承
_BUILTIN_HIGH_PRIVILEGE = ("root", "Administrator", "SYSTEM")


def normalize_path(raw: str) -> str:
    """规范化路径用于包含判定（不访问文件系统）。"""
    path = os.path.normpath(raw)
    # 统一末尾分隔符，保证 /var/log 能包含 /var/log/app
    if path != os.sep and path.endswith(os.sep):
        path = path.rstrip(os.sep)
    return path


def path_is_within(child: str, parent: str) -> bool:
    """判断 ``child`` 是否等于或位于 ``parent`` 目录之内。"""
    child_n = normalize_path(child)
    parent_n = normalize_path(parent)
    if child_n == parent_n:
        return True
    prefix = parent_n if parent_n.endswith(os.sep) else parent_n + os.sep
    return child_n.startswith(prefix)


def check_identity(
    resolved: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> list[Finding]:
    """启动身份核对。

    配置层缺陷（未声明启动用户）来自 config；进程身份与配置不符来自 runtime。
    """
    findings: list[Finding] = []
    expected_user = resolved.get("user")
    expected_group = resolved.get("group")

    if not expected_user:
        findings.append(
            Finding(
                check=CHECK_IDENTITY,
                source=SOURCE_CONFIG,
                message="最终配置未声明低权限启动用户，无法约束启动身份",
                severity="high",
                expected="<非空低权限用户>",
                actual=expected_user,
            )
        )

    if snapshot is None:
        return findings

    if expected_user and snapshot.get("user") != expected_user:
        findings.append(
            Finding(
                check=CHECK_IDENTITY,
                source=SOURCE_RUNTIME,
                message=(
                    f"进程实际启动用户 {snapshot.get('user')!r} 与配置要求 "
                    f"{expected_user!r} 不一致"
                ),
                severity="critical",
                expected=expected_user,
                actual=snapshot.get("user"),
                detail={"field": "user"},
            )
        )

    # 快照未上报 group 时视为现场未提供该维度，不做一致性判定
    if (
        expected_group
        and snapshot.get("group") is not None
        and snapshot.get("group") != expected_group
    ):
        findings.append(
            Finding(
                check=CHECK_IDENTITY,
                source=SOURCE_RUNTIME,
                message=(
                    f"进程实际启动组 {snapshot.get('group')!r} 与配置要求 "
                    f"{expected_group!r} 不一致"
                ),
                severity="high",
                expected=expected_group,
                actual=snapshot.get("group"),
                detail={"field": "group"},
            )
        )
    return findings


def check_write_scope(
    resolved: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
) -> list[Finding]:
    """写入范围核对。

    配置未给出任何允许路径属于 config 缺陷；快照中的实际写入路径越界属于
    runtime 缺陷（边界：恰好等于允许路径视为合规）。
    """
    findings: list[Finding] = []
    allowed: list[str] = list(resolved.get("write_paths", []))

    if not allowed:
        findings.append(
            Finding(
                check=CHECK_WRITE_SCOPE,
                source=SOURCE_CONFIG,
                message="最终配置未声明允许写入路径，写入范围不受约束",
                severity="high",
                expected="<非空写入路径白名单>",
                actual=allowed,
            )
        )

    if snapshot is None:
        return findings

    for actual_path in snapshot.get("actual_write_paths", []):
        if not any(path_is_within(actual_path, allowed_p) for allowed_p in allowed):
            findings.append(
                Finding(
                    check=CHECK_WRITE_SCOPE,
                    source=SOURCE_RUNTIME,
                    message=(
                        f"进程实际写入路径 {actual_path!r} 超出配置允许的写入范围"
                    ),
                    severity="critical",
                    expected=list(allowed),
                    actual=actual_path,
                    detail={"violating_path": actual_path},
                )
            )
    return findings


def check_inheritance(
    resolved: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    platform_forbidden: list[str],
) -> list[Finding]:
    """权限继承核对：禁止以高权限身份运行。

    配置用户命中禁止身份 -> config；进程实测身份命中禁止身份 -> runtime。
    """
    findings: list[Finding] = []
    forbidden = set(platform_forbidden) | set(_BUILTIN_HIGH_PRIVILEGE)
    configured_user = resolved.get("user")

    if configured_user and configured_user in forbidden:
        findings.append(
            Finding(
                check=CHECK_INHERITANCE,
                source=SOURCE_CONFIG,
                message=(
                    f"最终配置的启动用户 {configured_user!r} 属于禁止继承的"
                    "高权限身份"
                ),
                severity="critical",
                expected=sorted(forbidden),
                actual=configured_user,
            )
        )

    if snapshot is not None:
        runtime_user = snapshot.get("user")
        if runtime_user and runtime_user in forbidden:
            findings.append(
                Finding(
                    check=CHECK_INHERITANCE,
                    source=SOURCE_RUNTIME,
                    message=(
                        f"进程实测身份 {runtime_user!r} 命中禁止的高权限身份，"
                        "存在权限继承风险"
                    ),
                    severity="critical",
                    expected=sorted(forbidden),
                    actual=runtime_user,
                )
            )
    return findings


def run_all_checks(
    resolved: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    platform_forbidden: list[str],
) -> list[Finding]:
    """执行全部三项核对并汇总不符合项。"""
    return [
        *check_identity(resolved, snapshot),
        *check_write_scope(resolved, snapshot),
        *check_inheritance(resolved, snapshot, platform_forbidden),
    ]
