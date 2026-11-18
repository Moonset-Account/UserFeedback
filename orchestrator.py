"""补丁重启批次编排组件。

业务背景
--------
办公计算集群安装安全补丁后，必须等待承载进程重启并加载新版本，主机才能退出
"待防护"状态。本组件依据：

* 主机安装回执（InstallReceipt）
* 进程加载版本上报（VersionReport）
* 重启窗口容量（reboot_capacity）
* 服务副本分布（service -> host 上的进程副本）

完成重启名额的预留、下发、超时回收与生效确认，并保证以下交叉约束：

1. 只有提交了当前批次安装回执的主机才能被预留/下发；
2. 名额受重启容量限制，预留与重启中的主机共同占用名额；
3. 只有候选主机重启后，其承载的每个服务仍有不少于要求数量的健康副本时才下发；
4. 重启超时后释放名额，主机回到待确认（PENDING_CONFIRMATION）状态；
5. 主机上登记的全部进程均上报加载到目标版本，补丁才标记生效；
6. 迟到的旧批次回执不能进入新批次，旧批次的版本上报也不能完成新批次。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set


class HostState(str, Enum):
    """主机在补丁批次中的生命周期状态。"""

    NO_RECEIPT = "NO_RECEIPT"                       # 未提交当前批次回执
    AWAITING_DISPATCH = "AWAITING_DISPATCH"         # 回执已收，等待下发
    RESERVED = "RESERVED"                           # 已预留重启名额，尚未下发
    REBOOTING = "REBOOTING"                         # 已下发重启，占用名额并计时
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"   # 重启完成/超时回收，等待版本确认
    EFFECTIVE = "EFFECTIVE"                         # 全部进程加载新版本，补丁生效


class RejectReason(str, Enum):
    """拒绝/阻止一次操作的原因码。"""

    NO_ACTIVE_BATCH = "NO_ACTIVE_BATCH"
    UNKNOWN_HOST = "UNKNOWN_HOST"
    NO_RECEIPT = "NO_RECEIPT"
    STALE_RECEIPT = "STALE_RECEIPT"          # 旧批次的迟到回执
    WRONG_BATCH = "WRONG_BATCH"              # 旧批次的迟到版本上报
    CAPACITY_FULL = "CAPACITY_FULL"          # 重启容量不足
    QUORUM_BREACH = "QUORUM_BREACH"          # 健康副本不满足服务最低要求
    ILLEGAL_STATE = "ILLEGAL_STATE"
    VERSION_MISMATCH = "VERSION_MISMATCH"    # 进程上报的不是目标版本
    NOT_DISPATCHED = "NOT_DISPATCHED"        # 主机未处于重启/待确认阶段
    UNKNOWN_SERVICE = "UNKNOWN_SERVICE"


@dataclass(frozen=True)
class InstallReceipt:
    """补丁安装回执：声明某主机在某批次安装了某目标版本。"""

    host_id: str
    batch_id: str
    target_version: str
    installed_at: float


@dataclass(frozen=True)
class VersionReport:
    """进程加载版本上报；batch_id 为进程侧记录的批次，可能是旧批次。"""

    host_id: str
    batch_id: Optional[str]
    process_name: str
    loaded_version: str
    reported_at: float


@dataclass(frozen=True)
class ServiceSpec:
    """服务的健康副本最低要求。"""

    name: str
    required_healthy: int


@dataclass(frozen=True)
class Outcome:
    """一次编排操作的结果。"""

    accepted: bool
    reason: Optional[str]
    host_id: str
    state: str

    @classmethod
    def ok(cls, host_id: str, state: HostState) -> "Outcome":
        return cls(True, None, host_id, state.value)

    @classmethod
    def reject(cls, host_id: str, state: HostState, reason: RejectReason) -> "Outcome":
        return cls(False, reason.value, host_id, state.value)


@dataclass
class _HostRecord:
    """编排器内部的主机运行态记录。"""

    host_id: str
    state: HostState = HostState.NO_RECEIPT
    receipt_batch: Optional[str] = None
    target_version: Optional[str] = None
    reserved: bool = False
    rebooting: bool = False
    dispatched_at: Optional[float] = None
    timeout_at: Optional[float] = None
    # service_name -> 该主机上承载的进程名集合
    services: Dict[str, Set[str]] = field(default_factory=dict)
    # process_name -> 已确认加载的版本（仅登记当前批次的合法上报）
    reported_versions: Dict[str, str] = field(default_factory=dict)


class RebootOrchestrator:
    """批次编排器：管理单批次内主机的重启名额与生效确认。"""

    def __init__(
        self,
        reboot_capacity: int,
        services: List[ServiceSpec],
        now_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if reboot_capacity < 1:
            raise ValueError("reboot_capacity 必须 >= 1")
        self._capacity = reboot_capacity
        self._services: Dict[str, ServiceSpec] = {s.name: s for s in services}
        self._hosts: Dict[str, _HostRecord] = {}
        self._batch_id: Optional[str] = None
        self._now = now_func
        self.events: List[dict] = []

    # ------------------------------------------------------------------ #
    # 拓扑与批次管理
    # ------------------------------------------------------------------ #
    def add_replica(self, host_id: str, service_name: str, process_name: str) -> None:
        """登记服务副本分布：某主机上的某进程承载某服务。"""
        if service_name not in self._services:
            raise ValueError(RejectReason.UNKNOWN_SERVICE.value + ": " + service_name)
        host = self._hosts.setdefault(host_id, _HostRecord(host_id))
        host.services.setdefault(service_name, set()).add(process_name)

    def begin_batch(self, batch_id: Optional[str] = None) -> str:
        """开启新批次，所有主机回到无回执状态并释放全部名额与计时。"""
        self._batch_id = batch_id or ("batch-" + uuid.uuid4().hex[:8])
        for host in self._hosts.values():
            host.state = HostState.NO_RECEIPT
            host.receipt_batch = None
            host.target_version = None
            host.reserved = False
            host.rebooting = False
            host.dispatched_at = None
            host.timeout_at = None
            host.reported_versions.clear()
        self._emit("BATCH_BEGIN", None, {"batch_id": self._batch_id})
        return self._batch_id

    @property
    def batch_id(self) -> Optional[str]:
        return self._batch_id

    # ------------------------------------------------------------------ #
    # 回执
    # ------------------------------------------------------------------ #
    def submit_receipt(self, receipt: InstallReceipt) -> Outcome:
        """接收主机安装回执；旧批次的迟到回执必须被拒绝。"""
        host = self._hosts.get(receipt.host_id)
        if host is None:
            return Outcome.reject(receipt.host_id, HostState.NO_RECEIPT,
                                  RejectReason.UNKNOWN_HOST)
        if self._batch_id is None:
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.NO_ACTIVE_BATCH)
        if receipt.batch_id != self._batch_id:
            # 迟到的旧批次回执不能进入新批次
            self._emit("RECEIPT_REJECTED", host.host_id,
                       {"reason": RejectReason.STALE_RECEIPT.value,
                        "receipt_batch": receipt.batch_id})
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.STALE_RECEIPT)
        if host.state == HostState.REBOOTING:
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.ILLEGAL_STATE)

        host.receipt_batch = receipt.batch_id
        host.target_version = receipt.target_version
        if host.state == HostState.NO_RECEIPT:
            host.state = HostState.AWAITING_DISPATCH
        self._emit("RECEIPT_ACCEPTED", host.host_id,
                   {"target_version": receipt.target_version})
        return Outcome.ok(host.host_id, host.state)

    # ------------------------------------------------------------------ #
    # 名额预留与下发
    # ------------------------------------------------------------------ #
    def reserve_slot(self, host_id: str) -> Outcome:
        """为一台已回执主机预留重启名额（不触发重启、不做副本校验）。"""
        host = self._hosts.get(host_id)
        if host is None:
            return Outcome.reject(host_id, HostState.NO_RECEIPT,
                                  RejectReason.UNKNOWN_HOST)
        if self._batch_id is None:
            return Outcome.reject(host_id, host.state,
                                  RejectReason.NO_ACTIVE_BATCH)
        if host.state not in (HostState.AWAITING_DISPATCH,
                              HostState.PENDING_CONFIRMATION):
            return Outcome.reject(host_id, host.state,
                                  RejectReason.ILLEGAL_STATE)
        if self._held_slots() >= self._capacity:
            return Outcome.reject(host_id, host.state,
                                  RejectReason.CAPACITY_FULL)

        host.reserved = True
        host.state = HostState.RESERVED
        self._emit("SLOT_RESERVED", host_id, {"held": self._held_slots()})
        return Outcome.ok(host_id, host.state)

    def dispatch(self, host_id: str, timeout_seconds: float) -> Outcome:
        """下发重启：原子校验回执、容量与全部相关服务的健康副本下限。"""
        host = self._hosts.get(host_id)
        if host is None:
            return Outcome.reject(host_id, HostState.NO_RECEIPT,
                                  RejectReason.UNKNOWN_HOST)
        if self._batch_id is None:
            return Outcome.reject(host_id, host.state,
                                  RejectReason.NO_ACTIVE_BATCH)
        if host.receipt_batch != self._batch_id or host.target_version is None:
            return Outcome.reject(host_id, host.state, RejectReason.NO_RECEIPT)
        if host.state not in (HostState.AWAITING_DISPATCH,
                              HostState.RESERVED,
                              HostState.PENDING_CONFIRMATION):
            return Outcome.reject(host_id, host.state,
                                  RejectReason.ILLEGAL_STATE)

        # 容量校验：候选主机若未预留，则下发会新占用一个名额
        held = self._held_slots()
        if not host.reserved:
            held += 1
        if held > self._capacity:
            self._emit("DISPATCH_REJECTED", host_id,
                       {"reason": RejectReason.CAPACITY_FULL.value})
            return Outcome.reject(host_id, host.state,
                                  RejectReason.CAPACITY_FULL)

        # 副本校验：候选主机重启后，每个服务剩余健康副本仍须达标
        breach = self._quorum_breach(host)
        if breach is not None:
            self._emit("DISPATCH_REJECTED", host_id,
                       {"reason": RejectReason.QUORUM_BREACH.value,
                        "service": breach})
            return Outcome.reject(host_id, host.state,
                                  RejectReason.QUORUM_BREACH)

        now = self._now()
        host.reserved = False
        host.rebooting = True
        host.state = HostState.REBOOTING
        host.dispatched_at = now
        host.timeout_at = now + timeout_seconds
        host.reported_versions.clear()
        self._emit("REBOOT_DISPATCHED", host_id,
                   {"timeout_at": host.timeout_at, "held": self._held_slots()})
        return Outcome.ok(host_id, host.state)

    # ------------------------------------------------------------------ #
    # 版本上报与生效确认
    # ------------------------------------------------------------------ #
    def report_version(self, report: VersionReport) -> Outcome:
        """接收进程版本上报；全部进程加载目标版本后主机补丁生效。"""
        host = self._hosts.get(report.host_id)
        if host is None:
            return Outcome.reject(report.host_id, HostState.NO_RECEIPT,
                                  RejectReason.UNKNOWN_HOST)
        if self._batch_id is None:
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.NO_ACTIVE_BATCH)
        # 旧批次的迟到上报不能完成新批次
        if report.batch_id != self._batch_id:
            self._emit("REPORT_REJECTED", host.host_id,
                       {"reason": RejectReason.WRONG_BATCH.value,
                        "report_batch": report.batch_id})
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.WRONG_BATCH)
        if host.state not in (HostState.REBOOTING,
                              HostState.PENDING_CONFIRMATION):
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.NOT_DISPATCHED)
        if report.loaded_version != host.target_version:
            self._emit("REPORT_REJECTED", host.host_id,
                       {"reason": RejectReason.VERSION_MISMATCH.value,
                        "process": report.process_name,
                        "loaded": report.loaded_version,
                        "expected": host.target_version})
            return Outcome.reject(host.host_id, host.state,
                                  RejectReason.VERSION_MISMATCH)

        host.reported_versions[report.process_name] = report.loaded_version
        if host.state == HostState.REBOOTING:
            host.state = HostState.PENDING_CONFIRMATION
        self._emit("VERSION_REPORTED", host.host_id,
                   {"process": report.process_name,
                    "confirmed": len(host.reported_versions),
                    "expected": len(self._expected_processes(host))})

        if self._all_processes_confirmed(host):
            host.state = HostState.EFFECTIVE
            host.rebooting = False
            host.timeout_at = None
            self._emit("PATCH_EFFECTIVE", host.host_id,
                       {"version": host.target_version})
        return Outcome.ok(host.host_id, host.state)

    # ------------------------------------------------------------------ #
    # 超时回收
    # ------------------------------------------------------------------ #
    def expire_timeouts(self) -> List[str]:
        """回收所有已超时的重启名额，主机保持待确认状态。"""
        now = self._now()
        expired: List[str] = []
        for host in self._hosts.values():
            if (host.state == HostState.REBOOTING
                    and host.timeout_at is not None
                    and host.timeout_at <= now):
                host.rebooting = False
                host.timeout_at = None
                host.state = HostState.PENDING_CONFIRMATION
                expired.append(host.host_id)
                self._emit("TIMEOUT_RELEASED", host.host_id,
                           {"held": self._held_slots()})
        return expired

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_state(self, host_id: str) -> str:
        host = self._hosts.get(host_id)
        if host is None:
            raise KeyError(host_id)
        return host.state.value

    def held_slots(self) -> int:
        """当前被占用的重启名额（预留中 + 重启中）。"""
        return self._held_slots()

    def effective_hosts(self) -> List[str]:
        return sorted(h.host_id for h in self._hosts.values()
                      if h.state == HostState.EFFECTIVE)

    def healthy_replica_count(self, service_name: str) -> int:
        """某服务当前健康副本数（扣除预留/重启中的主机）。"""
        return self._healthy_after_down(service_name, self._down_hosts())

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _held_slots(self) -> int:
        return sum(1 for h in self._hosts.values()
                   if h.reserved or h.rebooting)

    def _expected_processes(self, host: _HostRecord) -> Set[str]:
        expected: Set[str] = set()
        for procs in host.services.values():
            expected.update(procs)
        return expected

    def _all_processes_confirmed(self, host: _HostRecord) -> bool:
        expected = self._expected_processes(host)
        if not expected:
            return False
        return all(host.reported_versions.get(p) == host.target_version
                   for p in expected)

    def _down_hosts(self, candidate: Optional[_HostRecord] = None) -> Set[str]:
        """当前不可用（预留/重启）主机，可额外计入候选主机。"""
        down = {h.host_id for h in self._hosts.values()
                if h.reserved or h.rebooting}
        if candidate is not None:
            down.add(candidate.host_id)
        return down

    def _healthy_after_down(self, service_name: str,
                            down: Set[str]) -> int:
        total = sum(1 for h in self._hosts.values()
                    if service_name in h.services)
        unavailable = sum(1 for h in self._hosts.values()
                          if service_name in h.services and h.host_id in down)
        return total - unavailable

    def _quorum_breach(self, candidate: _HostRecord) -> Optional[str]:
        """返回首个不满足健康副本下限的服务名；全部满足返回 None。"""
        down = self._down_hosts(candidate)
        for spec in self._services.values():
            if self._healthy_after_down(spec.name, down) < spec.required_healthy:
                return spec.name
        return None

    def _emit(self, event: str, host_id: Optional[str],
              detail: Optional[dict] = None) -> None:
        self.events.append({"ts": self._now(), "event": event,
                            "host_id": host_id, "detail": detail or {}})
