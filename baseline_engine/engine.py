"""基线引擎主流程。

处理顺序：

1. 建立服务定义索引与别名映射（``aliases`` 与 ``alias_of`` 两种写法），
   别名链成环或重名时抛出 :class:`BaselineConfigError`；
2. 对每个规范服务按“平台默认值 -> 规范定义 -> 别名定义层 -> 覆盖层”
   叠加出最终配置；
3. 用规范名（含别名）匹配运行快照；
4. 执行启动身份、写入范围、权限继承三项核对并按规则分桶。
"""

from __future__ import annotations

from typing import Any, Mapping

from .checks import run_all_checks
from .merge import merge_layers
from .models import (
    BaselineReport,
    Overlay,
    PlatformDefaults,
    ProcessSnapshot,
    ServiceDef,
    ServiceReport,
    SOURCE_CONFIG,
    SOURCE_RUNTIME,
    STATUS_COMPLIANT,
    STATUS_NO_SNAPSHOT,
    STATUS_NONCOMPLIANT,
    STATUS_REMEDIATE,
    STATUS_SKIPPED,
)


class BaselineConfigError(ValueError):
    """输入配置自相矛盾或引用了不存在的服务。"""


class BaselineEngine:
    def __init__(
        self,
        service_defs: list[ServiceDef],
        overlays: list[Overlay] | None = None,
        platform_defaults: PlatformDefaults | None = None,
        snapshots: list[ProcessSnapshot] | None = None,
    ) -> None:
        self.service_defs = list(service_defs)
        self.overlays = list(overlays or [])
        self.platform_defaults = platform_defaults or PlatformDefaults()
        self.snapshots = list(snapshots or [])
        self._index: dict[str, ServiceDef] = {}
        self._alias_to_canonical: dict[str, str] = {}
        self._build_index()

    # ------------------------------------------------------------------
    # 索引与别名
    # ------------------------------------------------------------------
    def _build_index(self) -> None:
        for svc in self.service_defs:
            if svc.name in self._index:
                raise BaselineConfigError(f"服务定义重名: {svc.name!r}")
            self._index[svc.name] = svc

        # 第一轮：登记显式 aliases 与 alias_of 转发
        for svc in self.service_defs:
            for alias in svc.aliases:
                self._register_alias(alias, svc.name)
            if svc.alias_of is not None:
                if svc.alias_of not in self._index:
                    raise BaselineConfigError(
                        f"服务 {svc.name!r} 的 alias_of 指向不存在的服务: "
                        f"{svc.alias_of!r}"
                    )
                self._register_alias(svc.name, svc.alias_of)

    def _register_alias(self, alias: str, target: str) -> None:
        existing = self._alias_to_canonical.get(alias)
        if existing is not None and existing != target:
            raise BaselineConfigError(
                f"别名 {alias!r} 同时指向 {existing!r} 与 {target!r}"
            )
        if alias in self._index and self._index[alias].alias_of is None:
            raise BaselineConfigError(
                f"别名 {alias!r} 与规范服务定义冲突"
            )
        self._alias_to_canonical[alias] = target

    def resolve_chain(self, name: str) -> list[str]:
        """返回从引用名到规范服务名的完整别名链（含两端），并检测成环。"""
        chain: list[str] = []
        seen: set[str] = set()
        current = name
        while True:
            if current in seen:
                raise BaselineConfigError(
                    f"服务别名链存在循环引用: {' -> '.join(chain + [current])}"
                )
            seen.add(current)
            chain.append(current)
            if current not in self._alias_to_canonical:
                if current not in self._index:
                    raise BaselineConfigError(f"引用了不存在的服务: {name!r}")
                return chain
            current = self._alias_to_canonical[current]

    def canonical(self, name: str) -> str:
        return self.resolve_chain(name)[-1]

    # ------------------------------------------------------------------
    # 配置分层
    # ------------------------------------------------------------------
    def _overlay_groups(self) -> dict[str, list[Overlay]]:
        groups: dict[str, list[Overlay]] = {}
        known_names = set(self._index) | set(self._alias_to_canonical)
        for overlay in self.overlays:
            if overlay.target not in known_names:
                raise BaselineConfigError(
                    f"覆盖层指向不存在的服务: {overlay.target!r}"
                )
            groups.setdefault(overlay.target, []).append(overlay)
        return groups

    def _snapshot_groups(self) -> dict[str, ProcessSnapshot]:
        grouped: dict[str, ProcessSnapshot] = {}
        for snap in self.snapshots:
            if snap.service not in self._index and snap.service not in self._alias_to_canonical:
                raise BaselineConfigError(
                    f"运行快照对应未知服务: {snap.service!r}"
                )
            canonical = self.canonical(snap.service)
            if canonical in grouped:
                raise BaselineConfigError(
                    f"服务 {canonical!r} 存在多个运行快照 "
                    f"({grouped[canonical].service!r}, {snap.service!r})"
                )
            grouped[canonical] = snap
        return grouped

    def _def_chain_names(self, canonical: str) -> list[str]:
        """规范名 + 全部别名定义名，按“规范定义在内层、别名向外覆盖”排序。

        对 a -> b -> collector 这类嵌套别名，b 的定义层必须先于 a 生效。
        别名成环在 :meth:`canonical` 中已被拒绝，这里不会无限递归。
        """
        names = [canonical]
        alias_defs = [
            svc.name
            for svc in self.service_defs
            if svc.alias_of is not None and self.canonical(svc.name) == canonical
        ]
        alias_set = set(alias_defs)
        visited: set[str] = set()
        ordered: list[str] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            target = self._index[name].alias_of
            if target in alias_set:
                visit(target)
            ordered.append(name)

        for name in alias_defs:
            visit(name)
        names.extend(ordered)
        return names

    def _build_layers(
        self, chain_names: list[str]
    ) -> list[Mapping[str, Any]]:
        defaults = self.platform_defaults
        layers: list[Mapping[str, Any]] = [
            {
                "user": defaults.user,
                "group": defaults.group,
                "write_paths": list(defaults.write_paths),
                "forbidden_identities": list(defaults.forbidden_identities),
                "low_privilege_required": defaults.low_privilege_required,
            }
        ]
        # chain_names 为 [规范名, 别名定义...]，从内向外顺序叠加
        for layer_name in chain_names:
            svc = self._index[layer_name]
            layers.append(
                {
                    "user": svc.user,
                    "group": svc.group,
                    "write_paths": svc.write_paths,
                    "forbidden_identities": svc.forbidden_identities,
                    "low_privilege_required": svc.low_privilege_required,
                    "description": svc.description,
                }
            )
        # 覆盖层按输入顺序、按链上任一名字定向生效
        for overlay in self.overlays:
            if overlay.target in chain_names:
                layers.append(
                    {
                        "user": overlay.user,
                        "group": overlay.group,
                        "write_paths": overlay.write_paths,
                        "forbidden_identities": overlay.forbidden_identities,
                        "low_privilege_required": overlay.low_privilege_required,
                        "description": overlay.description or None,
                    }
                )
        return layers

    # ------------------------------------------------------------------
    # 主评估
    # ------------------------------------------------------------------
    def evaluate(self) -> BaselineReport:
        overlay_groups = self._overlay_groups()
        snapshot_groups = self._snapshot_groups()

        # 规范服务按定义首次出现顺序输出；别名服务不单独成报
        canonical_order: list[str] = []
        aliases_by_canonical: dict[str, list[str]] = {}
        for svc in self.service_defs:
            canonical = self.canonical(svc.name)
            if canonical not in aliases_by_canonical:
                canonical_order.append(canonical)
                aliases_by_canonical[canonical] = []
            if svc.alias_of is not None:
                aliases_by_canonical[canonical].append(svc.name)
            for alias in svc.aliases:
                aliases_by_canonical[canonical].append(alias)

        reports: list[ServiceReport] = []
        for canonical in canonical_order:
            chain_names = self._def_chain_names(canonical)
            layers = self._build_layers(chain_names)
            resolved = merge_layers(layers)
            required = bool(resolved["low_privilege_required"])
            resolved_report = dict(resolved)
            resolved_report["layer_chain"] = chain_names

            snapshot = snapshot_groups.get(canonical)
            aliases = sorted(set(aliases_by_canonical.get(canonical, [])))

            if not required:
                reports.append(
                    ServiceReport(
                        name=canonical,
                        canonical_name=canonical,
                        status=STATUS_SKIPPED,
                        low_privilege_required=False,
                        resolved=resolved_report,
                        aliases=aliases,
                        has_snapshot=snapshot is not None,
                        snapshot_service=snapshot.service if snapshot else None,
                    )
                )
                continue

            findings = run_all_checks(
                resolved,
                snapshot.__dict__ if snapshot else None,
                self.platform_defaults.forbidden_identities,
            )

            if snapshot is None:
                status = STATUS_NO_SNAPSHOT
            elif any(f.source == SOURCE_CONFIG for f in findings):
                status = STATUS_NONCOMPLIANT
            elif any(f.source == SOURCE_RUNTIME for f in findings):
                status = STATUS_REMEDIATE
            else:
                status = STATUS_COMPLIANT

            reports.append(
                ServiceReport(
                    name=canonical,
                    canonical_name=canonical,
                    status=status,
                    low_privilege_required=True,
                    resolved=resolved_report,
                    findings=findings,
                    aliases=aliases,
                    has_snapshot=snapshot is not None,
                    snapshot_service=snapshot.service if snapshot else None,
                )
            )

        return BaselineReport(services=reports)


def load_input(raw: Mapping[str, Any]) -> BaselineEngine:
    """从 JSON 风格字典构造引擎。"""
    defaults = PlatformDefaults.from_dict(raw.get("platform_defaults"))
    defs = [ServiceDef.from_dict(item) for item in raw.get("service_defs", [])]
    overlays = [Overlay.from_dict(item) for item in raw.get("overlays", [])]
    snapshots = [
        ProcessSnapshot.from_dict(item) for item in raw.get("snapshots", [])
    ]
    return BaselineEngine(defs, overlays, defaults, snapshots)


def evaluate(raw: Mapping[str, Any]) -> BaselineReport:
    """便捷入口：直接评估 JSON 风格输入。"""
    return load_input(raw).evaluate()
