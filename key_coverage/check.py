"""发送前密钥覆盖检查的核心算法。

规则摘要（与 README 对应）：

1. 邮件固定一个目录版本，遍历该快照里的组成员关系；通讯组可以嵌套、自引用，
   别名可指向人员 / 组 / 另一个别名。
2. 每条成员边可以带“最低密级”约束。一条到达路径上的所有约束都满足时，
   该路径才让人员有资格；同一人员沿不同路径到达时，只要有一条合格路径即有
   资格（各路径资格取并集）。
3. 自引用 / 互引用形成的环按“路径内已访问”截断，但截断前已经沿其他分支到达
   的真实成员不会丢失。
4. 有资格的人员必须在预定投递时刻持有至少一把有效密钥（半开区间
   ``[valid_from, valid_to)``），否则整封邮件停在 ``preparing`` 状态，并给出
   能回到原始组名的原因链。
5. 最终收件人按实际选中的加密密钥 ID 去重。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .model import (
    Alias,
    CoverageResult,
    DirectorySnapshot,
    DirectoryStore,
    Group,
    KeyDiagnostic,
    KeyRecord,
    MemberKind,
    MemberRef,
    MissingKeyFailure,
    PathNode,
    Person,
    RecipientCoverage,
    Status,
    classification_rank,
)


def _node_name(snapshot: DirectorySnapshot, ref: str, kind: MemberKind) -> str:
    if kind is MemberKind.PERSON:
        person = snapshot.persons.get(ref)
        return person.display_name() if person else ref
    if kind is MemberKind.GROUP:
        group = snapshot.groups.get(ref)
        return group.display_name() if group else ref
    alias = snapshot.aliases.get(ref)
    return alias.id if alias else ref


def _node_for(snapshot: DirectorySnapshot, ref: MemberRef) -> PathNode:
    return PathNode(
        ref=ref.target_id,
        kind=ref.kind,
        name=_node_name(snapshot, ref.target_id, ref.kind),
        min_level=ref.min_level,
    )


def _path_is_eligible(path: tuple[PathNode, ...], notification_rank: int) -> bool:
    """一条路径上的所有密级约束都被通知密级满足，才算合格路径。"""
    for node in path:
        if node.min_level is not None:
            if classification_rank(node.min_level) > notification_rank:
                return False
    return True


def _blocking_levels(path: tuple[PathNode, ...], notification_rank: int) -> list[str]:
    return [
        node.min_level
        for node in path
        if node.min_level is not None
        and classification_rank(node.min_level) > notification_rank
    ]


def _format_chain(path: tuple[PathNode, ...]) -> str:
    parts: list[str] = []
    for node in path:
        label = f"{node.name}（id={node.ref}）"
        if node.kind is MemberKind.ALIAS:
            label = "别名 " + label
        elif node.kind is MemberKind.GROUP:
            label = "组 " + label
        else:
            label = "成员 " + label
        if node.min_level is not None:
            label += f"，要求密级>={node.min_level}"
        parts.append(label)
    return " -> ".join(parts)


def _key_diagnostic(key: KeyRecord, when: datetime) -> KeyDiagnostic:
    if key.is_valid_at(when):
        reason = "密钥在投递时刻有效"
        valid = True
    elif key.valid_from is not None and when < key.valid_from:
        valid = False
        reason = f"密钥尚未生效（valid_from={key.valid_from.isoformat()}）"
    else:
        valid = False
        assert key.valid_to is not None
        reason = f"密钥已过有效期（valid_to={key.valid_to.isoformat()}，半开区间右端点不含）"
    return KeyDiagnostic(
        key_id=key.key_id,
        valid=valid,
        valid_from=key.valid_from,
        valid_to=key.valid_to,
        reason=reason,
    )


def _select_valid_key(keys: tuple[KeyRecord, ...], when: datetime) -> Optional[KeyRecord]:
    """从有效密钥里确定性地选一把：有效期结束越晚越优先，并列时 key_id 最小。"""
    valid = [key for key in keys if key.is_valid_at(when)]
    if not valid:
        return None
    infinite = [key for key in valid if key.valid_to is None]
    pool = infinite if infinite else valid
    latest = max(key.valid_to for key in pool)  # pool 非空且此处均为有限时间
    return sorted(
        (key for key in pool if key.valid_to == latest),
        key=lambda key: key.key_id,
    )[0]


def check_key_coverage(
    store: DirectoryStore,
    *,
    directory_version: str,
    root_group_id: str,
    notification_level: str,
    scheduled_at: datetime,
) -> CoverageResult:
    """对一封待发加密通知做发送前密钥覆盖检查。

    :param store: 按版本保存的目录。
    :param directory_version: 本封邮件固定使用的目录版本。
    :param root_group_id: 原始目标通讯组 ID（原因链从这里开始）。
    :param notification_level: 本封通知的密级。
    :param scheduled_at: 预定投递时刻（密钥有效时段按它判定）。
    :raises KeyError: 目录版本或根组不存在。
    :raises ValueError: 密级无法识别或投递时间缺少时区信息。
    """
    notification_rank = classification_rank(notification_level)
    if scheduled_at.tzinfo is None:
        raise ValueError("scheduled_at 必须带时区信息，避免有效时段比较产生歧义")

    snapshot = store.snapshot(directory_version)
    root = snapshot.group(root_group_id)

    root_node = PathNode(
        ref=root.id,
        kind=MemberKind.GROUP,
        name=root.display_name(),
        min_level=None,
    )

    # person_id -> 全部到达路径（含合格与受限路径）
    all_paths: dict[str, list[tuple[PathNode, ...]]] = {}
    cycle_cuts: list[tuple[PathNode, ...]] = []
    structural_errors: list[str] = []

    def enter_group(group: Group, path: tuple[PathNode, ...], on_stack: set[str]) -> None:
        for member in group.members:
            follow(member, path, on_stack)

    def follow(
        ref: MemberRef,
        path: tuple[PathNode, ...],
        on_stack: set[str],
    ) -> None:
        node = _node_for(snapshot, ref)
        next_path = path + (node,)

        if ref.kind is MemberKind.PERSON:
            person = snapshot.persons.get(ref.target_id)
            if person is None:
                structural_errors.append(
                    f"悬空人员引用：{_format_chain(next_path)}，"
                    f"目录版本 {snapshot.version!r} 中不存在该人员"
                )
                return
            all_paths.setdefault(person.id, []).append(next_path)
            return

        if ref.kind is MemberKind.ALIAS:
            alias = snapshot.aliases.get(ref.target_id)
            if alias is None:
                structural_errors.append(
                    f"悬空别名引用：{_format_chain(next_path)}，"
                    f"目录版本 {snapshot.version!r} 中不存在该别名"
                )
                return
            if alias.id in on_stack:
                cycle_cuts.append(next_path)
                return
            resolve_alias(alias, next_path, on_stack | {alias.id})
            return

        # ref.kind is GROUP
        group = snapshot.groups.get(ref.target_id)
        if group is None:
            structural_errors.append(
                f"悬空组引用：{_format_chain(next_path)}，"
                f"目录版本 {snapshot.version!r} 中不存在该组"
            )
            return
        if group.id in on_stack:
            cycle_cuts.append(next_path)
            return
        enter_group(group, next_path, on_stack | {group.id})

    def resolve_alias(
        alias: Alias,
        path: tuple[PathNode, ...],
        on_stack: set[str],
    ) -> None:
        target_ref = MemberRef(
            target_id=alias.target_id,
            kind=alias.kind,
            min_level=None,  # 约束只挂在成员边上，别名自身不带密级要求
        )
        follow(target_ref, path, on_stack)

    enter_group(root, (root_node,), {root.id})

    eligible_paths: dict[str, list[tuple[PathNode, ...]]] = {}
    restricted_paths: dict[str, list[tuple[PathNode, ...]]] = {}
    for person_id, paths in all_paths.items():
        good = [path for path in paths if _path_is_eligible(path, notification_rank)]
        if good:
            eligible_paths[person_id] = good
        else:
            restricted_paths[person_id] = paths

    missing: list[MissingKeyFailure] = []
    chosen: list[tuple[str, KeyRecord, list[tuple[PathNode, ...]]]] = []
    for person_id in sorted(eligible_paths):
        person = snapshot.persons[person_id]
        paths = eligible_paths[person_id]
        diagnostics = tuple(_key_diagnostic(key, scheduled_at) for key in person.keys)
        selected = _select_valid_key(person.keys, scheduled_at)
        if selected is None:
            missing.append(_build_missing_failure(person, paths, diagnostics, scheduled_at, root))
        else:
            chosen.append((person_id, selected, paths))

    # 按实际加密密钥去重：同一把 key_id 只保留一条收件人记录（保留先遇到者）。
    recipients: list[RecipientCoverage] = []
    seen_keys: set[str] = set()
    for person_id, key, paths in chosen:
        if key.key_id in seen_keys:
            continue
        seen_keys.add(key.key_id)
        person = snapshot.persons[person_id]
        recipients.append(
            RecipientCoverage(
                person_id=person_id,
                person_name=person.display_name(),
                key_id=key.key_id,
                eligible_paths=tuple(paths),
            )
        )

    status = Status.PREPARING if (missing or structural_errors) else Status.READY

    return CoverageResult(
        status=status,
        notification_level=notification_level,
        directory_version=snapshot.version,
        scheduled_at=scheduled_at,
        recipients=tuple(recipients),
        missing_key_failures=tuple(missing),
        reached_person_ids=tuple(sorted(all_paths)),
        restricted={pid: tuple(paths) for pid, paths in sorted(restricted_paths.items())},
        cycle_cuts=tuple(cycle_cuts),
        structural_errors=tuple(structural_errors),
    )


def _build_missing_failure(
    person: Person,
    paths: list[tuple[PathNode, ...]],
    diagnostics: tuple[KeyDiagnostic, ...],
    when: datetime,
    root: Group,
) -> MissingKeyFailure:
    chain_text = " 或 ".join(_format_chain(path) for path in paths)
    if not person.keys:
        key_text = "该人未登记任何加密密钥"
    else:
        detail = "；".join(diag.reason for diag in diagnostics)
        key_text = f"共 {len(person.keys)} 把密钥，投递时刻无一把有效：{detail}"
    reason = (
        f"原始组 {root.display_name()!r}（id={root.id}）出发，{chain_text}；"
        f"预定投递时刻 {when.isoformat()}，{key_text}，"
        "该有资格收件人无法被加密覆盖，整封邮件停在准备状态"
    )
    return MissingKeyFailure(
        person_id=person.id,
        person_name=person.display_name(),
        eligible_paths=tuple(paths),
        key_diagnostics=diagnostics,
        reason_chain=reason,
    )
