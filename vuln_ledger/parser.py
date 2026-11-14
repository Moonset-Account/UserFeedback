"""JSON 输入解析与结构校验。

输入格式见 README。解析阶段只做结构校验和归一化，状态判定在 engine 中完成。
"""
from __future__ import annotations

import json
from typing import Any

from .models import Build, Commit, InputError, LedgerInput, MaintenanceLine, MergeResolution
from .graph import assert_acyclic


def _require(obj: Any, key: str, ctx: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise InputError(f"{ctx} 缺少必填字段: {key}")
    return obj[key]


def _as_str_list(value: Any, ctx: str) -> list[str]:
    if not isinstance(value, list):
        raise InputError(f"{ctx} 必须是字符串数组")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise InputError(f"{ctx} 中的元素必须是非空字符串")
        out.append(item)
    return out


def parse_input(raw: Any) -> LedgerInput:
    """把原始 JSON 对象归一化为 LedgerInput，并校验引用关系。"""
    if not isinstance(raw, dict):
        raise InputError("输入必须是 JSON 对象")

    commits_raw = _require(raw, "commits", "输入")
    if not isinstance(commits_raw, list) or not commits_raw:
        raise InputError("commits 必须是非空数组")

    commits: dict[str, Commit] = {}
    for idx, item in enumerate(commits_raw):
        ctx = f"commits[{idx}]"
        if not isinstance(item, dict):
            raise InputError(f"{ctx} 必须是对象")
        cid = _require(item, "id", ctx)
        if not isinstance(cid, str) or not cid:
            raise InputError(f"{ctx}.id 必须是非空字符串")
        if cid in commits:
            raise InputError(f"提交标识重复: {cid}")
        parents = _as_str_list(item.get("parents", []), f"{ctx}.parents")

        fix_ids = _as_str_list(item.get("fix_ids", []), f"{ctx}.fix_ids")
        if len(set(fix_ids)) != len(fix_ids):
            raise InputError(f"{ctx}.fix_ids 存在重复标识")

        revert_refs: list[str | None] = []
        for ridx, rv in enumerate(item.get("reverts", [])):
            rctx = f"{ctx}.reverts[{ridx}]"
            if isinstance(rv, str):
                if not rv:
                    raise InputError(f"{rctx} 不能是空字符串")
                revert_refs.append(rv)
            elif isinstance(rv, dict):
                fid = rv.get("fix_id")
                if fid is not None and (not isinstance(fid, str) or not fid):
                    raise InputError(f"{rctx}.fix_id 必须是非空字符串或 null")
                revert_refs.append(fid)
            else:
                raise InputError(f"{rctx} 必须是字符串或对象")

        commits[cid] = Commit(id=cid, parents=parents, fix_ids=fix_ids, revert_refs=revert_refs)

    lines_raw = _require(raw, "maintenance_lines", "输入")
    if not isinstance(lines_raw, list) or not lines_raw:
        raise InputError("maintenance_lines 必须是非空数组")
    lines: dict[str, MaintenanceLine] = {}
    for idx, item in enumerate(lines_raw):
        ctx = f"maintenance_lines[{idx}]"
        if not isinstance(item, dict):
            raise InputError(f"{ctx} 必须是对象")
        name = _require(item, "name", ctx)
        if not isinstance(name, str) or not name:
            raise InputError(f"{ctx}.name 必须是非空字符串")
        if name in lines:
            raise InputError(f"维护线名称重复: {name}")
        roots = _as_str_list(_require(item, "roots", ctx), f"{ctx}.roots")
        if not roots:
            raise InputError(f"{ctx}.roots 不能为空")
        recognized = _as_str_list(
            _require(item, "recognized_fix_ids", ctx), f"{ctx}.recognized_fix_ids"
        )
        if len(set(recognized)) != len(recognized):
            raise InputError(f"{ctx}.recognized_fix_ids 存在重复标识")
        lines[name] = MaintenanceLine(name=name, roots=roots, recognized_fix_ids=recognized)

    builds_raw = _require(raw, "builds", "输入")
    if not isinstance(builds_raw, list) or not builds_raw:
        raise InputError("builds 必须是非空数组")
    builds: list[Build] = []
    seen_builds: set[str] = set()
    for idx, item in enumerate(builds_raw):
        ctx = f"builds[{idx}]"
        if not isinstance(item, dict):
            raise InputError(f"{ctx} 必须是对象")
        bid = _require(item, "build_id", ctx)
        if not isinstance(bid, str) or not bid:
            raise InputError(f"{ctx}.build_id 必须是非空字符串")
        if bid in seen_builds:
            raise InputError(f"部署构建 ID 重复: {bid}")
        seen_builds.add(bid)
        line_name = _require(item, "line", ctx)
        if not isinstance(line_name, str) or not line_name:
            raise InputError(f"{ctx}.line 必须是非空字符串")
        source = _require(item, "source_commit", ctx)
        if not isinstance(source, str) or not source:
            raise InputError(f"{ctx}.source_commit 必须是非空字符串")
        builds.append(Build(build_id=bid, line=line_name, source_commit=source))

    resolutions: list[MergeResolution] = []
    for idx, item in enumerate(raw.get("merge_resolutions", [])):
        ctx = f"merge_resolutions[{idx}]"
        if not isinstance(item, dict):
            raise InputError(f"{ctx} 必须是对象")
        cid = _require(item, "commit", ctx)
        if not isinstance(cid, str) or not cid:
            raise InputError(f"{ctx}.commit 必须是非空字符串")
        resolved = _require(item, "resolved", ctx)
        if resolved not in ("fixed", "affected"):
            raise InputError(f"{ctx}.resolved 只能是 'fixed' 或 'affected'")
        fid = item.get("fix_id")
        if fid is not None and (not isinstance(fid, str) or not fid):
            raise InputError(f"{ctx}.fix_id 必须是非空字符串或 null")
        line_name = item.get("line")
        if line_name is not None and (not isinstance(line_name, str) or not line_name):
            raise InputError(f"{ctx}.line 必须是非空字符串或 null")
        resolutions.append(
            MergeResolution(commit=cid, resolved=resolved, fix_id=fid, line=line_name)
        )

    # 引用关系校验
    for cid, commit in commits.items():
        for parent in commit.parents:
            if parent not in commits:
                raise InputError(f"提交 {cid} 引用了不存在的父提交: {parent}")
        if cid in commit.parents:
            raise InputError(f"提交 {cid} 不能以自身为父提交")
    for line in lines.values():
        for root in line.roots:
            if root not in commits:
                raise InputError(f"维护线 {line.name} 的根提交不存在: {root}")
    for build in builds:
        if build.line not in lines:
            raise InputError(
                f"部署 {build.build_id} 引用了不存在的维护线: {build.line}"
            )
        if build.source_commit not in commits:
            raise InputError(
                f"部署 {build.build_id} 引用了不存在的来源提交: {build.source_commit}"
            )
    for resolution in resolutions:
        if resolution.commit not in commits:
            raise InputError(
                f"解决记录引用了不存在的合并提交: {resolution.commit}"
            )
        if resolution.line is not None and resolution.line not in lines:
            raise InputError(
                f"解决记录 {resolution.commit} 引用了不存在的维护线: {resolution.line}"
            )

    # 全图有向环检测（引用关系已校验，此时父提交必然存在）
    assert_acyclic(commits)

    return LedgerInput(commits=commits, lines=lines, builds=builds, resolutions=resolutions)


def parse_json_text(text: str) -> LedgerInput:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"JSON 解析失败: {exc}") from exc
    return parse_input(raw)
