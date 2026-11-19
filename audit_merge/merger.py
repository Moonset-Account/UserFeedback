"""合并引擎：校验、去重、冲突搁置、分簇断言与临时副本回滚。

处理流水线：

1. 逐组把两种坐标归一为字符索引，并校验非空区间、版本、旧文本；
   任一编辑失败 -> 整组撤下（原子性）。
2. 独立组在同一文件同一区间给出完全相同的替换 -> 合并为一次编辑。
3. 同文件区间有重叠且改动不同 -> 直接冲突，冲突双方（含传递关联）
   全部搁置。
4. 存活组按“共享文件”做连通分簇；每个簇在临时副本上合并该簇全部编辑，
   再调用簇内各告警登记的保护断言。
5. 任一断言失效 -> 只回滚该簇涉及的所有组，记录告警编号；
   其它簇不受影响。断言全部通过的簇才写入真实工作区。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from .model import (
    ConflictRecord,
    DedupRecord,
    RawGroup,
    ResolvedEdit,
    SuggestionGroup,
    Withdrawal,
    build_line_tables,
    coerce_groups,
    resolve_edit,
)

# 保护断言签名：接收上下文，返回 None 表示通过，抛异常或返回 False 表示失效。
AssertionFn = Callable[["AssertionContext"], object]


@dataclass
class AssertionContext:
    """保护断言运行上下文，基于临时副本构造。"""

    workspace: "Workspace"
    temp_dir: str
    files: dict[str, str]
    alert_id: str
    group_ids: tuple[str, ...]
    cluster_files: tuple[str, ...]

    def temp_path(self, relative_path: str) -> str:
        return os.path.join(self.temp_dir, relative_path)

    def read_temp(self, relative_path: str) -> str:
        with open(self.temp_path(relative_path), "r", encoding="utf-8", newline="") as fh:
            return fh.read()


@dataclass
class MergeReport:
    """合并结果报告。"""

    applied_groups: list[str] = field(default_factory=list)
    applied_edits: list[ResolvedEdit] = field(default_factory=list)
    withdrawals: list[Withdrawal] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    dedup: list[DedupRecord] = field(default_factory=list)
    clusters: list[tuple[str, ...]] = field(default_factory=list)
    recorded_alerts: list[str] = field(default_factory=list)
    output_dir: str | None = None

    @property
    def ok(self) -> bool:
        return not self.withdrawals and not self.conflicts


class Workspace:
    """源码工作区：按相对路径保存文本，换行原样保留。"""

    def __init__(self, files: dict[str, str], versions: dict[str, str] | None = None):
        self.files: dict[str, str] = dict(files)
        self.versions: dict[str, str] = dict(versions or {})

    @classmethod
    def from_directory(cls, root: str, versions: dict[str, str] | None = None) -> "Workspace":
        """读取目录下全部 UTF-8 文本文件，``newline=''`` 禁止换行转换。"""

        files: dict[str, str] = {}
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                with open(full, "r", encoding="utf-8", newline="") as fh:
                    files[rel] = fh.read()
        return cls(files, versions)

    def write_to(self, root: str) -> None:
        """把全部文件写入目录，``newline=''`` 保证 CRLF/LF 原样落盘。"""

        for rel, text in self.files.items():
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)


def _overlap(a: ResolvedEdit, b: ResolvedEdit) -> bool:
    return a.file == b.file and a.start < b.end and b.start < a.end


def _same_replacement(a: ResolvedEdit, b: ResolvedEdit) -> bool:
    return a.start == b.start and a.end == b.end and a.old_text == b.old_text and a.new_text == b.new_text


def _apply_edits(text: str, edits: list[ResolvedEdit]) -> str:
    """按区间从后往前应用互不重叠的编辑，区间外字节保持不变。"""

    ordered = sorted(edits, key=lambda e: (e.start, e.end), reverse=True)
    result = text
    for edit in ordered:
        result = result[: edit.start] + edit.new_text + result[edit.end :]
    return result


def merge_suggestions(
    workspace: Workspace,
    groups_data,
    assertions: dict[str, AssertionFn],
    output_dir: str | None = None,
) -> MergeReport:
    """执行完整合并流水线并返回 :class:`MergeReport`。

    :param workspace: 源码工作区（含每个文件的当前版本号）。
    :param groups_data: 插件建议组（字典列表或 :class:`RawGroup` 列表）。
    :param assertions: 告警编号 -> 保护断言的登记表；未登记的告警
        其所属组在断言阶段撤下。
    :param output_dir: 给定后把最终合并结果写入该目录（临时副本始终在
        系统临时目录中创建并在用完后清理）。
    """

    raw_groups: list[RawGroup] = coerce_groups(groups_data)
    report = MergeReport()
    line_tables = build_line_tables(workspace.files)

    seen_ids: set[str] = set()
    resolved_groups: list[SuggestionGroup] = []
    for raw in raw_groups:
        if raw.group_id in seen_ids:
            raise ValueError(f"组 ID 重复：{raw.group_id}")
        seen_ids.add(raw.group_id)
        group = SuggestionGroup(group_id=raw.group_id, alert_id=raw.alert_id, version=raw.version)
        failed_reason: str | None = None
        for edit in raw.edits:
            text = workspace.files.get(edit.file)
            if text is None:
                failed_reason = f"文件 {edit.file} 不在工作区"
                break
            expected_version = workspace.versions.get(edit.file)
            if expected_version is not None and edit.version != expected_version:
                failed_reason = (
                    f"版本对不上：{edit.file} 期望 {expected_version!r}，"
                    f"建议基于 {edit.version!r}"
                )
                break
            try:
                resolved = resolve_edit(
                    edit, raw.group_id, raw.alert_id, text, line_tables[edit.file]
                )
            except Exception as exc:  # SpanError 等坐标问题
                failed_reason = f"坐标解析失败：{exc}"
                break
            if resolved.start >= resolved.end:
                failed_reason = "替换区间必须为非空"
                break
            if text[resolved.start : resolved.end] != edit.old_text:
                failed_reason = "旧文本与当前源码对不上"
                break
            group.edits.append(resolved)
        if failed_reason is not None:
            report.withdrawals.append(
                Withdrawal(
                    group_id=raw.group_id,
                    alert_id=raw.alert_id,
                    reason=failed_reason,
                    files=tuple(dict.fromkeys(e.file for e in raw.edits)),
                    stage="validate",
                )
            )
            continue
        resolved_groups.append(group)

    # ---- 去重：不同组在同一文件同一区间给出完全相同的替换，共用一次编辑 ----
    key_owner: dict[tuple, ResolvedEdit] = {}
    key_merged: dict[tuple, list[str]] = {}
    surviving: list[SuggestionGroup] = []
    for group in resolved_groups:
        kept: list[ResolvedEdit] = []
        for edit in group.edits:
            owner = key_owner.get(edit.key)
            if owner is None:
                key_owner[edit.key] = edit
                key_merged[edit.key] = [group.group_id]
                kept.append(edit)
            else:
                # 与已保留编辑完全相同：共用一次编辑，本组不再单独持有
                key_merged[edit.key].append(group.group_id)
        surviving.append(
            SuggestionGroup(
                group_id=group.group_id,
                alert_id=group.alert_id,
                version=group.version,
                edits=kept,
            )
        )
    for key, owner in key_owner.items():
        merged = tuple(key_merged[key][1:])
        if merged:
            report.dedup.append(
                DedupRecord(
                    key=key,
                    kept_group=owner.group_id,
                    merged_groups=merged,
                    file=owner.file,
                )
            )

    # ---- 冲突检测：同文件区间重叠且改动不同，冲突双方全部搁置 ----
    conflicted: set[str] = set()
    all_edits = [edit for group in surviving for edit in group.edits]
    recorded_pairs: set[tuple[str, str, str]] = set()
    for i in range(len(all_edits)):
        for j in range(i + 1, len(all_edits)):
            a, b = all_edits[i], all_edits[j]
            if a.group_id == b.group_id or not _overlap(a, b):
                continue
            if _same_replacement(a, b):
                continue
            conflicted.add(a.group_id)
            conflicted.add(b.group_id)
            pair_key = (a.file, frozenset((a.group_id, b.group_id)))
            if pair_key not in recorded_pairs:
                recorded_pairs.add(pair_key)
                report.conflicts.append(
                    ConflictRecord(
                        file=a.file,
                        left_group=a.group_id,
                        right_group=b.group_id,
                        left_key=a.key,
                        right_key=b.key,
                    )
                )

    active: list[SuggestionGroup] = []
    for group in surviving:
        if group.group_id in conflicted:
            report.withdrawals.append(
                Withdrawal(
                    group_id=group.group_id,
                    alert_id=group.alert_id,
                    reason="与其它组在重叠区间给出不同改动，直接冲突搁置",
                    files=tuple(dict.fromkeys(e.file for e in group.edits)),
                    stage="conflict",
                )
            )
        else:
            active.append(group)

    # ---- 按共享文件对存活组做连通分簇 ----
    ids = [group.group_id for group in active]
    file_to_groups: dict[str, list[str]] = {}
    for group in active:
        for rel in dict.fromkeys(e.file for e in group.edits):
            file_to_groups.setdefault(rel, []).append(group.group_id)

    parent: dict[str, str] = {gid: gid for gid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for group_ids in file_to_groups.values():
        anchor = group_ids[0]
        for other in group_ids[1:]:
            ra, rb = find(anchor), find(other)
            if ra != rb:
                parent[rb] = ra

    cluster_map: dict[str, list[SuggestionGroup]] = {}
    for group in active:
        root = find(group.group_id)
        cluster_map.setdefault(root, []).append(group)
    clusters = list(cluster_map.values())
    clusters.sort(key=lambda members: min(m.group_id for m in members))

    # ---- 每个簇在临时副本上应用编辑并运行保护断言 ----
    for members in clusters:
        cluster_id = tuple(m.group_id for m in sorted(members, key=lambda g: g.group_id))
        report.clusters.append(cluster_id)
        cluster_files = tuple(
            sorted({edit.file for group in members for edit in group.edits})
        )
        cluster_edits = [edit for group in members for edit in group.edits]
        merged_files = dict(workspace.files)
        for rel in cluster_files:
            merged_files[rel] = _apply_edits(
                workspace.files[rel], [e for e in cluster_edits if e.file == rel]
            )

        failed_alert: str | None = None
        failure_reason: str | None = None
        temp_dir = tempfile.mkdtemp(prefix="audit_merge_")
        try:
            staged = Workspace(merged_files, workspace.versions)
            staged.write_to(temp_dir)
            for group in sorted(members, key=lambda g: g.group_id):
                alert_id = group.alert_id
                assertion = assertions.get(alert_id)
                if assertion is None:
                    failed_alert = alert_id
                    failure_reason = f"告警 {alert_id} 未登记保护断言"
                    break
                ctx = AssertionContext(
                    workspace=staged,
                    temp_dir=temp_dir,
                    files=merged_files,
                    alert_id=alert_id,
                    group_ids=cluster_id,
                    cluster_files=cluster_files,
                )
                try:
                    outcome = assertion(ctx)
                except Exception as exc:
                    failed_alert = alert_id
                    failure_reason = f"保护断言抛出异常：{type(exc).__name__}: {exc}"
                    break
                if outcome is False:
                    failed_alert = alert_id
                    failure_reason = "保护断言返回 False"
                    break
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if failed_alert is not None:
            for group in members:
                if group.alert_id not in report.recorded_alerts:
                    report.recorded_alerts.append(group.alert_id)
                report.withdrawals.append(
                    Withdrawal(
                        group_id=group.group_id,
                        alert_id=group.alert_id,
                        reason=failure_reason or "保护断言失效",
                        files=tuple(
                            sorted({edit.file for edit in group.edits})
                        ),
                        stage="assertion",
                        triggered_alert=failed_alert,
                    )
                )
            continue

        # 断言全部通过：簇内编辑正式生效
        for rel in cluster_files:
            workspace.files[rel] = merged_files[rel]
        for group in members:
            report.applied_groups.append(group.group_id)
            report.applied_edits.extend(group.edits)
            if group.alert_id not in report.recorded_alerts:
                report.recorded_alerts.append(group.alert_id)

    if output_dir is not None:
        os.makedirs(output_dir, exist_ok=True)
        workspace.write_to(output_dir)
        report.output_dir = os.path.abspath(output_dir)
    return report
