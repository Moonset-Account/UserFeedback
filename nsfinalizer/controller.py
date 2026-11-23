"""分批收敛的命名空间终结控制器.

命名空间收到删除请求后, 本控制器负责终结(finalize)流程:

1. 通过发现(discovery)获得资源类型集合, 发现结果钉在一个可指定的
   控制面修订号(revision)上;
2. 每个类型按分页清单(list)枚举命名空间内对象, 每一页都必须固定到
   当前所选修订号, 否则该页被拒绝;
3. 每轮(step)最多发出给定数量的删除动作, 按对象 UID 追踪删除回执;
4. 对象被删后同名重建会得到新的 UID, 作为新对象重新处理;
5. 已收敛的类型在没有新的失效事件(修订号前进 / 新对象更新 / 新增类型)
   时保留确认;
6. 即使每种类型都曾经各自返回空集, 也只有在同一轮发现与清单屏障下
   确认没有遗留对象时, 才允许终结命名空间;
7. 状态可存档(archive)为纯 JSON 结构, 恢复(restore)后继续处理未决
   删除动作, 并能报告阻止终结的类型与具体对象.

仅使用标准库, 适配 Python 3.11.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 阻止终结的原因常量
# ---------------------------------------------------------------------------

#: 类型尚未被当前发现结果覆盖(出现了未知类型的对象)
REASON_AWAITING_DISCOVERY = "awaiting_discovery"
#: 类型在当前修订号下从未完成过一次完整清单
REASON_LISTING_INCOMPLETE = "listing_incomplete"
#: 类型曾经完成清单, 但清单版本落后于当前所选修订号(版本过期)
REASON_LISTING_STALE = "listing_stale"
#: 清单证据有效, 但仍有已知存活对象待删除
REASON_OBJECTS_REMAINING = "objects_remaining"
#: 删除动作已发出, 仍在等待回执
REASON_DELETES_PENDING = "deletes_pending"

_ARCHIVE_VERSION = 1


# ---------------------------------------------------------------------------
# 公开数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectRef:
    """命名空间内某个对象的引用."""

    uid: str
    name: str


@dataclass(frozen=True)
class DeleteAction:
    """一轮中发出的删除动作, 按对象 UID 追踪回执."""

    type: str
    uid: str
    name: str


@dataclass(frozen=True)
class BlockingType:
    """某个阻止终结的类型及其具体对象."""

    type: str
    reason: str
    objects: Tuple[ObjectRef, ...]


@dataclass(frozen=True)
class BlockingReport:
    """阻止命名空间终结的完整报告."""

    namespace_uid: str
    revision: int
    discovery_observed: bool
    types: Tuple[BlockingType, ...]

    @property
    def is_blocked(self) -> bool:
        """只要发现屏障未建立或仍有类型未收敛, 就处于阻止状态."""
        return (not self.discovery_observed) or bool(self.types)

    def type_names(self) -> Tuple[str, ...]:
        return tuple(bt.type for bt in self.types)


# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------


class _TypeState:
    """单个资源类型的收敛状态."""

    __slots__ = ("objects", "in_progress", "expect_token", "complete_revision")

    def __init__(self) -> None:
        # uid -> name, 当前已知存活(尚未收到删除回执)的对象
        self.objects: Dict[str, str] = {}
        # 分页进度: 是否正在列举中, 以及下一页应携带的 token
        self.in_progress: bool = False
        self.expect_token: str = ""
        # 最近一次完整清单完成时的修订号; None 表示没有有效空集证据
        self.complete_revision: Optional[int] = None


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------


class NamespaceFinalizerController:
    """命名空间终结控制器.

    参数:
        namespace_uid: 当前命名空间的 UID.
        revision: 初始选定的控制面修订号(可指定, 之后由发现结果推进).
        max_deletes_per_round: 每轮(step)最多发出的删除动作数量.
    """

    def __init__(
        self,
        namespace_uid: str,
        revision: int = 0,
        max_deletes_per_round: int = 50,
    ) -> None:
        if not namespace_uid:
            raise ValueError("namespace_uid 不能为空")
        if max_deletes_per_round < 0:
            raise ValueError("max_deletes_per_round 不能为负")
        self.namespace_uid = namespace_uid
        self._revision = revision
        self._max_per_round = max_deletes_per_round
        # 发现屏障: 最近一次被接受的发现结果及其修订号
        self._discovery_revision: Optional[int] = None
        self._discovered: frozenset = frozenset()
        # 每个资源类型的收敛状态(包含发现集之外的"嫌疑"类型)
        self._types: Dict[str, _TypeState] = {}
        # 未决删除动作: uid -> (type, name)
        self._pending: Dict[str, Tuple[str, str]] = {}
        # 已收到删除回执的 UID 墓碑, 用于过滤滞后事件
        self._deleted: set = set()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        """当前所选的控制面修订号."""
        return self._revision

    @property
    def max_deletes_per_round(self) -> int:
        return self._max_per_round

    # ------------------------------------------------------------------
    # 输入: 发现 / 分页清单 / 对象更新 / 删除回执
    # ------------------------------------------------------------------

    def observe_discovery(self, revision: int, types: Iterable[str]) -> bool:
        """接受一份资源类型发现结果.

        修订号低于当前所选修订号的发现结果被忽略(返回 False)。
        修订号前进时, 所有类型的清单证据随之过期, 必须在新的屏障下
        重新获得空集证据; 新增类型同样需要重新列举。已知存活对象与
        未决删除动作不受影响。
        """
        if revision < self._revision:
            return False
        if revision > self._revision:
            self._revision = revision
            self._invalidate_listings()
        discovered = frozenset(types)
        # 从发现集中消失的类型不再阻塞终结, 其未决回执仍会被匹配
        for t in list(self._types):
            if t not in discovered and not self._types[t].objects:
                if not any(pt == t for pt, _ in self._pending.values()):
                    del self._types[t]
        for t in discovered:
            self._types.setdefault(t, _TypeState())
        self._discovered = discovered
        self._discovery_revision = revision
        return True

    def advance_revision(self, revision: int) -> bool:
        """显式推进所选修订号(不改动发现集)。

        推进后所有清单证据过期, 且需要在新修订号下重新观察发现结果,
        才能重新建立发现屏障。修订号不前进时返回 False。
        """
        if revision <= self._revision:
            return False
        self._revision = revision
        self._invalidate_listings()
        return True

    def observe_page(
        self,
        type_: str,
        revision: int,
        page_token: str,
        items: Iterable,
        next_page_token: Optional[str],
    ) -> bool:
        """接受某类型的一页清单.

        页面必须固定到当前所选修订号, 且页 token 必须与本类型的分页
        进度衔接(第一页 token 为空串)。不满足时该页被拒绝(返回 False)。
        最后一页的 next_page_token 为 None 或空串, 此时该类型在当前
        修订号下获得完整清单证据。
        """
        if revision != self._revision:
            return False
        if type_ not in self._discovered:
            return False
        ts = self._types[type_]
        if page_token == "":
            # (重新)开始一次列举
            ts.in_progress = True
            ts.expect_token = ""
        if not ts.in_progress or page_token != ts.expect_token:
            return False
        for item in items:
            uid, name = self._coerce_item(item)
            if uid in self._deleted or uid in self._pending:
                continue
            ts.objects[uid] = name
        if next_page_token:
            ts.expect_token = next_page_token
        else:
            ts.in_progress = False
            ts.complete_revision = revision
        return True

    def observe_update(
        self,
        type_: str,
        uid: str,
        name: str,
        revision: Optional[int] = None,
    ) -> bool:
        """接受一次对象更新(新建或变更)事件.

        已删除(墓碑)或已发出删除动作的对象的滞后事件被忽略。
        携带修订号且落后于当前屏障的事件同样被忽略——屏障处的清单
        已经覆盖了它。真正的新对象会使该类型的空集证据失效, 需要
        重新列举; 同名重建的对象带有新 UID, 按新对象重新处理。
        """
        if revision is not None and revision < self._revision:
            return False
        if uid in self._deleted or uid in self._pending:
            return False
        ts = self._types.get(type_)
        if ts is None:
            ts = _TypeState()
            self._types[type_] = ts
        is_new = uid not in ts.objects
        ts.objects[uid] = name
        if is_new:
            # 新对象出现 => 既有空集证据不足以覆盖该类型, 需重新列举
            ts.complete_revision = None
            ts.in_progress = False
            ts.expect_token = ""
        return True

    def observe_delete_receipt(self, uid: str) -> bool:
        """按对象 UID 匹配删除回执.

        命中未决删除动作时返回 True 并记录墓碑; 未知或重复的 UID
        返回 False。
        """
        entry = self._pending.pop(uid, None)
        if entry is None:
            return False
        type_, _name = entry
        self._deleted.add(uid)
        ts = self._types.get(type_)
        if ts is not None:
            ts.objects.pop(uid, None)
        return True

    # ------------------------------------------------------------------
    # 控制: 每轮分批发出删除动作
    # ------------------------------------------------------------------

    def step(self) -> List[DeleteAction]:
        """执行一轮收敛: 对已知存活且未决的对象发出删除动作.

        每轮最多发出 max_deletes_per_round 个动作; 已发出但未收到
        回执的对象不会重复发出。返回本轮换到的删除动作列表(按
        (类型, 名称, UID) 排序, 保证确定性)。
        """
        actions: List[DeleteAction] = []
        if self._max_per_round == 0:
            return actions
        candidates: List[Tuple[str, str, str]] = []
        for t in sorted(self._discovered):
            ts = self._types.get(t)
            if ts is None:
                continue
            for uid, name in ts.objects.items():
                if uid not in self._pending:
                    candidates.append((t, name, uid))
        candidates.sort()
        for t, name, uid in candidates[: self._max_per_round]:
            self._pending[uid] = (t, name)
            actions.append(DeleteAction(type=t, uid=uid, name=name))
        return actions

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def pending_deletes(self) -> Tuple[DeleteAction, ...]:
        """当前所有未决(已发出未收到回执)的删除动作."""
        entries = sorted((t, name, uid) for uid, (t, name) in self._pending.items())
        return tuple(DeleteAction(type=t, uid=uid, name=name) for t, name, uid in entries)

    def blocking(self) -> BlockingReport:
        """返回阻止命名空间终结的类型与具体对象."""
        blocking_types: List[BlockingType] = []
        for t in sorted(self._types):
            ts = self._types[t]
            objs = [
                ObjectRef(uid, name)
                for uid, name in sorted(ts.objects.items())
                if uid not in self._pending
            ]
            pend = [
                ObjectRef(uid, name)
                for uid, (pt, name) in sorted(self._pending.items())
                if pt == t
            ]
            all_objs = tuple(objs + pend)
            if t not in self._discovered:
                if all_objs:
                    blocking_types.append(
                        BlockingType(t, REASON_AWAITING_DISCOVERY, all_objs)
                    )
                continue
            if ts.complete_revision is None:
                reason = REASON_LISTING_INCOMPLETE
            elif ts.complete_revision != self._revision:
                reason = REASON_LISTING_STALE
            elif objs:
                reason = REASON_OBJECTS_REMAINING
            elif pend:
                reason = REASON_DELETES_PENDING
            else:
                continue  # 该类型已在当前屏障下收敛
            blocking_types.append(BlockingType(t, reason, all_objs))
        discovery_observed = (
            self._discovery_revision is not None
            and self._discovery_revision == self._revision
        )
        return BlockingReport(
            namespace_uid=self.namespace_uid,
            revision=self._revision,
            discovery_observed=discovery_observed,
            types=tuple(blocking_types),
        )

    def finalized(self) -> bool:
        """是否允许终结命名空间.

        只有在同一轮发现与清单屏障下, 所有被发现的类型都持有当前
        修订号的完整清单证据、没有遗留对象、没有未决回执时才为真。
        """
        return not self.blocking().is_blocked

    # ------------------------------------------------------------------
    # 存档 / 恢复
    # ------------------------------------------------------------------

    def archive(self) -> dict:
        """把控制器状态序列化为纯 JSON 兼容结构."""
        return {
            "version": _ARCHIVE_VERSION,
            "namespace_uid": self.namespace_uid,
            "revision": self._revision,
            "max_deletes_per_round": self._max_per_round,
            "discovery_revision": self._discovery_revision,
            "discovered": sorted(self._discovered),
            "types": {
                t: {
                    "objects": dict(ts.objects),
                    "in_progress": ts.in_progress,
                    "expect_token": ts.expect_token,
                    "complete_revision": ts.complete_revision,
                }
                for t, ts in sorted(self._types.items())
            },
            "pending": {uid: [t, name] for uid, (t, name) in sorted(self._pending.items())},
            "deleted": sorted(self._deleted),
        }

    @classmethod
    def restore(cls, data: dict) -> "NamespaceFinalizerController":
        """从存档恢复控制器, 未决删除动作继续按 UID 等待回执."""
        if not isinstance(data, dict):
            raise ValueError("存档必须是 dict")
        if data.get("version") != _ARCHIVE_VERSION:
            raise ValueError(f"不支持的存档版本: {data.get('version')!r}")
        for key in ("namespace_uid", "revision", "max_deletes_per_round",
                    "discovered", "types", "pending", "deleted"):
            if key not in data:
                raise ValueError(f"存档缺少字段: {key}")
        c = cls(
            namespace_uid=data["namespace_uid"],
            revision=data["revision"],
            max_deletes_per_round=data["max_deletes_per_round"],
        )
        c._discovery_revision = data.get("discovery_revision")
        c._discovered = frozenset(data["discovered"])
        for t, ts_data in data["types"].items():
            ts = _TypeState()
            ts.objects = dict(ts_data.get("objects", {}))
            ts.in_progress = bool(ts_data.get("in_progress", False))
            ts.expect_token = ts_data.get("expect_token", "")
            ts.complete_revision = ts_data.get("complete_revision")
            c._types[t] = ts
        for uid, entry in data["pending"].items():
            t, name = entry
            c._pending[uid] = (t, name)
        c._deleted = set(data["deleted"])
        return c

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _invalidate_listings(self) -> None:
        """修订号前进时, 作废所有类型的分页进度.

        complete_revision 保留原值, 使 blocking() 能区分"版本过期"
        与"从未完成清单"。
        """
        for ts in self._types.values():
            ts.in_progress = False
            ts.expect_token = ""

    @staticmethod
    def _coerce_item(item) -> Tuple[str, str]:
        if isinstance(item, ObjectRef):
            return item.uid, item.name
        uid, name = item
        return uid, name
