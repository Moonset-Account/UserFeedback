"""静态准入分析器。

判定规则（与题面逐条对应）：

* 目录授权只在其**确认建立并按所有权条款锁定后**成立（
  :class:`~admission.model.EstablishDirectory` 后必须有所有者一致的
  :class:`~admission.model.LockDirectory`）；
* 继承目录的所有权未经锁定确认前不可接收对象，继承动作本身保留待复核；
* 移动分别确认源对象的私有处理状态与目标目录的可接收性
  （:class:`~admission.model.MoveObject`）；
* 按分支未知的目录所有权保留待复核（:class:`~admission.model.UnknownBranch`
  与分支合并规则）；
* 审计逐操作定位“按哪个目录、由哪个创建/锁定身份”得出判断；
* 对象以不可变 ID 标识，复用旧路径名产生的是全新对象，不能继承旧对象授权。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .model import (
    Branch,
    CreateFile,
    EstablishDirectory,
    InheritDirectory,
    LockDirectory,
    MoveObject,
    Operation,
    Sequence,
    UnknownBranch,
    Verdict,
)

# ---------------------------------------------------------------------------
# 判定原因码（审计 entry.reason 使用，全部为稳定字符串）
# ---------------------------------------------------------------------------

# 结构性错误
R_STRUCTURAL = "STRUCTURAL_ERROR"

# 目录建立 / 继承 / 锁定
R_DIR_ESTABLISHED = "DIRECTORY_ESTABLISHED_NOT_AUTHORIZED"
R_DIR_DUPLICATE = "DIRECTORY_ID_DUPLICATE"
R_DIR_UNKNOWN = "DIRECTORY_UNKNOWN"
R_INHERIT_REVIEW = "INHERITED_OWNERSHIP_UNCONFIRMED"
R_INHERIT_DUPLICATE = "DIRECTORY_ID_DUPLICATE"
R_LOCK_OK = "DIRECTORY_LOCKED_OWNERSHIP_CONFIRMED"
R_LOCK_NOT_ESTABLISHED = "LOCK_BEFORE_ESTABLISH"
R_LOCK_OWNER_MISMATCH = "LOCK_OWNER_MISMATCH"
R_LOCK_NONPRIVATE = "LOCK_NONPRIVATE_DIRECTORY"
R_LOCK_REJECTED = "LOCK_ON_REJECTED_DIRECTORY"
R_LOCK_ALREADY = "LOCK_ALREADY_LOCKED"
R_LOCK_INHERIT_UNVERIFIED = "LOCK_INHERITED_OWNERSHIP_UNVERIFIED"

# 文件创建
R_FILE_OK = "FILE_CREATED_IN_LOCKED_DIRECTORY"
R_FILE_DIR_NOT_LOCKED = "CREATE_IN_NON_LOCKED_DIRECTORY"
R_FILE_DIR_REJECTED = "CREATE_IN_REJECTED_DIRECTORY"
R_FILE_DIR_UNKNOWN = "CREATE_IN_UNKNOWN_DIRECTORY"
R_FILE_DIR_AMBIGUOUS = "CREATE_IN_AMBIGUOUS_DIRECTORY"
R_FILE_DIR_INHERITED = "CREATE_IN_INHERITED_UNCONFIRMED_DIRECTORY"
R_FILE_DUPLICATE = "OBJECT_ID_DUPLICATE"

# 移动
R_MOVE_OK = "MOVE_ALLOWED_PRIVATE_SOURCE_RECEIVING_TARGET"
R_MOVE_SOURCE_UNKNOWN = "MOVE_SOURCE_UNKNOWN"
R_MOVE_SOURCE_AMBIGUOUS = "MOVE_SOURCE_AMBIGUOUS_ACROSS_BRANCHES"
R_MOVE_SOURCE_NONPRIVATE = "MOVE_SOURCE_NOT_PRIVATE"
R_MOVE_TARGET_UNKNOWN = "MOVE_TARGET_DIRECTORY_UNKNOWN"
R_MOVE_TARGET_REJECTED = "MOVE_TARGET_DIRECTORY_REJECTED"
R_MOVE_TARGET_AMBIGUOUS = "MOVE_TARGET_OWNERSHIP_AMBIGUOUS"
R_MOVE_TARGET_NOT_LOCKED = "MOVE_TARGET_NOT_LOCKED"
R_MOVE_TARGET_INHERITED = "MOVE_TARGET_INHERITED_UNCONFIRMED"
R_MOVE_NAME_CONFLICT = "MOVE_TARGET_NAME_OCCUPIED"

# 分支 / 未知所有权
R_BRANCH_OWNER_UNKNOWN = "BRANCH_UNKNOWN_DIRECTORY_OWNERSHIP"
R_BRANCH_DIR_STATE_DIVERGED = "BRANCH_DIRECTORY_STATE_DIVERGED"
R_BRANCH_LOCK_OWNER_DIVERGED = "BRANCH_LOCK_OWNER_DIVERGED"
R_BRANCH_OBJECT_DIVERGED = "BRANCH_OBJECT_STATE_DIVERGED"
R_BRANCH_LOCATION_DIVERGED = "BRANCH_OBJECT_LOCATION_DIVERGED"
R_BRANCH_BINDING_CONFLICT = "BRANCH_PATH_BINDING_CONFLICT"

_RANK = {Verdict.ALLOW: 0, Verdict.REVIEW: 1, Verdict.DENY: 2}


# ---------------------------------------------------------------------------
# 审计结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEntry:
    """单个具体操作的审计记录。

    结构控制流节点（Sequence/Branch/UnknownBranch 包装本身）不产生条目；
    分支内的具体操作各自记录，``branch_path`` 标注其所在分支。
    """

    sequence_index: int
    """该操作在整棵输入树的具体操作先序序号（从 1 开始）。"""
    operation: Operation
    verdict: Verdict
    reason: str
    basis_directory_id: Optional[str]
    """得出判断所依据的目录 ID（按哪个目录）。"""
    basis_identity: Optional[str]
    """得出判断所依据的创建/锁定身份（由哪个身份）。"""
    object_id: Optional[str] = None
    """涉及的文件对象 ID（如适用）。"""
    branch_path: Tuple[int, ...] = ()
    """分支定位，例如 (0, 1) 表示第 0 个 Branch 的第 1 个备选分支。"""
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass(frozen=True)
class AuditReport:
    """整份输入的准入报告。"""

    verdict: Verdict
    entries: Tuple[AuditEntry, ...]
    structural_errors: Tuple[str, ...] = ()

    def entries_for(self, object_id: str) -> Tuple[AuditEntry, ...]:
        """返回与某对象 ID 相关的全部审计条目。"""
        return tuple(
            e
            for e in self.entries
            if e.object_id == object_id
            or getattr(e.operation, "id", None) == object_id
        )

    def has_reason(self, reason: str) -> bool:
        return any(e.reason == reason for e in self.entries)

    def reasons(self) -> Tuple[str, ...]:
        return tuple(e.reason for e in self.entries)


# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------

# 目录生命周期
_S_UNKNOWN = "UNKNOWN"          # 分支合流后状态无法确定
_S_ESTABLISHED = "ESTABLISHED"  # 已建立，尚未锁定（未授权）
_S_LOCKED = "LOCKED"            # 已按所有权条款锁定（已授权）
_S_INHERITED = "INHERITED"      # 继承而来，所有权未确认
_S_REJECTED = "REJECTED"        # 曾被确定性拒绝（如锁定所有者不匹配）


@dataclass
class _DirState:
    state: str = _S_UNKNOWN
    path: Optional[str] = None
    owner: Optional[str] = None          # 建立者 / 声明的预期所有者
    lock_owner: Optional[str] = None     # 实际锁定身份
    ambiguous: bool = False              # 分支合流后是否不确定
    review_reason: Optional[str] = None  # 不确定时的复核原因


@dataclass
class _ObjState:
    name: Optional[str] = None
    directory_id: Optional[str] = None
    creator: Optional[str] = None
    private: bool = True
    uncertain: bool = False              # 分支合流后对象是否可能不存在/不可达
    review_reason: Optional[str] = None
    location_uncertain: bool = False     # 对象存在但所在目录跨分支不一致


@dataclass
class _State:
    dirs: Dict[str, _DirState] = field(default_factory=dict)
    objs: Dict[str, _ObjState] = field(default_factory=dict)
    # (directory_id, name) -> object_id，路径/名字绑定
    bindings: Dict[Tuple[str, str], str] = field(default_factory=dict)
    # object_id -> (directory_id, name)，与 bindings 反向同步
    locations: Dict[str, Tuple[str, str]] = field(default_factory=dict)

    def clone(self) -> "_State":
        return deepcopy(self)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def admit(program: Any) -> AuditReport:
    """对结构化程序做静态准入判定。

    ``program`` 可以是单个操作、:class:`Sequence`，或操作的 list/tuple。
    """

    if isinstance(program, (list, tuple)):
        root: Operation = Sequence(steps=list(program))
    else:
        root = program

    errors = _validate(root)
    if errors:
        return AuditReport(
            verdict=Verdict.DENY,
            entries=(),
            structural_errors=tuple(errors),
        )

    state = _State()
    entries: List[AuditEntry] = []
    counter = [0]
    _walk(root, state, entries, counter, ())
    verdict = Verdict.ALLOW
    for entry in entries:
        if _RANK[entry.verdict] > _RANK[verdict]:
            verdict = entry.verdict
    return AuditReport(verdict=verdict, entries=tuple(entries))


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------


def _validate(program: Any) -> List[str]:
    errors: List[str] = []

    def check(node: Any, ctx: str) -> None:
        if isinstance(node, (EstablishDirectory, InheritDirectory)):
            _require_id(node.id, f"{ctx}.id", errors)
            if not isinstance(node.path, str) or not node.path:
                errors.append(f"{ctx}.path 必须是非空字符串")
            owner = node.owner if isinstance(node, EstablishDirectory) else node.expected_owner
            if owner is not None and (not isinstance(owner, str) or not owner):
                errors.append(f"{ctx} 所有者身份必须是非空字符串")
        elif isinstance(node, LockDirectory):
            _require_id(node.directory_id, f"{ctx}.directory_id", errors)
            if not isinstance(node.owner, str) or not node.owner:
                errors.append(f"{ctx}.owner 必须是非空字符串")
        elif isinstance(node, CreateFile):
            _require_id(node.id, f"{ctx}.id", errors)
            if not isinstance(node.name, str) or not node.name:
                errors.append(f"{ctx}.name 必须是非空字符串")
            _require_id(node.directory_id, f"{ctx}.directory_id", errors)
            if not isinstance(node.creator, str) or not node.creator:
                errors.append(f"{ctx}.creator 必须是非空字符串")
            if not isinstance(node.private, bool):
                errors.append(f"{ctx}.private 必须是布尔值")
        elif isinstance(node, MoveObject):
            _require_id(node.object_id, f"{ctx}.object_id", errors)
            _require_id(node.target_directory_id, f"{ctx}.target_directory_id", errors)
            if node.target_name is not None and (
                not isinstance(node.target_name, str) or not node.target_name
            ):
                errors.append(f"{ctx}.target_name 若非空必须是非空字符串")
        elif isinstance(node, UnknownBranch):
            for did in node.affected_directory_ids:
                _require_id(did, f"{ctx}.affected_directory_ids", errors)
            for oid in node.affected_object_ids:
                _require_id(oid, f"{ctx}.affected_object_ids", errors)
        elif isinstance(node, Sequence):
            for i, step in enumerate(node.steps):
                check(step, f"{ctx}.steps[{i}]")
        elif isinstance(node, Branch):
            if not node.alternatives:
                errors.append(f"{ctx} 至少需要一个备选分支")
            for i, alt in enumerate(node.alternatives):
                check(alt, f"{ctx}.alternatives[{i}]")
        else:
            errors.append(f"{ctx} 未知操作类型: {type(node).__name__}")

    check(program, "program")
    return errors


def _require_id(value: Any, ctx: str, errors: List[str]) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{ctx} 必须是非空字符串标识")


# ---------------------------------------------------------------------------
# 控制流遍历
# ---------------------------------------------------------------------------


def _walk(
    node: Operation,
    state: _State,
    entries: List[AuditEntry],
    counter: List[int],
    branch_path: Tuple[int, ...],
) -> None:
    if isinstance(node, Sequence):
        for step in node.steps:
            _walk(step, state, entries, counter, branch_path)
        return

    if isinstance(node, Branch):
        forked: List[_State] = []
        for i, alt in enumerate(node.alternatives):
            sub = state.clone()
            _walk(alt, sub, entries, counter, branch_path + (i,))
            forked.append(sub)
        merged = _merge_states(forked)
        # 把合并结果写回调用方持有的 state 对象
        state.dirs = merged.dirs
        state.objs = merged.objs
        state.bindings = merged.bindings
        state.locations = merged.locations
        return

    if isinstance(node, UnknownBranch):
        _apply_unknown(node, state)
        return

    counter[0] += 1
    entry = _apply_leaf(node, state, counter[0], branch_path)
    entries.append(entry)


# ---------------------------------------------------------------------------
# 未知分支：受影响目录所有权按未知处理，受影响对象可达性按未知处理
# ---------------------------------------------------------------------------


def _apply_unknown(node: UnknownBranch, state: _State) -> None:
    for did in node.affected_directory_ids:
        target = state.dirs.get(did)
        if target is None:
            target = _DirState(state=_S_UNKNOWN, ambiguous=True,
                               review_reason=R_BRANCH_OWNER_UNKNOWN)
            state.dirs[did] = target
        else:
            target.ambiguous = True
            target.review_reason = R_BRANCH_OWNER_UNKNOWN
            if target.state == _S_LOCKED:
                # 所有权可能被未知分支改动：授权不再确定
                target.state = _S_UNKNOWN
                target.lock_owner = None
    for oid in node.affected_object_ids:
        target = state.objs.get(oid)
        if target is None:
            state.objs[oid] = _ObjState(
                uncertain=True, review_reason=R_BRANCH_OBJECT_DIVERGED
            )
        else:
            target.uncertain = True
            target.review_reason = R_BRANCH_OBJECT_DIVERGED


# ---------------------------------------------------------------------------
# 叶子操作判定
# ---------------------------------------------------------------------------


def _record(
    op: Operation,
    index: int,
    verdict: Verdict,
    reason: str,
    basis_directory_id: Optional[str],
    basis_identity: Optional[str],
    branch_path: Tuple[int, ...],
    object_id: Optional[str] = None,
    detail: str = "",
) -> AuditEntry:
    return AuditEntry(
        sequence_index=index,
        operation=op,
        verdict=verdict,
        reason=reason,
        basis_directory_id=basis_directory_id,
        basis_identity=basis_identity,
        object_id=object_id,
        branch_path=branch_path,
        detail=detail,
    )


def _apply_leaf(
    op: Operation, state: _State, index: int, branch_path: Tuple[int, ...]
) -> AuditEntry:
    if isinstance(op, EstablishDirectory):
        return _apply_establish(op, state, index, branch_path)
    if isinstance(op, InheritDirectory):
        return _apply_inherit(op, state, index, branch_path)
    if isinstance(op, LockDirectory):
        return _apply_lock(op, state, index, branch_path)
    if isinstance(op, CreateFile):
        return _apply_create(op, state, index, branch_path)
    if isinstance(op, MoveObject):
        return _apply_move(op, state, index, branch_path)
    raise TypeError(f"非叶子节点不应进入判定: {type(op).__name__}")


def _apply_establish(
    op: EstablishDirectory, state: _State, index: int, branch_path: Tuple[int, ...]
) -> AuditEntry:
    if op.id in state.dirs:
        return _record(op, index, Verdict.DENY, R_DIR_DUPLICATE,
                       op.id, op.owner, branch_path,
                       detail="目录标识已存在，标识必须唯一")
    state.dirs[op.id] = _DirState(
        state=_S_ESTABLISHED, path=op.path, owner=op.owner
    )
    # 建立成功但尚未授权：审计结论为 REVIEW，提示锁定前不具备接收资格。
    return _record(op, index, Verdict.REVIEW, R_DIR_ESTABLISHED,
                   op.id, op.owner, branch_path,
                   detail="目录已建立，未按所有权条款锁定前不具备授权")


def _apply_inherit(
    op: InheritDirectory, state: _State, index: int, branch_path: Tuple[int, ...]
) -> AuditEntry:
    if op.id in state.dirs:
        return _record(op, index, Verdict.DENY, R_INHERIT_DUPLICATE,
                       op.id, op.expected_owner, branch_path,
                       detail="目录标识已存在，标识必须唯一")
    state.dirs[op.id] = _DirState(
        state=_S_INHERITED, path=op.path, owner=op.expected_owner
    )
    return _record(op, index, Verdict.REVIEW, R_INHERIT_REVIEW,
                   op.id, op.expected_owner, branch_path,
                   detail="继承目录的所有权需显式确认（锁定）后才可接收对象")


def _apply_lock(
    op: LockDirectory, state: _State, index: int, branch_path: Tuple[int, ...]
) -> AuditEntry:
    target = state.dirs.get(op.directory_id)
    if target is None:
        return _record(op, index, Verdict.DENY, R_LOCK_NOT_ESTABLISHED,
                       op.directory_id, op.owner, branch_path,
                       detail="锁定目标目录不存在")
    if target.state == _S_REJECTED:
        return _record(op, index, Verdict.DENY, R_LOCK_REJECTED,
                       op.directory_id, op.owner, branch_path,
                       detail="目录已被拒绝，不可再锁定")
    if target.ambiguous:
        return _record(
            op, index, Verdict.REVIEW,
            target.review_reason or R_BRANCH_OWNER_UNKNOWN,
            op.directory_id, op.owner, branch_path,
            detail="目录所有权按分支未知，锁定保留待复核",
        )
    if target.state == _S_LOCKED:
        return _record(op, index, Verdict.DENY, R_LOCK_ALREADY,
                       op.directory_id, target.lock_owner, branch_path,
                       detail="目录已锁定，不可重复锁定")
    if target.state == _S_INHERITED:
        # 继承目录：只有锁定身份与预期所有者一致，才视为所有权确认。
        if target.owner is not None and op.owner == target.owner:
            target.state = _S_LOCKED
            target.lock_owner = op.owner
            return _record(op, index, Verdict.ALLOW, R_LOCK_OK,
                           op.directory_id, op.owner, branch_path,
                           detail="继承目录所有权已确认并锁定")
        target.state = _S_REJECTED
        return _record(op, index, Verdict.DENY, R_LOCK_INHERIT_UNVERIFIED,
                       op.directory_id, op.owner, branch_path,
                       detail="继承目录所有权无法按声明确认")
    if target.state != _S_ESTABLISHED:
        return _record(op, index, Verdict.DENY, R_LOCK_NOT_ESTABLISHED,
                       op.directory_id, op.owner, branch_path)
    if op.owner != target.owner:
        target.state = _S_REJECTED
        return _record(op, index, Verdict.DENY, R_LOCK_OWNER_MISMATCH,
                       op.directory_id, op.owner, branch_path,
                       detail=f"锁定身份 {op.owner!r} 与创建身份 "
                              f"{target.owner!r} 不一致")
    target.state = _S_LOCKED
    target.lock_owner = op.owner
    return _record(op, index, Verdict.ALLOW, R_LOCK_OK,
                   op.directory_id, op.owner, branch_path,
                   detail="目录已按创建所有权条款私有锁定")


def _apply_create(
    op: CreateFile, state: _State, index: int, branch_path: Tuple[int, ...]
) -> AuditEntry:
    if op.id in state.objs:
        return _record(op, index, Verdict.DENY, R_FILE_DUPLICATE,
                       op.directory_id, op.creator, branch_path, object_id=op.id,
                       detail="对象标识已存在，标识必须唯一")

    target = state.dirs.get(op.directory_id)
    if target is None:
        return _record(op, index, Verdict.DENY, R_FILE_DIR_UNKNOWN,
                       op.directory_id, op.creator, branch_path, object_id=op.id,
                       detail="接收目录不存在")
    if target.ambiguous:
        return _record(
            op, index, Verdict.REVIEW,
            target.review_reason or R_FILE_DIR_AMBIGUOUS,
            op.directory_id, op.creator, branch_path, object_id=op.id,
            detail="接收目录所有权按分支未知，保留待复核",
        )
    if target.state == _S_REJECTED:
        return _record(op, index, Verdict.DENY, R_FILE_DIR_REJECTED,
                       op.directory_id, op.creator, branch_path, object_id=op.id)
    if target.state == _S_LOCKED:
        if (op.directory_id, op.name) in state.bindings:
            # 同名复用：旧路径名不继承旧授权，这里是全新对象；
            # 但同一锁定目录内不允许两个存活对象共用名字，确定性拒绝。
            return _record(op, index, Verdict.DENY, R_MOVE_NAME_CONFLICT,
                           op.directory_id, op.creator, branch_path,
                           object_id=op.id,
                           detail="目录内该名字已被另一存活对象占用")
        obj = _ObjState(name=op.name, directory_id=op.directory_id,
                        creator=op.creator, private=op.private)
        state.objs[op.id] = obj
        state.bindings[(op.directory_id, op.name)] = op.id
        state.locations[op.id] = (op.directory_id, op.name)
        return _record(op, index, Verdict.ALLOW, R_FILE_OK,
                       op.directory_id, op.creator, branch_path, object_id=op.id,
                       detail=f"文件由 {op.creator!r} 创建于已锁定目录")
    if target.state == _S_INHERITED:
        return _record(op, index, Verdict.DENY, R_FILE_DIR_INHERITED,
                       op.directory_id, op.creator, branch_path, object_id=op.id,
                       detail="继承目录所有权未确认，不可接收对象")
    # ESTABLISHED 或其他未授权状态
    return _record(op, index, Verdict.DENY, R_FILE_DIR_NOT_LOCKED,
                   op.directory_id, op.creator, branch_path, object_id=op.id,
                   detail="目录已建立但未按所有权条款锁定，不可接收对象")


def _apply_move(
    op: MoveObject, state: _State, index: int, branch_path: Tuple[int, ...]
) -> AuditEntry:
    # ---- 1. 源对象私有处理状态 ----
    source = state.objs.get(op.object_id)
    if source is None:
        return _record(op, index, Verdict.DENY, R_MOVE_SOURCE_UNKNOWN,
                       op.target_directory_id, op.mover or None, branch_path,
                       object_id=op.object_id, detail="被移动对象不存在")
    if source.uncertain:
        return _record(
            op, index, Verdict.REVIEW,
            source.review_reason or R_MOVE_SOURCE_AMBIGUOUS,
            op.target_directory_id, op.mover or None, branch_path,
            object_id=op.object_id,
            detail="源对象可达性按分支未知，移动保留待复核",
        )
    if source.location_uncertain or op.object_id not in state.locations:
        return _record(
            op, index, Verdict.REVIEW,
            source.review_reason or R_MOVE_SOURCE_AMBIGUOUS,
            op.target_directory_id, op.mover or None, branch_path,
            object_id=op.object_id,
            detail="源对象所在位置按分支不一致，移动保留待复核",
        )
    if not source.private:
        return _record(op, index, Verdict.DENY, R_MOVE_SOURCE_NONPRIVATE,
                       source.directory_id, source.creator, branch_path,
                       object_id=op.object_id,
                       detail="源对象不是私有处理对象，禁止移动")

    # ---- 2. 目标目录可接收性 ----
    target = state.dirs.get(op.target_directory_id)
    if target is None:
        return _record(op, index, Verdict.DENY, R_MOVE_TARGET_UNKNOWN,
                       op.target_directory_id, op.mover or None, branch_path,
                       object_id=op.object_id, detail="目标目录不存在")
    if target.ambiguous:
        return _record(
            op, index, Verdict.REVIEW,
            target.review_reason or R_MOVE_TARGET_AMBIGUOUS,
            op.target_directory_id, target.lock_owner or target.owner,
            branch_path, object_id=op.object_id,
            detail="目标目录所有权按分支未知，保留待复核",
        )
    if target.state == _S_REJECTED:
        return _record(op, index, Verdict.DENY, R_MOVE_TARGET_REJECTED,
                       op.target_directory_id, target.owner, branch_path,
                       object_id=op.object_id)
    if target.state == _S_INHERITED:
        return _record(op, index, Verdict.DENY, R_MOVE_TARGET_INHERITED,
                       op.target_directory_id, target.owner, branch_path,
                       object_id=op.object_id,
                       detail="继承目录所有权未确认，不可接收对象")
    if target.state != _S_LOCKED:
        return _record(op, index, Verdict.DENY, R_MOVE_TARGET_NOT_LOCKED,
                       op.target_directory_id, target.owner, branch_path,
                       object_id=op.object_id,
                       detail="目标目录未按所有权条款锁定")

    # ---- 3. 目标名字占用（同目录换名 / 跨目录移动都检查）----
    new_name = op.target_name or source.name
    occupant = state.bindings.get((op.target_directory_id, new_name))
    if occupant is not None and occupant != op.object_id:
        return _record(op, index, Verdict.DENY, R_MOVE_NAME_CONFLICT,
                       op.target_directory_id, target.lock_owner, branch_path,
                       object_id=op.object_id,
                       detail=f"目标名字已被对象 {occupant!r} 占用")

    # ---- 4. 提交：只更新绑定，对象 ID 与创建身份不变 ----
    old_loc = state.locations.pop(op.object_id)
    state.bindings.pop(old_loc, None)
    source.directory_id = op.target_directory_id
    source.name = new_name
    state.bindings[(op.target_directory_id, new_name)] = op.object_id
    state.locations[op.object_id] = (op.target_directory_id, new_name)
    return _record(
        op, index, Verdict.ALLOW, R_MOVE_OK,
        op.target_directory_id, target.lock_owner, branch_path,
        object_id=op.object_id,
        detail=f"私有源对象移动到由 {target.lock_owner!r} 锁定的目录，"
               f"创建身份仍为 {source.creator!r}",
    )


# ---------------------------------------------------------------------------
# 分支合并
# ---------------------------------------------------------------------------


def _merge_states(forked: List[_State]) -> _State:
    if len(forked) == 1:
        return forked[0]

    merged = _State()
    all_dir_ids = set()
    all_obj_ids = set()
    for sub in forked:
        all_dir_ids.update(sub.dirs)
        all_obj_ids.update(sub.objs)

    # --- 目录合并 ---
    for did in all_dir_ids:
        variants = [sub.dirs[did] for sub in forked if did in sub.dirs]
        merged.dirs[did] = _merge_dir(did, variants, len(forked))

    # --- 对象合并 ---
    for oid in all_obj_ids:
        variants = [sub.objs[oid] for sub in forked if oid in sub.objs]
        merged.objs[oid] = _merge_obj(oid, variants, len(forked))

    # --- 名字绑定合并：同一槽位指向不同对象即冲突 ---
    slot_sources: Dict[Tuple[str, str], set] = {}
    for sub in forked:
        for slot, oid in sub.bindings.items():
            slot_sources.setdefault(slot, set()).add(oid)
    for slot, oids in slot_sources.items():
        if len(oids) == 1:
            merged.bindings[slot] = next(iter(oids))
        else:
            # 冲突槽位不建立确定绑定；涉及对象标记位置不确定
            for oid in oids:
                obj = merged.objs.get(oid)
                if obj is not None:
                    obj.location_uncertain = True
                    obj.review_reason = R_BRANCH_BINDING_CONFLICT

    # --- 位置记录：只有在持有该对象的分支间位置一致时才保留确定位置 ---
    loc_sources: Dict[str, set] = {}
    for sub in forked:
        for oid, loc in sub.locations.items():
            loc_sources.setdefault(oid, set()).add(loc)
    for oid, locs in loc_sources.items():
        if len(locs) == 1:
            merged.locations[oid] = next(iter(locs))
        else:
            obj = merged.objs.get(oid)
            if obj is not None:
                obj.location_uncertain = True
                obj.review_reason = R_BRANCH_LOCATION_DIVERGED

    return merged


def _merge_dir(did: str, variants: List[_DirState], branch_count: int) -> _DirState:
    assert variants
    if len(variants) < branch_count:
        # 仅部分分支建立该目录：存在性本身就不确定
        base = _clone_dir(variants[0])
        base.ambiguous = True
        base.state = _S_UNKNOWN
        base.lock_owner = None
        base.review_reason = R_BRANCH_DIR_STATE_DIVERGED
        return base

    states = {v.state for v in variants}
    if any(v.ambiguous for v in variants):
        base = _clone_dir(variants[0])
        base.ambiguous = True
        base.review_reason = R_BRANCH_OWNER_UNKNOWN
        if states != {_S_LOCKED}:
            base.state = _S_UNKNOWN
            base.lock_owner = None
        return base

    if len(states) == 1:
        state = next(iter(states))
        if state == _S_LOCKED:
            owners = {v.lock_owner for v in variants}
            base = _clone_dir(variants[0])
            if len(owners) == 1:
                return base
            base.ambiguous = True
            base.state = _S_UNKNOWN
            base.lock_owner = None
            base.review_reason = R_BRANCH_LOCK_OWNER_DIVERGED
            return base
        if state == _S_REJECTED:
            return _clone_dir(variants[0])
        # ESTABLISHED / INHERITED / UNKNOWN：状态一致即可
        return _clone_dir(variants[0])

    # 状态集合不一致
    base = _clone_dir(variants[0])
    if states == {_S_ESTABLISHED, _S_LOCKED}:
        # 一个分支已授权、另一个还没有：汇合后授权不成立
        base.state = _S_ESTABLISHED
        base.lock_owner = None
        base.ambiguous = True
        base.review_reason = R_BRANCH_DIR_STATE_DIVERGED
        return base
    base.state = _S_UNKNOWN
    base.lock_owner = None
    base.ambiguous = True
    base.review_reason = R_BRANCH_DIR_STATE_DIVERGED
    return base


def _clone_dir(src: _DirState) -> _DirState:
    return _DirState(
        state=src.state,
        path=src.path,
        owner=src.owner,
        lock_owner=src.lock_owner,
        ambiguous=src.ambiguous,
        review_reason=src.review_reason,
    )


def _merge_obj(oid: str, variants: List[_ObjState], branch_count: int) -> _ObjState:
    assert variants
    if len(variants) < branch_count or any(v.uncertain for v in variants):
        base = _clone_obj(variants[0])
        base.uncertain = True
        base.review_reason = R_BRANCH_OBJECT_DIVERGED
        return base

    privates = {v.private for v in variants}
    creators = {v.creator for v in variants}
    dirs = {v.directory_id for v in variants}
    names = {v.name for v in variants}
    base = _clone_obj(variants[0])
    if len(privates) > 1 or len(creators) > 1:
        base.uncertain = True
        base.review_reason = R_BRANCH_OBJECT_DIVERGED
        return base
    if len(dirs) > 1 or len(names) > 1:
        base.location_uncertain = True
        base.review_reason = R_BRANCH_LOCATION_DIVERGED
    return base


def _clone_obj(src: _ObjState) -> _ObjState:
    return _ObjState(
        name=src.name,
        directory_id=src.directory_id,
        creator=src.creator,
        private=src.private,
        uncertain=src.uncertain,
        review_reason=src.review_reason,
        location_uncertain=src.location_uncertain,
    )
