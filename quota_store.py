"""对象存储配额模块。

特性：
- 块级去重：同一个块在同一租户的全部对象里只收费一次，在存储池里也只占一份物理空间。
- 事务式批量操作：一笔操作可新增、替换、删除多个对象，也可让目标租户复制另一租户
  在操作开始时的对象清单。
- 一次结算：每笔操作按最终引用关系结算各受影响租户的配额与全池容量；超限时抛出
  QuotaExceededError，携带超限方与差额，且存储状态完全不变（所有对象仍可按原清单读取）。
- 物理块只在最后一个引用被移走时释放。
- 每次操作返回实际重新计数的块编号（只涉及本次被改对象的旧清单与新清单中的块）。

仅使用 Python 标准库，适用于 Python 3.11。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ObjectStore",
    "QuotaExceededError",
    "Violation",
    "OperationReport",
    "TENANT_SCOPE",
    "POOL_SCOPE",
]

TENANT_SCOPE = "tenant"
POOL_SCOPE = "pool"

_MISSING = object()


@dataclass(frozen=True)
class Violation:
    """一次超限记录。deficit 为超出限额的字节数（差额）。"""

    scope: str  # TENANT_SCOPE 或 POOL_SCOPE
    name: str  # 租户名；池超限时为 "pool"
    used: int  # 按最终引用关系结算后的用量
    limit: int

    @property
    def deficit(self) -> int:
        return self.used - self.limit


class QuotaExceededError(Exception):
    """结算超限。属性 violations 为 Violation 元组，包含所有超限方与差额。

    抛出本异常时，存储状态保持操作前的样子，所有对象仍可按原清单读取。
    """

    def __init__(self, violations):
        self.violations = tuple(violations)
        detail = "; ".join(
            f"{v.scope}:{v.name} used={v.used} limit={v.limit} deficit={v.deficit}"
            for v in self.violations
        )
        super().__init__(f"quota exceeded: {detail}")


@dataclass(frozen=True)
class OperationReport:
    """一笔操作成功后的结算报告。"""

    recounted_blocks: tuple  # 本次实际重新计数的块编号（排序后）
    tenant_usage: dict  # 本次受影响租户 -> 结算后用量
    pool_used: int  # 结算后全池物理占用
    pool_capacity: int


class ObjectStore:
    """带配额与跨租户去重的对象存储。"""

    def __init__(self, pool_capacity, blocks=None):
        """pool_capacity：全池物理容量（字节，>=0）。
        blocks：可选的 {块编号: 字节数} 初始块目录。"""
        if not isinstance(pool_capacity, int) or pool_capacity < 0:
            raise ValueError("pool_capacity 必须是非负整数")
        self._pool_capacity = pool_capacity
        self._block_sizes = {}
        if blocks:
            for block_id, size in blocks.items():
                self.register_block(block_id, size)
        # tenant -> {"quota": int|None, "objects": {obj: [block,...]}, "usage": int}
        self._tenants = {}
        # tenant -> {block: 该租户引用此块的对象数}
        self._tenant_refs = {}
        # block -> 引用此块的对象总数（跨全部租户）；归零即释放物理块
        self._block_refs = {}
        self._pool_used = 0

    # ------------------------------------------------------------------ 目录与租户

    def register_block(self, block_id, size):
        """登记块及其字节数。重复登记同一块会覆盖其字节数（仅允许在无人引用时）。"""
        if not isinstance(size, int) or size < 0:
            raise ValueError("块字节数必须是非负整数")
        if block_id in self._block_sizes and self._block_refs.get(block_id, 0) > 0:
            if self._block_sizes[block_id] != size:
                raise ValueError(f"块 {block_id!r} 仍被引用，不能改变其字节数")
        self._block_sizes[block_id] = size

    def block_size(self, block_id):
        return self._block_sizes[block_id]

    def set_quota(self, tenant, quota):
        """创建/更新租户配额。quota 为 None 表示不限量。"""
        if quota is not None and (not isinstance(quota, int) or quota < 0):
            raise ValueError("配额必须是非负整数或 None")
        entry = self._tenants.setdefault(
            tenant, {"quota": None, "objects": {}, "usage": 0}
        )
        entry["quota"] = quota
        self._tenant_refs.setdefault(tenant, {})

    def quota(self, tenant):
        return self._tenants[tenant]["quota"]

    # ------------------------------------------------------------------ 查询

    def usage(self, tenant):
        """租户当前计费用量（块级去重后）。"""
        return self._tenants[tenant]["usage"]

    @property
    def pool_used(self):
        """全池当前物理占用。"""
        return self._pool_used

    @property
    def pool_capacity(self):
        return self._pool_capacity

    def live_blocks(self):
        """当前仍占用物理空间的块编号集合（至少被一个对象引用）。"""
        return set(self._block_refs)

    def read(self, tenant, obj):
        """按原清单读取对象：返回写入时的块清单副本（保留顺序与重复项）。"""
        objects = self._tenants[tenant]["objects"]
        return list(objects[obj])

    def list_objects(self, tenant):
        """租户当前对象清单的快照：{对象: [块, ...]}。"""
        return {obj: list(blocks) for obj, blocks in self._tenants[tenant]["objects"].items()}

    # ------------------------------------------------------------------ 便捷单对象操作

    def put(self, tenant, obj, blocks):
        """新增或替换一个对象（等价于只含该对象的一笔操作）。"""
        return self.apply({tenant: {obj: list(blocks)}})

    def delete(self, tenant, obj):
        """删除一个对象。删除不存在的对象视为无操作。"""
        return self.apply({tenant: {obj: None}})

    # ------------------------------------------------------------------ 事务式批量操作

    def apply(self, changes=None, *, copy_from=None):
        """执行一笔操作并按最终引用关系一次结算。

        changes：{租户: {对象: 新块清单 或 None}}。None 表示删除该对象；
        清单用于新增或替换。同一对象在一笔操作中出现多次时，以最后一条为准。
        copy_from：可选的 (源租户, 目标租户)。目标租户的对象清单整体替换为
        源租户在本操作开始时的对象清单快照；changes 中对目标租户的修改随后叠加。

        成功返回 OperationReport；超限抛出 QuotaExceededError 且状态不变。
        """
        changes = changes or {}

        # 1) 汇总每个租户的对象级变更计划：tenant -> {obj: 新清单 或 None(删除)}
        plan = {}
        if copy_from is not None:
            src, dst = copy_from
            if src == dst:
                raise ValueError("copy_from 的源租户与目标租户不能相同")
            self._require_tenant(src)
            self._require_tenant(dst)
            # 操作开始时的快照：之后本操作内对源租户的改动不影响复制内容
            snapshot = self.list_objects(src)
            dst_plan = {obj: None for obj in self._tenants[dst]["objects"]}
            dst_plan.update(snapshot)
            plan[dst] = dst_plan
        for tenant, obj_changes in changes.items():
            self._require_tenant(tenant)
            tplan = plan.setdefault(tenant, {})
            for obj, listing in obj_changes.items():
                tplan[obj] = None if listing is None else list(listing)

        # 2) 计算每个租户、每个块的引用计数增量，并收集实际重新计数的块
        recounted = set()
        tenant_ref_delta = {}  # tenant -> {block: delta}
        for tenant, tplan in plan.items():
            objects = self._tenants[tenant]["objects"]
            ref_delta = tenant_ref_delta.setdefault(tenant, {})
            for obj, new_listing in tplan.items():
                old_set = set(objects.get(obj, ()))
                new_set = set() if new_listing is None else set(new_listing)
                unknown = new_set.difference(self._block_sizes)
                if unknown:
                    raise KeyError(f"未登记的块: {sorted(unknown, key=repr)!r}")
                # 本对象旧清单与新清单里的块即本次需要重新计数的块
                recounted |= old_set | new_set
                for block in new_set - old_set:
                    ref_delta[block] = ref_delta.get(block, 0) + 1
                for block in old_set - new_set:
                    ref_delta[block] = ref_delta.get(block, 0) - 1

        # 3) 按最终引用关系推算各租户用量（同一块在同一租户内只计一次）
        new_usage = {}
        for tenant, ref_delta in tenant_ref_delta.items():
            usage = self._tenants[tenant]["usage"]
            refs = self._tenant_refs[tenant]
            for block, delta in ref_delta.items():
                before = refs.get(block, 0)
                after = before + delta
                if after < 0:  # pragma: no cover - 防御性检查，正常流程不会触发
                    raise AssertionError(f"租户 {tenant!r} 对块 {block!r} 的引用计数为负")
                if before == 0 and after > 0:
                    usage += self._block_sizes[block]
                elif before > 0 and after == 0:
                    usage -= self._block_sizes[block]
            new_usage[tenant] = usage

        # 4) 推算全池物理占用（同一块全池只占一份；最后一个引用移走才释放）
        block_delta = {}
        for ref_delta in tenant_ref_delta.values():
            for block, delta in ref_delta.items():
                block_delta[block] = block_delta.get(block, 0) + delta
        pool_used = self._pool_used
        for block, delta in block_delta.items():
            before = self._block_refs.get(block, 0)
            after = before + delta
            if after < 0:  # pragma: no cover - 防御性检查
                raise AssertionError(f"块 {block!r} 的全局引用计数为负")
            if before == 0 and after > 0:
                pool_used += self._block_sizes[block]
            elif before > 0 and after == 0:
                pool_used -= self._block_sizes[block]

        # 5) 一次结算：任一受影响租户或全池超限则整体拒绝，状态不变
        violations = []
        for tenant, usage in new_usage.items():
            quota = self._tenants[tenant]["quota"]
            if quota is not None and usage > quota:
                violations.append(Violation(TENANT_SCOPE, tenant, usage, quota))
        if pool_used > self._pool_capacity:
            violations.append(Violation(POOL_SCOPE, POOL_SCOPE, pool_used, self._pool_capacity))
        if violations:
            raise QuotaExceededError(violations)

        # 6) 提交：更新对象清单、引用计数与用量
        for tenant, tplan in plan.items():
            objects = self._tenants[tenant]["objects"]
            for obj, new_listing in tplan.items():
                if new_listing is None:
                    objects.pop(obj, None)
                else:
                    objects[obj] = new_listing
        for tenant, ref_delta in tenant_ref_delta.items():
            refs = self._tenant_refs[tenant]
            for block, delta in ref_delta.items():
                value = refs.get(block, 0) + delta
                if value:
                    refs[block] = value
                else:
                    refs.pop(block, None)
            self._tenants[tenant]["usage"] = new_usage[tenant]
        for block, delta in block_delta.items():
            value = self._block_refs.get(block, 0) + delta
            if value:
                self._block_refs[block] = value
            else:
                self._block_refs.pop(block, None)
        self._pool_used = pool_used

        return OperationReport(
            recounted_blocks=_sorted_ids(recounted),
            tenant_usage=dict(new_usage),
            pool_used=pool_used,
            pool_capacity=self._pool_capacity,
        )

    # ------------------------------------------------------------------ 内部

    def _require_tenant(self, tenant):
        if tenant not in self._tenants:
            raise KeyError(f"未知租户 {tenant!r}，请先调用 set_quota")


def _sorted_ids(ids):
    try:
        return tuple(sorted(ids))
    except TypeError:  # 混合不可比较类型时退化为按 repr 排序，保证确定性
        return tuple(sorted(ids, key=repr))
