"""受限操作图的静态分析器。

成本语义（见 README 详述）：

* ``seq`` 顺序执行 -> 成本**相加**；
* ``branch`` / ``reuse_if`` 的各臂互斥 -> 成本逐维度取 **max**，绝不相加；
* ``segments`` 分段展开 -> 次数上界 × 段体成本；
* ``recurse`` 子包递归 -> 有界深度与扇出下的等比实例数 × 每包成本；
* 具名且被支配性分配证明的引用 -> 0 新分配；
* 匿名引用 / 不能证明共享的具名引用 -> 保守按新分配计数；
* 缺少证据或静态调用环 -> 对应维度为 None，并产生带操作位置的 blocking Issue。

分析器只读取数据并做整数运算，不执行图中任何操作。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Optional

from .errors import GraphValidationError
from .loader import evidence_limit, load_evidence, load_operation_graph
from .model import (
    B,
    COST_ZERO,
    Cost,
    Issue,
    NodeReport,
    SEVERITY_BLOCKING,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    cost_add,
    cost_max,
    cost_scale,
)

# op 别名 -> 规范名
OP_ALIASES: dict[str, str] = {
    "allocate": "alloc",
    "object": "alloc",
    "new": "alloc",
    "sequence": "seq",
    "block": "seq",
    "do": "seq",
    "children": "seq",
    "choice": "branch",
    "conditional": "branch",
    "if": "branch",
    "switch": "branch",
    "segment_expand": "segments",
    "expand_segments": "segments",
    "segmented_expansion": "segments",
    "repeat": "segments",
    "map_segments": "segments",
    "chunked_expand": "segments",
    "loop": "segments",
    "subpackage": "recurse",
    "recursion": "recurse",
    "nested_packages": "recurse",
    "reference": "ref",
    "conditional_reuse": "reuse_if",
    "reuseif": "reuse_if",
    "reuse": "reuse_if",
    "invoke": "call",
    "mark_shared": "share",
    "pin": "share",
}

CANONICAL_OPS = {
    "alloc",
    "seq",
    "branch",
    "segments",
    "recurse",
    "ref",
    "reuse_if",
    "call",
    "share",
}


def _mute_tree(rep: NodeReport, reason: str) -> NodeReport:
    """把整棵子树的问题标记为非活动（原因保留），并标记不可达。"""
    muted_issues = [
        replace(iss, active=False, mute_reason=reason) for iss in rep.issues
    ]
    muted_children = [_mute_tree(ch, reason) for ch in rep.children]
    return replace(
        rep,
        issues=muted_issues,
        children=muted_children,
        unreachable=True,
    )


def instance_count(depth: Optional[int], fan_out: Optional[int]) -> Optional[int]:
    """有界递归树的实例总数上界 ``1 + f + ... + f**(d-1)``。

    * ``depth is None`` -> None（深度无界，除非成本维度本身为 0）；
    * ``depth == 0`` -> 0；
    * ``fan_out == 1`` -> depth（线性链）；
    * ``fan_out is None`` 且 depth <= 1 -> 1（扇出未知但至多一层）；
    * ``fan_out is None`` 且 depth >= 2 -> None；
    * 其余使用等比数列闭式解，避免按证据值循环展开。
    """
    if depth is None:
        return None
    if depth == 0:
        return 0
    if fan_out is None:
        return 1 if depth <= 1 else None
    if fan_out == 1:
        return depth
    return (pow(fan_out, depth) - 1) // (fan_out - 1)


class Analyzer:
    def __init__(
        self,
        graph: Mapping[str, Any],
        evidence: Mapping[str, Any],
        reject: frozenset[str],
    ) -> None:
        self.graph = graph
        self.evidence = evidence
        self.reject = reject
        self.defs: Mapping[str, Any] = {}
        self.known_region_ids: set[str] = set()

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _field(node: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in node and node[name] is not None:
                return node[name]
        return None

    @staticmethod
    def _pos_of(node: Any, path: str) -> str:
        if isinstance(node, Mapping):
            for key in ("pos", "location", "at"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return path

    def _rid_of(self, node: Mapping[str, Any], path: str) -> str:
        nid = node.get("id")
        if nid is None:
            return path
        if not isinstance(nid, str) or not nid.strip():
            raise GraphValidationError(
                "节点 id 必须是非空字符串", pos=self._pos_of(node, path), path=path
            )
        return nid

    def _share_key(
        self, node: Mapping[str, Any], path: str, *, required: bool
    ) -> Optional[str]:
        key = self._field(node, "share", "shared", "shared_id", "key", "identity")
        if key is None:
            if required:
                raise GraphValidationError(
                    "该操作缺少非空字符串类型的 share/key 字段",
                    pos=self._pos_of(node, path),
                )
            return None
        if not isinstance(key, str) or not key.strip():
            raise GraphValidationError(
                "共享键 share/key 必须是非空字符串",
                pos=self._pos_of(node, path),
            )
        return key

    def _issue(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        node: Mapping[str, Any],
        path: str,
        evidence_key: Optional[str] = None,
    ) -> Issue:
        pos = self._pos_of(node, path)
        nid = node.get("id") if isinstance(node.get("id"), str) else None
        rid = nid or path
        return Issue(
            code=code,
            severity=severity,
            message=message,
            pos=pos,
            node_id=nid,
            region_id=rid,
            evidence_key=evidence_key,
        )

    def _resolve_spec(
        self,
        spec: Any,
        *,
        field_name: str,
        node: Mapping[str, Any],
        path: str,
        default: Optional[int] = None,
        missing_code: str = B.MISSING_EVIDENCE,
    ) -> tuple[Optional[int], list[Issue]]:
        """把 int 或 ``{"evidence": key}`` 解析为整数上界。

        spec 为 None 时返回 ``default``（不产生 Issue）。
        图结构形态错误抛 :class:`GraphValidationError`；
        证据缺失/非法返回 blocking Issue。
        """
        pos = self._pos_of(node, path)
        if spec is None:
            return default, []
        if isinstance(spec, bool):
            raise GraphValidationError(
                f"字段 {field_name} 必须是非负整数或 {{'evidence': ...}}，不能是布尔值",
                pos=pos,
            )
        if isinstance(spec, int):
            if spec < 0:
                raise GraphValidationError(
                    f"字段 {field_name} 不能为负数: {spec}", pos=pos
                )
            return spec, []
        if isinstance(spec, Mapping):
            key = spec.get("evidence")
            if not isinstance(key, str) or not key.strip():
                raise GraphValidationError(
                    f"字段 {field_name}.evidence 必须是非空字符串", pos=pos
                )
            if key not in self.evidence:
                iss = self._issue(
                    missing_code,
                    SEVERITY_BLOCKING,
                    f"字段 {field_name} 需要外部证据 {key!r} 给出上限，但调用方未提供该证据；"
                    "无法证明此分支有界，导入端可拒绝该分支",
                    node=node,
                    path=path,
                    evidence_key=key,
                )
                return None, [iss]
            limit, err = evidence_limit(self.evidence[key])
            if err is not None:
                iss = self._issue(
                    B.INVALID_EVIDENCE,
                    SEVERITY_BLOCKING,
                    f"字段 {field_name} 引用的证据 {key!r} 不是合法上界：{err}",
                    node=node,
                    path=path,
                    evidence_key=key,
                )
                return None, [iss]
            return limit, []
        raise GraphValidationError(
            f"字段 {field_name} 必须是非负整数或 {{'evidence': '证据键'}}，"
            f"实际类型为 {type(spec).__name__}",
            pos=pos,
        )

    def _apply_rejection(self, rep: NodeReport, rid: str) -> NodeReport:
        rep = _mute_tree(rep, f"rejected:{rid}")
        rep.cost = COST_ZERO
        rep.rejected = True
        rep.unreachable = False
        rep.issues.append(
            Issue(
                code=B.BRANCH_REJECTED,
                severity=SEVERITY_INFO,
                message=(
                    f"区域 {rid!r} 被导入端显式拒绝：该区域成本按 0 处理，"
                    "其原始风险原因仍以非活动 Issue 保留"
                ),
                pos=rep.pos,
                region_id=rid,
                active=True,
            )
        )
        return rep

    def _arm_report(
        self,
        *,
        arm_rid: str,
        path: str,
        pos: Optional[str],
        inner: NodeReport,
    ) -> NodeReport:
        self.known_region_ids.add(arm_rid)
        arm = NodeReport(
            region_id=arm_rid,
            op="arm",
            pos=pos or inner.pos or path,
            cost=inner.cost,
            children=[inner],
            generates=inner.generates,
        )
        if arm_rid in self.reject:
            arm = self._apply_rejection(arm, arm_rid)
        return arm

    # ------------------------------------------------------------------
    # 入口分发
    # ------------------------------------------------------------------
    def analyze_node(
        self,
        node: Any,
        path: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        if not isinstance(node, Mapping):
            raise GraphValidationError(
                f"操作节点必须是 JSON 对象，得到 {type(node).__name__}", path=path
            )
        op0 = node.get("op")
        if not isinstance(op0, str) or not op0.strip():
            raise GraphValidationError(
                "节点缺少非空字符串类型的 op 字段",
                pos=self._pos_of(node, path),
                path=path,
            )
        op = OP_ALIASES.get(op0, op0)
        if op not in CANONICAL_OPS:
            raise GraphValidationError(
                f"未知操作类型 op={op0!r}，支持: {sorted(CANONICAL_OPS)}",
                pos=self._pos_of(node, path),
            )
        rid = self._rid_of(node, path)
        self.known_region_ids.add(rid)

        handler = {
            "alloc": self._op_alloc,
            "seq": self._op_seq,
            "branch": self._op_branch,
            "segments": self._op_segments,
            "recurse": self._op_recurse,
            "ref": self._op_ref,
            "reuse_if": self._op_reuse_if,
            "call": self._op_call,
            "share": self._op_share,
        }[op]
        rep = handler(node, path, rid, env, active)
        if rid in self.reject:
            rep = self._apply_rejection(rep, rid)
        return rep

    # ------------------------------------------------------------------
    # alloc / share
    # ------------------------------------------------------------------
    def _op_alloc(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        bytes_spec = self._field(node, "bytes", "size_bytes", "size")
        records_spec = self._field(node, "records", "record_count")
        b, iss_b = self._resolve_spec(
            bytes_spec, field_name="bytes", node=node, path=path, default=0
        )
        r, iss_r = self._resolve_spec(
            records_spec, field_name="records", node=node, path=path, default=1
        )
        key = self._share_key(node, path, required=False)
        return NodeReport(
            region_id=rid,
            op="alloc",
            pos=pos,
            cost=Cost(b, r),
            issues=[*iss_b, *iss_r],
            generates=frozenset({key}) if key else frozenset(),
        )

    def _op_share(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        key = self._share_key(node, path, required=True)
        return NodeReport(
            region_id=rid,
            op="share",
            pos=self._pos_of(node, path),
            cost=COST_ZERO,
            generates=frozenset({key}) if key else frozenset(),
            note=f"标记对象 {key!r} 从此位置起可被具名复用",
        )

    # ------------------------------------------------------------------
    # seq
    # ------------------------------------------------------------------
    def _op_seq(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        list_key: Optional[str] = None
        steps: Any = None
        for name in ("steps", "children", "ops"):
            if name in node:
                list_key, steps = name, node[name]
                break
        if steps is None:
            steps = []
            list_key = "steps"
        if not isinstance(steps, list):
            raise GraphValidationError(
                f"seq 的 {list_key} 必须是数组", pos=pos
            )

        total = COST_ZERO
        cur_env = env
        reps: list[NodeReport] = []
        generated: set[str] = set()
        for i, step in enumerate(steps):
            child_path = f"{path}.{list_key}[{i}]"
            rep = self.analyze_node(step, child_path, cur_env, active)
            reps.append(rep)
            total = cost_add(total, rep.cost)
            cur_env = cur_env | rep.generates
            generated.update(rep.generates)
        return NodeReport(
            region_id=rid,
            op="seq",
            pos=pos,
            cost=total,
            children=reps,
            generates=frozenset(generated),
        )

    # ------------------------------------------------------------------
    # branch
    # ------------------------------------------------------------------
    def _op_branch(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        raw_arms: list[Any] = []
        if "arms" in node or "cases" in node:
            key = "arms" if "arms" in node else "cases"
            raw_arms = node[key]
            if not isinstance(raw_arms, list) or not raw_arms:
                raise GraphValidationError(
                    f"branch.{key} 必须是非空数组", pos=pos
                )
        elif "then" in node or "else" in node:
            if "then" in node:
                raw_arms.append({"name": "then", "node": node["then"]})
            if "else" in node:
                raw_arms.append({"name": "else", "node": node["else"]})
        else:
            raise GraphValidationError(
                "branch 需要非空 arms/cases 数组，或 then/else 字段", pos=pos
            )

        arm_reports: list[NodeReport] = []
        total: Optional[Cost] = None
        gen_sets: list[frozenset[str]] = []
        for i, arm in enumerate(raw_arms):
            arm_path = f"{path}.arms[{i}]"
            arm_id: Optional[str] = None
            arm_name = str(i)
            arm_pos: Optional[str] = None
            if isinstance(arm, Mapping) and (
                "node" in arm or "do" in arm or "body" in arm
            ):
                inner = self._field(arm, "node", "do", "body")
                if arm.get("id") is not None:
                    if not isinstance(arm["id"], str) or not arm["id"].strip():
                        raise GraphValidationError(
                            "分支臂 id 必须是非空字符串",
                            pos=self._pos_of(arm, arm_path),
                        )
                    arm_id = arm["id"]
                if arm.get("name") is not None:
                    if not isinstance(arm["name"], str) or not arm["name"].strip():
                        raise GraphValidationError(
                            "分支臂 name 必须是非空字符串",
                            pos=self._pos_of(arm, arm_path),
                        )
                    arm_name = arm["name"]
                arm_pos = self._pos_of(arm, arm_path)
            else:
                inner = arm

            arm_rid = arm_id or f"{rid}#arm:{arm_name}"
            inner_rep = self.analyze_node(
                inner, f"{arm_path}.node", env, active
            )
            arm_rep = self._arm_report(
                arm_rid=arm_rid,
                path=arm_path,
                pos=arm_pos,
                inner=inner_rep,
            )
            arm_reports.append(arm_rep)
            total = arm_rep.cost if total is None else cost_max(total, arm_rep.cost)
            gen_sets.append(arm_rep.generates)

        generates = (
            gen_sets[0].intersection(*gen_sets[1:]) if gen_sets else frozenset()
        )
        condition = self._field(node, "condition", "when", "if")
        note = None
        if condition is not None:
            note = f"互斥分支，条件由调用方保证；静态分析保守假设每一臂都可能执行（condition={condition!r}）"
        return NodeReport(
            region_id=rid,
            op="branch",
            pos=pos,
            cost=total or COST_ZERO,
            children=arm_reports,
            generates=frozenset(generates),
            note=note or "互斥分支：成本取各臂逐维度 max，不相加",
        )

    # ------------------------------------------------------------------
    # segments 分段展开
    # ------------------------------------------------------------------
    def _op_segments(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        count_spec = self._field(
            node, "count", "segments", "times", "items", "number", "n"
        )
        if count_spec is None:
            raise GraphValidationError(
                "segments 缺少 count（或 evidence 上界）字段", pos=pos
            )
        count_val, count_issues = self._resolve_spec(
            count_spec,
            field_name="count",
            node=node,
            path=path,
            missing_code=B.UNBOUNDED_COUNT,
        )
        body = self._field(node, "body", "segment", "each", "item", "do")
        if body is None:
            raise GraphValidationError(
                "segments 缺少 body/each/segment 段体字段", pos=pos
            )
        body_rep = self.analyze_node(body, f"{path}.body", env, active)

        if count_val == 0:
            # 可证明零次展开：段体不可达，问题保留但静音
            body_rep = _mute_tree(body_rep, "zero_iterations")
            cost = COST_ZERO
        else:
            cost = cost_scale(body_rep.cost, count_val)

        # 次数只是“上界”，实际可能为 0，因此不把段体内注册的共享键
        # 当作段后必定可用（保守，不会漏算新分配）。
        return NodeReport(
            region_id=rid,
            op="segments",
            pos=pos,
            cost=cost,
            issues=count_issues,
            children=[body_rep],
            generates=frozenset(),
            note="分段展开：count 上界 × 段体成本",
        )

    # ------------------------------------------------------------------
    # recurse 子包递归
    # ------------------------------------------------------------------
    def _op_recurse(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        depth_spec = self._field(node, "depth", "max_depth", "levels")
        if depth_spec is None:
            raise GraphValidationError(
                "recurse 缺少 depth（递归层数上界，可为 {'evidence': ...}）字段",
                pos=pos,
            )
        depth_val, depth_issues = self._resolve_spec(
            depth_spec,
            field_name="depth",
            node=node,
            path=path,
            missing_code=B.UNBOUNDED_RECURSION,
        )

        fan_spec = self._field(
            node, "fan_out", "branching", "max_children", "children", "width"
        )
        if fan_spec is None:
            fan_val: Optional[int] = 1
            fan_issues: list[Issue] = []
        else:
            fan_val, fan_issues = self._resolve_spec(
                fan_spec,
                field_name="fan_out",
                node=node,
                path=path,
                default=1,
                missing_code=B.UNBOUNDED_RECURSION,
            )
            # depth<=1 时实例数恒为 1，扇出证据缺失不会造成无界：
            # 原因仍保留，但标记为非活动，不阻断门禁。
            if depth_val is not None and depth_val <= 1 and fan_issues:
                fan_issues = [
                    replace(
                        iss,
                        active=False,
                        mute_reason="fanout_irrelevant_when_depth_le_1",
                    )
                    for iss in fan_issues
                ]

        target = self._field(node, "target", "package", "definition")
        body = self._field(node, "body", "each", "step")
        if target is not None and body is not None:
            raise GraphValidationError(
                "recurse 不能同时指定 target 与内联 body，请二选一", pos=pos
            )

        own_issues = [*depth_issues, *fan_issues]
        children: list[NodeReport] = []
        if target is not None:
            if not isinstance(target, str) or not target.strip():
                raise GraphValidationError("recurse.target 必须是非空字符串", pos=pos)
            if target not in self.defs:
                own_issues.append(
                    self._issue(
                        B.UNKNOWN_TARGET,
                        SEVERITY_BLOCKING,
                        f"recurse 引用的子包定义 {target!r} 不存在，无法证明其资源成本",
                        node=node,
                        path=path,
                    )
                )
                per_instance = Cost(None, None)
            else:
                target_rep = self.analyze_node(
                    self.defs[target],
                    f"{path}.def[{target}]",
                    env,
                    active + (target,),
                )
                children.append(target_rep)
                per_instance = target_rep.cost
        elif body is not None:
            body_rep = self.analyze_node(body, f"{path}.body", env, active)
            children.append(body_rep)
            per_instance = body_rep.cost
        else:
            raise GraphValidationError(
                "recurse 需要 body/each 内联体或 target 子包定义", pos=pos
            )

        if depth_val == 0:
            # 可证明零层递归：子树/目标解析均不可达，问题保留但静音
            children = [_mute_tree(ch, "zero_depth") for ch in children]
            own_issues = [
                replace(iss, active=False, mute_reason="zero_depth")
                for iss in own_issues
            ]
            cost = COST_ZERO
        else:
            instances = instance_count(depth_val, fan_val)
            cost = cost_scale(per_instance, instances)

        return NodeReport(
            region_id=rid,
            op="recurse",
            pos=pos,
            cost=cost,
            issues=own_issues,
            children=children,
            generates=frozenset(),
            note=(
                "子包递归：实例数上界 1+f+...+f^(depth-1)，"
                "默认 fan_out=1（线性递归链）"
            ),
        )

    # ------------------------------------------------------------------
    # call 命名子包调用
    # ------------------------------------------------------------------
    def _op_call(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        target = node.get("target")
        if not isinstance(target, str) or not target.strip():
            raise GraphValidationError(
                "call 缺少非空字符串类型的 target 字段", pos=pos
            )
        if target not in self.defs:
            iss = self._issue(
                B.UNKNOWN_TARGET,
                SEVERITY_BLOCKING,
                f"call 引用的子包定义 {target!r} 不存在，无法证明其资源成本",
                node=node,
                path=path,
            )
            return NodeReport(
                region_id=rid, op="call", pos=pos,
                cost=Cost(None, None), issues=[iss],
            )
        if target in active:
            chain = " -> ".join([*active, target])
            iss = self._issue(
                B.RECURSION_CYCLE,
                SEVERITY_BLOCKING,
                f"检测到未经 depth 证据约束的静态递归调用环: {chain}；"
                "不能假定该子包递归会终止，资源上界无界，导入端可拒绝该分支，"
                "或改用带 depth/fan_out 上界的 recurse 节点建模",
                node=node,
                path=path,
            )
            return NodeReport(
                region_id=rid, op="call", pos=pos,
                cost=Cost(None, None), issues=[iss],
            )
        target_rep = self.analyze_node(
            self.defs[target],
            f"{path}.call[{target}]",
            env,
            active + (target,),
        )
        return NodeReport(
            region_id=rid,
            op="call",
            pos=pos,
            cost=target_rep.cost,
            children=[target_rep],
            generates=target_rep.generates,
        )

    # ------------------------------------------------------------------
    # ref 引用（匿名 -> 保守新分配）
    # ------------------------------------------------------------------
    def _op_ref(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        key = self._share_key(node, path, required=False)
        proven = key is not None and key in env

        if proven:
            reuse_records_spec = self._field(
                node, "reuse_records", "shared_records"
            )
            rr, rr_issues = self._resolve_spec(
                reuse_records_spec,
                field_name="reuse_records",
                node=node,
                path=path,
                default=0,
            )
            return NodeReport(
                region_id=rid,
                op="ref",
                pos=pos,
                cost=Cost(0, rr),
                issues=rr_issues,
                note=f"共享键 {key!r} 已被支配性分配证明：0 新分配",
            )

        issues: list[Issue] = []
        if key is None:
            issues.append(
                self._issue(
                    B.ANONYMOUS_REFERENCE,
                    SEVERITY_WARNING,
                    "匿名引用没有稳定共享身份，不能假定与任何已解包对象共享；"
                    "已保守按一次新分配计数",
                    node=node,
                    path=path,
                )
            )
        else:
            issues.append(
                self._issue(
                    B.UNPROVEN_SHARING,
                    SEVERITY_WARNING,
                    f"具名引用 {key!r} 在当前操作位置之前没有支配性的已解包/注册记录，"
                    "不能证明共享；已保守按一次新分配计数",
                    node=node,
                    path=path,
                )
            )

        bytes_spec = self._field(node, "bytes", "fresh_bytes", "size_bytes", "size")
        if bytes_spec is None:
            bval: Optional[int] = None
            issues.append(
                self._issue(
                    B.MISSING_OBJECT_SIZE,
                    SEVERITY_BLOCKING,
                    "该引用不能证明共享，而图中没有给出保守新分配所需的 bytes 大小上界；"
                    "无法证明内存预算，导入端可拒绝该分支或要求补充证据",
                    node=node,
                    path=path,
                )
            )
        else:
            bval, b_issues = self._resolve_spec(
                bytes_spec, field_name="bytes", node=node, path=path
            )
            issues.extend(b_issues)

        records_spec = self._field(node, "records", "fresh_records")
        rval, r_issues = self._resolve_spec(
            records_spec,
            field_name="records",
            node=node,
            path=path,
            default=1,
        )
        issues.extend(r_issues)
        return NodeReport(
            region_id=rid, op="ref", pos=pos, cost=Cost(bval, rval), issues=issues
        )

    # ------------------------------------------------------------------
    # reuse_if 条件复用
    # ------------------------------------------------------------------
    def _op_reuse_if(
        self,
        node: Mapping[str, Any],
        path: str,
        rid: str,
        env: frozenset[str],
        active: tuple[str, ...],
    ) -> NodeReport:
        pos = self._pos_of(node, path)
        key = self._share_key(node, path, required=True)
        assert key is not None
        proven = key in env

        # -- reuse 臂：条件成立（对象已解包），环境中加入该键 ----------
        reuse_inner: Optional[NodeReport] = None
        reuse_issues: list[Issue] = []
        reuse_node = node.get("reuse")
        if isinstance(reuse_node, Mapping):
            reuse_inner = self.analyze_node(
                reuse_node, f"{path}.reuse", env | {key}, active
            )
            reuse_cost = reuse_inner.cost
        else:
            rr_spec = self._field(node, "reuse_records", "shared_records")
            rr_val, reuse_issues = self._resolve_spec(
                rr_spec,
                field_name="reuse_records",
                node=node,
                path=f"{path}.reuse",
                default=0,
            )
            reuse_cost = Cost(0, rr_val)

        reuse_arm_rid = f"{rid}#arm:reuse"
        reuse_arm = NodeReport(
            region_id=reuse_arm_rid,
            op="arm",
            pos=self._pos_of(reuse_node, f"{path}.reuse")
            if isinstance(reuse_node, Mapping)
            else f"{path}.reuse",
            cost=reuse_cost,
            issues=list(reuse_issues),
            children=[reuse_inner] if reuse_inner is not None else [],
            generates=reuse_inner.generates if reuse_inner else frozenset(),
        )
        self.known_region_ids.add(reuse_arm_rid)

        # -- fresh 臂：未命中复用时的保守新分配 -------------------------
        fresh_inner: Optional[NodeReport] = None
        fresh_issues: list[Issue] = []
        fresh_node = node.get("fresh")
        if isinstance(fresh_node, Mapping):
            fresh_inner = self.analyze_node(
                fresh_node, f"{path}.fresh", env, active
            )
            fresh_cost = fresh_inner.cost
        else:
            bytes_spec = self._field(node, "bytes", "fresh_bytes")
            if bytes_spec is None:
                bval: Optional[int] = None
                fresh_issues.append(
                    Issue(
                        code=B.MISSING_OBJECT_SIZE,
                        severity=SEVERITY_BLOCKING,
                        message=(
                            "reuse_if 没有给出 fresh 臂节点，也没有 bytes 上界；"
                            "复用时点无法证明命中，因而无法证明内存上界"
                        ),
                        pos=pos,
                        node_id=node.get("id") if isinstance(node.get("id"), str) else None,
                        region_id=rid,
                    )
                )
            else:
                bval, bi = self._resolve_spec(
                    bytes_spec, field_name="bytes", node=node, path=path
                )
                fresh_issues.extend(bi)
            records_spec = self._field(node, "records", "fresh_records")
            rval, ri = self._resolve_spec(
                records_spec,
                field_name="records",
                node=node,
                path=path,
                default=1,
            )
            fresh_issues.extend(ri)
            fresh_cost = Cost(bval, rval)

        fresh_arm_rid = f"{rid}#arm:fresh"
        fresh_arm = NodeReport(
            region_id=fresh_arm_rid,
            op="arm",
            pos=self._pos_of(fresh_node, f"{path}.fresh")
            if isinstance(fresh_node, Mapping)
            else f"{path}.fresh",
            cost=fresh_cost,
            issues=fresh_issues,
            children=[fresh_inner] if fresh_inner is not None else [],
            generates=fresh_inner.generates if fresh_inner else frozenset(),
        )
        self.known_region_ids.add(fresh_arm_rid)

        # 先处理“证明必命中 -> fresh 臂结构不可达”，再应用显式拒绝，
        # 最后才组合成本，确保臂级 reject 能影响 max 的结果。
        if proven:
            fresh_arm = _mute_tree(fresh_arm, "sharing_proven")
            note = f"共享键 {key!r} 已被支配性分配证明：reuse_if 必然命中复用"
        else:
            note = "条件复用：reuse/fresh 互斥，成本取 max，不相加"

        if reuse_arm_rid in self.reject:
            reuse_arm = self._apply_rejection(reuse_arm, reuse_arm_rid)
        if fresh_arm_rid in self.reject:
            fresh_arm = self._apply_rejection(fresh_arm, fresh_arm_rid)

        own_issues: list[Issue] = []
        if not proven:
            own_issues.append(
                self._issue(
                    B.REUSE_WITHOUT_EVIDENCE,
                    SEVERITY_WARNING,
                    f"reuse_if 依赖的已解包对象 {key!r} 在当前位置没有支配性分配证据；"
                    "静态分析假设 fresh 臂也可能执行，上界取两臂 max",
                    node=node,
                    path=path,
                )
            )
            effective_cost = cost_max(reuse_arm.cost, fresh_arm.cost)
        else:
            effective_cost = reuse_arm.cost

        gen_sets = [reuse_arm.generates, fresh_arm.generates]
        generates = gen_sets[0].intersection(*gen_sets[1:])
        return NodeReport(
            region_id=rid,
            op="reuse_if",
            pos=pos,
            cost=effective_cost,
            issues=own_issues,
            children=[reuse_arm, fresh_arm],
            generates=frozenset(generates),
            note=note,
        )


def analyze(
    graph: Any,
    evidence: Any = None,
    *,
    reject: frozenset[str] | set[str] | tuple[str, ...] | list[str] = (),
):
    """分析受限操作图，返回 :class:`~budget_guard.model.AnalysisReport`。

    参数
    ----
    graph:
        dict 形式的操作图，或本地 JSON 文件路径（由
        :func:`load_operation_graph` 处理）。
    evidence:
        ``{证据键: 非负整数 或 {"limit": int, "source": ...}}``，
        也可以是证据 JSON 路径。
    reject:
        导入端决定拒绝的 region id 集合（来自
        ``report.rejectable_branches[*].branch_id`` 或报告树中的
        ``region_id``）。被拒绝区域成本按 0 处理，原始 Issue 保留但静音。
    """
    from .model import AnalysisReport

    g = load_operation_graph(graph)
    ev = load_evidence(evidence)
    if not all(isinstance(x, str) and x for x in reject):
        raise TypeError("reject 中的 id 必须都是非空字符串")
    reject_set = frozenset(reject)

    # 顶层操作描述数组 -> 隐式顺序 seq
    if isinstance(g, list):
        g = {"op": "seq", "id": "operations", "steps": g}

    definitions = g.get("definitions", {})
    if definitions is None:
        definitions = {}
    if not isinstance(definitions, Mapping):
        raise GraphValidationError("顶层 definitions 必须是 JSON 对象")

    if "root" in g:
        root_spec: Any = g["root"]
        root_path = "$.root"
    elif "op" in g:
        root_spec = g
        root_path = "$"
    else:
        raise GraphValidationError(
            "操作图顶层必须是节点对象（含 op），或包含 root 字段"
        )

    analyzer = Analyzer(g, ev, reject_set)
    analyzer.defs = definitions

    if isinstance(root_spec, str):
        root_spec = {"op": "call", "target": root_spec}
    root_report = analyzer.analyze_node(root_spec, root_path, frozenset(), ())

    unknown = set(reject_set) - analyzer.known_region_ids
    if unknown:
        raise ValueError(
            "reject 包含报告中不存在的 region id: "
            f"{sorted(unknown)}；可用 id 见 report.known_region_ids"
        )

    return AnalysisReport(
        graph=g,
        evidence=ev,
        reject=reject_set,
        root=root_report,
        known_region_ids=frozenset(analyzer.known_region_ids),
    )
