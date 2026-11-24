"""预检引擎：按步骤动作核对每种数据的可经过/可持久化区域。"""

from __future__ import annotations

from .config import PipelineConfig
from .models import (
    ConditionTrigger,
    CrossRegionLanding,
    DataReport,
    Finding,
    PreflightResult,
    TransitRecord,
)


class PreflightEngine:
    """对一份流水线配置执行数据地域预检。"""

    def __init__(self, config: PipelineConfig):
        self.config = config

    # ------------------------------------------------------------------
    def run(self) -> PreflightResult:
        reports: dict[str, DataReport] = {}
        all_findings: list[Finding] = []
        for data_type in self.config.approvals:
            report = self._evaluate_data(data_type)
            reports[data_type] = report
            all_findings.extend(report.findings)
        return PreflightResult(
            name=self.config.name,
            home_region=self.config.home_region,
            data_types=list(self.config.approvals),
            reports=reports,
            findings=all_findings,
        )

    # ------------------------------------------------------------------
    def _evaluate_data(self, data_type: str) -> DataReport:
        cfg = self.config
        approved = set(cfg.approved_regions(data_type))

        transitable: set[str] = set()
        persistable: set[str] = set()
        main_route_regions: set[str] = set()
        transit_records: list[TransitRecord] = []
        landings: list[CrossRegionLanding] = []
        findings: list[Finding] = []
        finding_keys: set[tuple] = set()

        def add_finding(finding: Finding) -> None:
            key = (finding.code, finding.node_id, finding.region)
            if key not in finding_keys:
                finding_keys.add(key)
                findings.append(finding)

        # 先沿主路径（只跟 next，不进 on 分支）收集本区判定所需区域。
        node = cfg.root
        while node is not None:
            if node.applies_to(data_type):
                main_route_regions.add(node.region)
            node = node.next
        main_route_all_home = main_route_regions <= {cfg.home_region}

        def visit(
            current,
            path: list[str],
            triggers: list[ConditionTrigger],
            on_failover: bool,
        ) -> None:
            if not current.applies_to(data_type):
                # 步骤不处理该数据时仍继续向下遍历，数据类型作用域只影响本步。
                pass
            else:
                spec = cfg.action_spec(current.action)
                transitable.add(current.region)

                if not spec.persists:
                    # 只转发的节点另行记录，不参与法务核准判定。
                    transit_records.append(
                        TransitRecord(
                            data_type=data_type,
                            node_id=current.node_id,
                            step=current.step,
                            action=current.action,
                            region=current.region,
                            on_failover=on_failover,
                            path=list(path),
                            trigger_chain=list(triggers),
                        )
                    )
                    if not on_failover and current.region != cfg.home_region:
                        add_finding(
                            Finding(
                                code="MAIN_ROUTE_OUTSIDE_HOME",
                                severity="warning",
                                data_type=data_type,
                                node_id=current.node_id,
                                region=current.region,
                                message=(
                                    f"主路径转发步骤 {current.node_id} 位于 "
                                    f"{current.region}，主路径未全程保持在本区 "
                                    f"{cfg.home_region}"
                                ),
                                path=list(path),
                                trigger_chain=list(triggers),
                            )
                        )
                else:
                    persistable.add(current.region)
                    self._check_persistence(
                        current=current,
                        data_type=data_type,
                        approved=approved,
                        on_failover=on_failover,
                        main_route_all_home=main_route_all_home,
                        path=path,
                        triggers=triggers,
                        landings=landings,
                        add_finding=add_finding,
                    )

            for is_failover, condition, child in current.iter_children():
                child_triggers = triggers
                child_failover = on_failover
                if is_failover:
                    child_triggers = triggers + [
                        ConditionTrigger(current.node_id, condition)
                    ]
                    child_failover = True
                visit(
                    child,
                    path + [child.node_id],
                    child_triggers,
                    child_failover,
                )

        visit(cfg.root, [cfg.root.node_id], [], False)

        transit_only = sorted(transitable - persistable)
        return DataReport(
            data_type=data_type,
            approved_regions=sorted(approved),
            transitable_regions=sorted(transitable),
            persistable_regions=sorted(persistable),
            transit_only_regions=transit_only,
            main_route_regions=sorted(main_route_regions),
            cross_region_landings=landings,
            transit_records=transit_records,
            findings=findings,
        )

    # ------------------------------------------------------------------
    def _check_persistence(
        self,
        *,
        current,
        data_type: str,
        approved: set[str],
        on_failover: bool,
        main_route_all_home: bool,
        path: list[str],
        triggers: list[ConditionTrigger],
        landings: list[CrossRegionLanding],
        add_finding,
    ) -> None:
        cfg = self.config
        region = current.region
        is_approved = region in approved
        outside_home = region != cfg.home_region

        # 法务核准：所有持久化区域必须在该数据类型的核准清单内。
        if not is_approved:
            add_finding(
                Finding(
                    code="UNAPPROVED_PERSISTENCE",
                    severity="error",
                    data_type=data_type,
                    node_id=current.node_id,
                    region=region,
                    message=(
                        f"步骤 {current.node_id}（{current.action}）将 {data_type} "
                        f"持久化到未核准区域 {region}；核准清单：{sorted(approved)}"
                    ),
                    path=list(path),
                    trigger_chain=list(triggers),
                )
            )
        elif outside_home and on_failover:
            # 经核准的异地故障切换落地：允许，但需人工知悉。
            add_finding(
                Finding(
                    code="APPROVED_FAILOVER_LANDING",
                    severity="warning",
                    data_type=data_type,
                    node_id=current.node_id,
                    region=region,
                    message=(
                        f"故障切换分支步骤 {current.node_id} 经核准可在异地 "
                        f"{region} 落地 {data_type}，触发条件："
                        f"{[t.render() for t in triggers]}"
                    ),
                    path=list(path),
                    trigger_chain=list(triggers),
                )
            )
        elif outside_home and not on_failover:
            add_finding(
                Finding(
                    code="MAIN_ROUTE_OUTSIDE_HOME",
                    severity="warning",
                    data_type=data_type,
                    node_id=current.node_id,
                    region=region,
                    message=(
                        f"主路径持久化步骤 {current.node_id} 位于异地 {region}，"
                        f"主路径未全程保持在本区 {cfg.home_region}（已核准）"
                    ),
                    path=list(path),
                    trigger_chain=list(triggers),
                )
            )

        # 题面场景：主路径全程本区，回退分支在其他区域留下落地数据。
        # 返回从数据入口到该副本的完整路径与触发条件链。
        if on_failover and outside_home and main_route_all_home:
            landings.append(
                CrossRegionLanding(
                    data_type=data_type,
                    node_id=current.node_id,
                    step=current.step,
                    action=current.action,
                    region=region,
                    path=list(path),
                    trigger_chain=list(triggers),
                    approved=is_approved,
                )
            )
