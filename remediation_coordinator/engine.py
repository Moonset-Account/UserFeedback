"""修复动作协调引擎。

职责边界（按题目要求）：
  * 只推导动作占位/拒绝/派发/结算，不执行任何真实修复；
  * 规划后、开始前重新核对前置条件，过期动作一律拒绝；
  * 只感知相关资源/证据变化，无关变化不影响其他链；
  * 全链路依赖形成的间接冲突，不允许两个链同时占位；
  * 不可撤销阶段的在途动作继续等结果，其后续动作按最新事实重排；
  * 执行器回执必须带代次，过期代次的回执只记账、不改状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .executor import DispatchOrder, Executor
from .model import (
    Action,
    ActionSpec,
    ActionState,
    ChainRecord,
    ChainStatus,
    EventType,
    OCCUPYING_STATES,
    Receipt,
    TERMINAL_STATES,
    WorldState,
)

SNAPSHOT_FORMAT = 1


@dataclass(frozen=True)
class RecoveryBuckets:
    """重启后对在途动作的三分类（另附不可撤销在途桶便于审计）。"""

    pending_start: tuple[str, ...]            # 待开始：planned / scheduled
    effective_unconfirmed: tuple[str, ...]    # 已生效但未确认：effective
    cancellable: tuple[str, ...]              # 尚可取消：dispatched / cancelling
    irrevocable_in_flight: tuple[str, ...]    # 已过不可撤销点，只能等结果

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_start": list(self.pending_start),
            "effective_unconfirmed": list(self.effective_unconfirmed),
            "cancellable": list(self.cancellable),
            "irrevocable_in_flight": list(self.irrevocable_in_flight),
        }


class RemediationCoordinator:
    def __init__(
        self,
        world: WorldState,
        executor: Executor | None = None,
        *,
        clock: int = 0,
    ) -> None:
        self.world = world
        self.executor = executor
        self._clock = clock
        self._seq = 0
        self._next_generation = 0
        self._chain_order = 0
        self.chains: dict[str, ChainRecord] = {}
        self.actions: dict[str, Action] = {}
        self.receipts: list[Receipt] = []

    # ------------------------------------------------------------------
    # 回执账本
    # ------------------------------------------------------------------

    def _emit(
        self,
        event: EventType,
        *,
        chain_id: str | None = None,
        action_id: str | None = None,
        generation: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Receipt:
        self._seq += 1
        self._clock += 1
        receipt = Receipt(
            seq=self._seq,
            timestamp=self._clock,
            event=event.value,
            chain_id=chain_id,
            action_id=action_id,
            generation=generation,
            details=dict(details or {}),
        )
        self.receipts.append(receipt)
        return receipt

    def receipts_for(self, action_id: str) -> tuple[Receipt, ...]:
        return tuple(r for r in self.receipts if r.action_id == action_id)

    def receipts_for_chain(self, chain_id: str) -> tuple[Receipt, ...]:
        return tuple(r for r in self.receipts if r.chain_id == chain_id)

    def _last_receipt(self, action_id: str) -> Receipt | None:
        for receipt in reversed(self.receipts):
            if receipt.action_id == action_id:
                return receipt
        return None

    # ------------------------------------------------------------------
    # 规划：提交处置链
    # ------------------------------------------------------------------

    def submit_chain(self, chain_id: str, specs: Sequence[ActionSpec]) -> None:
        if chain_id in self.chains:
            raise ValueError(f"处置链 {chain_id} 已存在")
        if not specs:
            raise ValueError(f"处置链 {chain_id} 至少需要一个动作")
        ordered = sorted(specs, key=lambda s: s.step)
        for index, spec in enumerate(ordered):
            if spec.chain_id != chain_id:
                raise ValueError(
                    f"动作 {spec.action_id} 归属链 {spec.chain_id}，"
                    f"与提交链 {chain_id} 不一致"
                )
            if spec.step != index:
                raise ValueError(
                    f"处置链 {chain_id} 的步骤必须从 0 连续编号，"
                    f"动作 {spec.action_id} 的 step={spec.step} 不符合"
                )
            if spec.action_id in self.actions:
                raise ValueError(f"动作 id 重复: {spec.action_id}")

        record = ChainRecord(chain_id=chain_id, order=self._chain_order)
        self._chain_order += 1
        self.chains[chain_id] = record
        for spec in ordered:
            self.actions[spec.action_id] = Action(spec=spec, state=ActionState.PLANNED)
            self._emit(
                EventType.PLANNED,
                chain_id=chain_id,
                action_id=spec.action_id,
                details={"step": spec.step},
            )
        self.pump()

    # ------------------------------------------------------------------
    # 调度主循环
    # ------------------------------------------------------------------

    def _chain_actions(self, chain_id: str) -> list[Action]:
        return sorted(
            (a for a in self.actions.values() if a.chain_id == chain_id),
            key=lambda a: a.spec.step,
        )

    def _head_action(self, chain_id: str) -> Action | None:
        for action in self._chain_actions(chain_id):
            if not action.is_terminal:
                return action
        return None

    def pump(self) -> None:
        """推进所有可推进的链，直到一轮扫描没有任何状态变化。

        在途（已派发未结算）的头部会挡住同链后续动作，但不影响其他链；
        一旦头部进入终态，下一轮扫描立即接棒后续动作。
        """
        progressed = True
        while progressed:
            progressed = False
            for record in sorted(self.chains.values(), key=lambda c: c.order):
                if record.status != ChainStatus.ACTIVE:
                    continue
                head = self._head_action(record.chain_id)
                if head is None:
                    self._complete_chain(record)
                    progressed = True
                    continue
                if head.state in (ActionState.PLANNED, ActionState.SCHEDULED):
                    # 每轮扫描先重新核对最新事实：即使动作仍被冲突挡住，
                    # 只要资源版本/证据已经变化，过期动作立即拒绝而不是继续等
                    ok, reason = self._evaluate_preconditions(head)
                    if not ok:
                        self._abort_chain(
                            head.chain_id, reason or "前置条件过期", head
                        )
                        progressed = True
                        continue
                if head.state == ActionState.PLANNED:
                    # 一次扫描内同一动作至多推进一次，避免同轮反复派发
                    progressed = self._try_promote(head) or progressed
                elif head.state == ActionState.SCHEDULED:
                    progressed = self._try_dispatch(head) or progressed

    def _evaluate_preconditions(self, action: Action) -> tuple[bool, str | None]:
        for precondition in action.spec.preconditions:
            if not precondition.evaluate(self.world):
                return False, f"前置条件不再成立: {precondition!r}"
        return True, None

    def _try_promote(self, action: Action) -> bool:
        ok, reason = self._evaluate_preconditions(action)
        if not ok:
            self._abort_chain(action.chain_id, reason or "前置条件过期", action)
            return True
        conflict = self._conflict_detail(action)
        if conflict is not None:
            last = self._last_receipt(action.action_id)
            if last is None or last.event != EventType.BLOCKED.value:
                self._emit(
                    EventType.BLOCKED,
                    chain_id=action.chain_id,
                    action_id=action.action_id,
                    details=conflict,
                )
            return False
        action.state = ActionState.SCHEDULED
        self._emit(
            EventType.SCHEDULED,
            chain_id=action.chain_id,
            action_id=action.action_id,
        )
        return True

    def _try_dispatch(self, action: Action) -> bool:
        # 开始前重新核对：资源版本或证据变化会在这里被抓住
        ok, reason = self._evaluate_preconditions(action)
        if not ok:
            self._abort_chain(action.chain_id, reason or "前置条件过期", action)
            return True
        conflict = self._conflict_detail(action)
        if conflict is not None:
            last = self._last_receipt(action.action_id)
            if last is None or last.event != EventType.BLOCKED.value:
                self._emit(
                    EventType.BLOCKED,
                    chain_id=action.chain_id,
                    action_id=action.action_id,
                    details=conflict,
                )
            return False
        if self.executor is None:
            # 没有注入执行器时只推导到"待开始"占位，不产生派发
            return False
        self._next_generation += 1
        generation = self._next_generation
        action.generation = generation
        action.phase_index = 0
        action.state = ActionState.DISPATCHED
        order = DispatchOrder(
            action_id=action.action_id,
            chain_id=action.chain_id,
            generation=generation,
            phase=action.spec.phases[0],
            reads=action.spec.reads,
            writes=action.spec.writes,
            payload={},
        )
        self.executor.dispatch(order)
        self._emit(
            EventType.DISPATCHED,
            chain_id=action.chain_id,
            action_id=action.action_id,
            generation=generation,
            details={"phase": action.spec.phases[0]},
        )
        return True

    # ------------------------------------------------------------------
    # 全链路冲突（含间接冲突）
    # ------------------------------------------------------------------

    @staticmethod
    def _incompatible_resource(
        spec_a: ActionSpec, spec_b: ActionSpec
    ) -> str | None:
        for resource_id in spec_a.footprint() & spec_b.footprint():
            a_writes = resource_id in spec_a.writes
            b_writes = resource_id in spec_b.writes
            if a_writes or b_writes:
                return resource_id
        return None

    def _conflict_detail(self, candidate: Action) -> dict[str, Any] | None:
        """返回阻止 candidate 占位的冲突详情；无冲突返回 None。

        冲突图以"链"为节点，覆盖所有**非终态**动作：
          * 占位态（scheduled 及之后）动作持有资源槽位；
          * planned 动作虽未占位，但代表本链对后续资源的全链路主张，
            终态动作则已经释放资源，不再参与；
          * 同链动作天然合并为一个节点（全链路依赖）；
          * 跨链动作只要存在读写/写写不兼容就合并连通分量。

        候选链所在分量里出现其他链，即说明存在直接或经由桥接链的间接冲突。
        """
        graph_actions = [
            a for a in self.actions.values() if not a.is_terminal
        ]
        if candidate not in graph_actions:
            graph_actions.append(candidate)

        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        # 同链动作合并为一个全链路节点
        by_chain: dict[str, list[Action]] = {}
        for action in graph_actions:
            by_chain.setdefault(action.chain_id, []).append(action)
        for chain_id, actions in by_chain.items():
            for action in actions[1:]:
                union(chain_id, action.chain_id)

        # 跨链不兼容访问产生边
        for index, left in enumerate(graph_actions):
            for right in graph_actions[index + 1 :]:
                if left.chain_id == right.chain_id:
                    continue
                resource_id = self._incompatible_resource(left.spec, right.spec)
                if resource_id is not None:
                    union(left.chain_id, right.chain_id)

        candidate_root = find(candidate.chain_id)
        component_chains = sorted(
            chain_id for chain_id in by_chain if find(chain_id) == candidate_root
        )
        other_chains = [c for c in component_chains if c != candidate.chain_id]
        if not other_chains:
            return None

        # 直接争用只统计当前真正持有槽位的动作；与未占位（planned）动作的
        # 资源交集属于全链路主张上的间接关系
        direct_resources: list[str] = []
        for action in graph_actions:
            if action.chain_id == candidate.chain_id:
                continue
            if action.state not in OCCUPYING_STATES:
                continue
            resource_id = self._incompatible_resource(candidate.spec, action.spec)
            if resource_id is not None and resource_id not in direct_resources:
                direct_resources.append(resource_id)
        reason = (
            f"动作 {candidate.action_id} 与链 {other_chains} 处于同一全链路冲突分量"
        )
        if direct_resources:
            reason += f"，直接争用资源 {direct_resources}"
        else:
            reason += "（经由桥接链形成的间接冲突）"
        return {
            "reason": reason,
            "chains": other_chains,
            "resources": direct_resources,
            "indirect": not direct_resources,
        }

    def conflict_components(self) -> list[list[str]]:
        """当前所有非终态链形成的链级冲突分量（供观测/测试使用）。

        口径与 :meth:`_conflict_detail` 一致：planned 动作的全链路资源主张
        同样入图，因此尚未占位的链也可能出现在分量中。
        """
        live = [a for a in self.actions.values() if not a.is_terminal]
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for action in live:
            find(action.chain_id)
        for index, left in enumerate(live):
            for right in live[index + 1 :]:
                if left.chain_id == right.chain_id:
                    union(left.chain_id, right.chain_id)
                elif self._incompatible_resource(left.spec, right.spec) is not None:
                    union(left.chain_id, right.chain_id)
        groups: dict[str, list[str]] = {}
        for chain_id in parent:
            groups.setdefault(find(chain_id), []).append(chain_id)
        return [sorted(chains) for chains in groups.values()]

    # ------------------------------------------------------------------
    # 外部世界变化入口（只触发相关链的重新核对）
    # ------------------------------------------------------------------

    def resource_changed(self, resource_id: str, new_version: int) -> Receipt:
        self.world.update_resource(resource_id, new_version)
        receipt = self._emit(
            EventType.WORLD_CHANGED,
            details={"kind": "resource", "resource_id": resource_id, "version": new_version},
        )
        self.pump()
        return receipt

    def evidence_changed(
        self,
        evidence_id: str,
        active: bool,
        observed_versions: Mapping[str, int] | None = None,
    ) -> Receipt:
        self.world.update_evidence(evidence_id, active, observed_versions)
        receipt = self._emit(
            EventType.WORLD_CHANGED,
            details={"kind": "evidence", "evidence_id": evidence_id, "active": active},
        )
        self.pump()
        return receipt

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------

    def cancel(self, action_id: str) -> Receipt:
        action = self._require_action(action_id)
        if action.state in (ActionState.PLANNED, ActionState.SCHEDULED):
            action.state = ActionState.CANCELLED
            receipt = self._emit(
                EventType.CANCELLED,
                chain_id=action.chain_id,
                action_id=action_id,
                generation=action.generation or None,
                details={"reason": "派发前取消"},
            )
            self._abort_chain(
                action.chain_id, f"动作 {action_id} 在派发前被取消", action
            )
            self.pump()
            return receipt

        if action.state == ActionState.CANCELLING:
            receipt = self._emit(
                EventType.CANCEL_REJECTED,
                chain_id=action.chain_id,
                action_id=action_id,
                generation=action.generation,
                details={"reason": "取消请求已在途，等待执行器回执"},
            )
            return receipt

        if action.state == ActionState.IRREVOCABLE:
            return self._emit(
                EventType.CANCEL_REJECTED,
                chain_id=action.chain_id,
                action_id=action_id,
                generation=action.generation,
                details={"reason": "动作已进入不可撤销阶段，继续等待结果"},
            )

        if action.state == ActionState.EFFECTIVE:
            return self._emit(
                EventType.CANCEL_REJECTED,
                chain_id=action.chain_id,
                action_id=action_id,
                generation=action.generation,
                details={"reason": "动作已生效，只能确认"},
            )

        if action.state in TERMINAL_STATES:
            return self._emit(
                EventType.CANCEL_REJECTED,
                chain_id=action.chain_id,
                action_id=action_id,
                generation=action.generation,
                details={"reason": f"动作已处于终态 {action.state.value}"},
            )

        # DISPATCHED：尚在可撤销窗口，向执行器发取消并等待带代次的回执
        irrevocable_index = (
            action.spec.phases.index(action.spec.irrevocable_phase)
            if action.spec.irrevocable_phase is not None
            else None
        )
        if (
            irrevocable_index is not None
            and action.phase_index >= irrevocable_index
        ):
            return self._emit(
                EventType.CANCEL_REJECTED,
                chain_id=action.chain_id,
                action_id=action_id,
                generation=action.generation,
                details={"reason": "动作已越过不可撤销阶段点"},
            )
        if self.executor is None:
            raise RuntimeError("未注入执行器，无法请求取消")
        request = getattr(self.executor, "request_cancel", None)
        if request is not None:
            request(action_id, action.generation)
        action.state = ActionState.CANCELLING
        receipt = self._emit(
            EventType.CANCEL_REQUESTED,
            chain_id=action.chain_id,
            action_id=action_id,
            generation=action.generation,
        )
        self.pump()
        return receipt

    # ------------------------------------------------------------------
    # 执行器带代次回执的结算
    # ------------------------------------------------------------------

    def _require_action(self, action_id: str) -> Action:
        action = self.actions.get(action_id)
        if action is None:
            raise KeyError(f"未知动作: {action_id}")
        return action

    def _stale(
        self, action: Action, generation: int, kind: str, extra: Mapping[str, Any] | None = None
    ) -> Receipt:
        details: dict[str, Any] = {
            "reason": "代次过期",
            "received_generation": generation,
            "current_generation": action.generation,
            "kind": kind,
        }
        if extra:
            details.update(extra)
        return self._emit(
            EventType.STALE_RESULT,
            chain_id=action.chain_id,
            action_id=action.action_id,
            generation=generation,
            details=details,
        )

    def report_phase(self, action_id: str, generation: int, phase: str) -> Receipt:
        action = self._require_action(action_id)
        if generation != action.generation:
            return self._stale(action, generation, "phase", {"phase": phase})
        if action.state not in (
            ActionState.DISPATCHED,
            ActionState.IRREVOCABLE,
            ActionState.CANCELLING,
        ):
            raise ValueError(
                f"动作 {action_id} 当前状态 {action.state.value} 不接受阶段回报"
            )
        if phase not in action.spec.phases:
            raise ValueError(f"动作 {action_id} 没有阶段 {phase!r}")
        new_index = action.spec.phases.index(phase)
        if new_index <= action.phase_index:
            raise ValueError(
                f"阶段回报必须前进: 当前游标 {action.phase_index}，收到 {new_index}"
            )
        action.phase_index = new_index
        if (
            action.spec.irrevocable_phase is not None
            and phase == action.spec.irrevocable_phase
            and action.state != ActionState.IRREVOCABLE
            and action.state != ActionState.CANCELLING
        ):
            action.state = ActionState.IRREVOCABLE
        receipt = self._emit(
            EventType.PHASE_ADVANCED,
            chain_id=action.chain_id,
            action_id=action_id,
            generation=generation,
            details={"phase": phase, "state": action.state.value},
        )
        self.pump()
        return receipt

    def _apply_version_updates(
        self, action: Action, resource_versions: Mapping[str, int] | None
    ) -> None:
        if not resource_versions:
            return
        for resource_id, new_version in resource_versions.items():
            current = self.world.resource_version(resource_id)
            if current is not None and new_version <= current:
                raise ValueError(
                    f"动作 {action.action_id} 回报的资源 {resource_id} 版本 "
                    f"{new_version} 不大于当前版本 {current}"
                )
            self.world.update_resource(resource_id, new_version)

    def report_effective(
        self,
        action_id: str,
        generation: int,
        resource_versions: Mapping[str, int] | None = None,
    ) -> Receipt:
        action = self._require_action(action_id)
        if generation != action.generation:
            return self._stale(action, generation, "effective")
        if action.state not in (
            ActionState.DISPATCHED,
            ActionState.IRREVOCABLE,
            ActionState.CANCELLING,
        ):
            raise ValueError(
                f"动作 {action_id} 当前状态 {action.state.value} 不接受生效回报"
            )
        self._apply_version_updates(action, resource_versions)
        despite_cancel = action.state == ActionState.CANCELLING
        action.state = ActionState.EFFECTIVE
        action.phase_index = len(action.spec.phases) - 1
        receipt = self._emit(
            EventType.EFFECTIVE,
            chain_id=action.chain_id,
            action_id=action_id,
            generation=generation,
            details={
                "resource_versions": dict(resource_versions or {}),
                "despite_cancel_request": despite_cancel,
            },
        )
        self.pump()
        return receipt

    def confirm(self, action_id: str, generation: int) -> Receipt:
        action = self._require_action(action_id)
        if generation != action.generation:
            return self._stale(action, generation, "confirmation")
        if action.state != ActionState.EFFECTIVE:
            raise ValueError(
                f"动作 {action_id} 当前状态 {action.state.value}，无法确认"
            )
        action.state = ActionState.CONFIRMED
        receipt = self._emit(
            EventType.CONFIRMED,
            chain_id=action.chain_id,
            action_id=action_id,
            generation=generation,
        )
        self.pump()
        return receipt

    def report_cancelled(self, action_id: str, generation: int) -> Receipt:
        action = self._require_action(action_id)
        if generation != action.generation:
            return self._stale(action, generation, "cancelled")
        if action.state != ActionState.CANCELLING:
            raise ValueError(
                f"动作 {action_id} 当前状态 {action.state.value}，没有在途取消请求"
            )
        action.state = ActionState.CANCELLED
        cancel_receipt = self._emit(
            EventType.CANCELLED,
            chain_id=action.chain_id,
            action_id=action_id,
            generation=generation,
            details={"reason": "执行器确认取消"},
        )
        self._abort_chain(action.chain_id, f"动作 {action_id} 被取消", action)
        self.pump()
        return cancel_receipt

    def report_failed(
        self, action_id: str, generation: int, reason: str = "执行失败"
    ) -> Receipt:
        action = self._require_action(action_id)
        if generation != action.generation:
            return self._stale(action, generation, "failure", {"reason": reason})
        if action.state not in (
            ActionState.DISPATCHED,
            ActionState.IRREVOCABLE,
            ActionState.CANCELLING,
        ):
            raise ValueError(
                f"动作 {action_id} 当前状态 {action.state.value} 不接受失败回报"
            )
        action.state = ActionState.FAILED
        receipt = self._emit(
            EventType.FAILED,
            chain_id=action.chain_id,
            action_id=action_id,
            generation=generation,
            details={"reason": reason},
        )
        self._abort_chain(action.chain_id, f"动作 {action_id} 执行失败: {reason}", action)
        self.pump()
        return receipt

    # ------------------------------------------------------------------
    # 链的收尾
    # ------------------------------------------------------------------

    def _abort_chain(
        self, chain_id: str, reason: str, trigger: Action | None = None
    ) -> None:
        record = self.chains[chain_id]
        if record.status != ChainStatus.ACTIVE:
            return
        record.status = ChainStatus.ABORTED
        record.abort_reason = reason
        for action in self._chain_actions(chain_id):
            if action.is_terminal:
                continue
            if action.state in OCCUPYING_STATES or action.state == ActionState.PLANNED:
                # 触发动作（通常是前置条件过期的当前头部）也要落终态，
                # 否则会出现"链已中止但动作仍在占位"的不一致；
                # 已派发及之后的在途动作不在此列，它们要等执行器结果。
                if (
                    action is trigger
                    and action.state
                    not in (ActionState.PLANNED, ActionState.SCHEDULED)
                ):
                    continue
                action.state = ActionState.REJECTED
                action.reject_reason = reason
                self._emit(
                    EventType.REJECTED,
                    chain_id=chain_id,
                    action_id=action.action_id,
                    generation=action.generation or None,
                    details={"reason": reason, "chain_aborted": True},
                )
        self._emit(
            EventType.CHAIN_ABORTED,
            chain_id=chain_id,
            action_id=trigger.action_id if trigger else None,
            details={"reason": reason},
        )

    def _complete_chain(self, record: ChainRecord) -> None:
        actions = self._chain_actions(record.chain_id)
        if any(action.state != ActionState.CONFIRMED for action in actions):
            self._abort_chain(
                record.chain_id,
                "链上存在非确认终态的动作，无法完成",
            )
            return
        record.status = ChainStatus.COMPLETED
        self._emit(
            EventType.CHAIN_COMPLETED,
            chain_id=record.chain_id,
            details={"actions": [a.action_id for a in actions]},
        )

    # ------------------------------------------------------------------
    # 重启恢复
    # ------------------------------------------------------------------

    def recovery_buckets(self) -> RecoveryBuckets:
        pending: list[str] = []
        effective: list[str] = []
        cancellable: list[str] = []
        irrevocable: list[str] = []
        for action in sorted(self.actions.values(), key=lambda a: (a.chain_id, a.spec.step)):
            if action.state in (ActionState.PLANNED, ActionState.SCHEDULED):
                pending.append(action.action_id)
            elif action.state == ActionState.EFFECTIVE:
                effective.append(action.action_id)
            elif action.state in (ActionState.DISPATCHED, ActionState.CANCELLING):
                cancellable.append(action.action_id)
            elif action.state == ActionState.IRREVOCABLE:
                irrevocable.append(action.action_id)
        return RecoveryBuckets(
            pending_start=tuple(pending),
            effective_unconfirmed=tuple(effective),
            cancellable=tuple(cancellable),
            irrevocable_in_flight=tuple(irrevocable),
        )

    # ------------------------------------------------------------------
    # 快照与恢复
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": SNAPSHOT_FORMAT,
            "clock": self._clock,
            "seq": self._seq,
            "next_generation": self._next_generation,
            "chain_order": self._chain_order,
            "world": self.world.to_dict(),
            "chains": [record.to_dict() for record in self.chains.values()],
            "actions": [action.to_dict() for action in self.actions.values()],
            "receipts": [receipt.to_dict() for receipt in self.receipts],
        }

    @classmethod
    def restore(
        cls, data: Mapping[str, Any], executor: Executor | None = None
    ) -> "RemediationCoordinator":
        if data.get("format") != SNAPSHOT_FORMAT:
            raise ValueError(
                f"不支持的快照格式: {data.get('format')!r}，期望 {SNAPSHOT_FORMAT}"
            )
        coordinator = cls(
            WorldState.from_dict(data["world"]),
            executor=executor,
            clock=data["clock"],
        )
        coordinator._seq = data["seq"]
        coordinator._next_generation = data["next_generation"]
        coordinator._chain_order = data["chain_order"]
        coordinator.chains = {
            record["chain_id"]: ChainRecord.from_dict(record) for record in data["chains"]
        }
        coordinator.actions = {
            action_data["spec"]["action_id"]: Action.from_dict(action_data)
            for action_data in data["actions"]
        }
        coordinator.receipts = [Receipt.from_dict(item) for item in data["receipts"]]
        buckets = coordinator.recovery_buckets()
        coordinator._emit(
            EventType.RECOVERED,
            details={"buckets": buckets.to_dict()},
        )
        return coordinator
