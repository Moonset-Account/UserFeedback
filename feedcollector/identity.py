"""身份归并、冲突检测与跨版本更正合并。

身份优先级（条目）：
    P1 atom:id
    P2 rss:guid（区分 isPermaLink true/false/缺省）
    P3 订阅源作用域内 permalink
    P4 首个 alternate 链接（同作用域）
    P5 结构指纹（仅兜底的弱标识）

硬性规则：
- 标题相同不代表同一实体，标题永不参与唯一化，只产生“同名不同实体”提示。
- 媒体 URL 永不参与唯一化；同一身份下媒体地址变化记为“媒体更换”事件。
- 任何冲突都保留全部条目与证据，绝不静默覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ParsedEntry, ParsedFeed


@dataclass
class MediaChange:
    """同一身份条目附件地址的变化证据。"""
    version_index: int
    version_label: str
    enclosure_origin: str
    old_url: str
    new_url: str
    old_fragment: str
    new_fragment: str


@dataclass
class RevisionRecord:
    """同一实体在某一版本快照中的出现记录。"""
    version_index: int
    version_label: str
    feed_url: str
    title: str | None
    published: str | None
    updated: str | None
    enclosure_urls: list[str]
    permalink: str | None
    fragment: str
    identity_tier: int


@dataclass
class CatalogEntry:
    """目录中的一个实体（可能由多个版本的修订合并而成）。"""
    identity_key: str
    identity_tier: int
    identity_evidence_explanation: str
    feed_identity_key: str
    title: str | None
    first_seen_version: int
    latest_version: int
    revisions: list[RevisionRecord] = field(default_factory=list)
    media_changes: list[MediaChange] = field(default_factory=list)
    # 当前版本的附件/封面/链接证据（来自最新一次出现）
    current_enclosure_urls: list[str] = field(default_factory=list)
    current_permalinks: list[str] = field(default_factory=list)
    cover_urls: list[str] = field(default_factory=list)
    raw_entry: dict | None = None  # 最新版本 ParsedEntry 的 JSONable 形态


@dataclass
class ConflictRecord:
    """一次需要人工知晓的身份问题。冲突永不静默覆盖。"""
    kind: str                 # duplicate_identity / identifier_overlap / duplicate_title / cross_feed_identifier
    severity: str             # conflict / warning
    title: str
    detail: str
    identity_key: str | None
    versions: list[int]
    entries: list[dict]       # 涉及条目的精简证据（标题/链接/片段/来源版本）


@dataclass
class Catalog:
    feed_groups: dict[str, dict]          # feed_identity_key -> 订阅源信息
    entries: list[CatalogEntry]
    conflicts: list[ConflictRecord]
    snapshots: list[dict]                 # 每次快照的摘要信息


def _entry_brief(entry: ParsedEntry, version_index: int) -> dict:
    return {
        "version_index": version_index,
        "feed_url": entry.feed_url,
        "entry_index": entry.entry_index,
        "title": entry.title,
        "chosen_identity_key": entry.chosen_identity_key,
        "chosen_identity_tier": entry.chosen_identity_tier,
        "permalinks": entry.permalinks,
        "enclosure_urls": [e.resolved_url for e in entry.enclosures],
        "raw_fragment": entry.raw_fragment,
    }


def _detect_snapshot_conflicts(entries: list[ParsedEntry],
                               version_index: int) -> list[ConflictRecord]:
    conflicts: list[ConflictRecord] = []

    # 1) 同一快照内选中身份键重复 —— 文档自身的标识冲突
    by_key: dict[str, list[ParsedEntry]] = {}
    for e in entries:
        by_key.setdefault(e.chosen_identity_key, []).append(e)
    for key, group in by_key.items():
        if len(group) > 1:
            conflicts.append(ConflictRecord(
                kind="duplicate_identity", severity="conflict",
                title="同一快照内出现重复身份键",
                detail=(f"身份键 {key}（P{group[0].chosen_identity_tier}）被 "
                        f"{len(group)} 个条目同时声明。二者均保留为独立目录条目，"
                        "不做覆盖，请人工核对发布者标识错误。"),
                identity_key=key, versions=[version_index],
                entries=[_entry_brief(e, version_index) for e in group],
            ))

    # 2) 跨身份线索重叠：A 的选中键出现在 B 的候选线索中 —— 标识体系冲突
    key_to_entries: dict[str, list[ParsedEntry]] = {}
    for e in entries:
        for cand in e.identity_candidates:
            key_to_entries.setdefault(cand.key, []).append(e)
    seen_overlaps: set[frozenset[str]] = set()
    for cand_key, holders in key_to_entries.items():
        chosen = {id(h): h for h in holders}
        distinct = {h.chosen_identity_key: h for h in chosen.values()}
        if len(distinct) > 1:
            sig = frozenset(distinct)
            if sig in seen_overlaps:
                continue
            seen_overlaps.add(sig)
            involved = list(distinct.values())
            conflicts.append(ConflictRecord(
                kind="identifier_overlap", severity="conflict",
                title="标识线索跨条目重叠（标识冲突）",
                detail=(f"线索 {cand_key} 同时指向 {len(involved)} 个不同身份的条目："
                        + "；".join(f"「{h.title or '无标题'}」→ {h.chosen_identity_key}"
                                    for h in involved)
                        + "。采集器按各自最高优先级标识分别建目，不合并、不覆盖。"),
                identity_key=cand_key, versions=[version_index],
                entries=[_entry_brief(h, version_index) for h in involved],
            ))

    # 3) 同标题不同实体 —— 仅警告，证明没有按标题唯一化
    by_title: dict[str, list[ParsedEntry]] = {}
    for e in entries:
        if e.title:
            by_title.setdefault(e.title.strip(), []).append(e)
    for t, group in by_title.items():
        keys = {g.chosen_identity_key for g in group}
        if len(group) > 1 and len(keys) > 1:
            conflicts.append(ConflictRecord(
                kind="duplicate_title", severity="warning",
                title="标题相同但实体不同",
                detail=(f"标题「{t}」被 {len(group)} 个不同身份条目使用"
                        f"（身份键 {len(keys)} 种）。标题不参与唯一化，各自独立建目。"),
                identity_key=None, versions=[version_index],
                entries=[_entry_brief(g, version_index) for g in group],
            ))
    return conflicts


def build_catalog(feeds: list[ParsedFeed],
                  version_labels: list[str] | None = None) -> Catalog:
    """把多个订阅源快照合并为带修订历史与冲突清单的目录。"""
    version_labels = version_labels or [f"v{i + 1}" for i in range(len(feeds))]
    catalog = Catalog(feed_groups={}, entries=[], conflicts=[], snapshots=[])

    # 订阅源分组（feed 自身身份冲突另行检测）
    feed_identities: dict[str, list[tuple[int, ParsedFeed]]] = {}
    for vi, feed in enumerate(feeds):
        feed_identities.setdefault(feed.feed_identity_key, []).append((vi, feed))
        catalog.snapshots.append({
            "version_index": vi,
            "version_label": version_labels[vi],
            "feed_url": feed.feed_url,
            "format": feed.format,
            "title": feed.title,
            "fetch_error": feed.fetch_error,
            "entry_count": len(feed.entries),
            "feed_identity_key": feed.feed_identity_key,
            "feed_identity_tier": feed.feed_identity_tier,
        })

    for fkey, group in feed_identities.items():
        first = group[0][1]
        catalog.feed_groups[fkey] = {
            "feed_identity_key": fkey,
            "title": first.title,
            "format": first.format,
            "identity_tier": first.feed_identity_tier,
            "versions": [vi for vi, _ in group],
            "urls": sorted({f.feed_url for _, f in group}),
            "self_link": first.self_link,
            "site_link": first.site_link,
            "identity_evidence": [
                {"tier": c.tier, "key": c.key, "explanation": c.explanation}
                for c in first.feed_identity_evidence
            ],
        }

    # 跨订阅源的 feed 身份冲突（不同 feed_identity_key 但 self/url 交叉）
    self_links: dict[str, list[str]] = {}
    for feed in feeds:
        if feed.self_link:
            self_links.setdefault(feed.self_link, []).append(feed.feed_identity_key)
    for link, keys in self_links.items():
        if len(set(keys)) > 1:
            catalog.conflicts.append(ConflictRecord(
                kind="cross_feed_identifier", severity="conflict",
                title="订阅源自身标识冲突",
                detail=(f"同一个 self 链接 {link} 出现在不同 feed 身份"
                        f"（{sorted(set(keys))}）下，两个订阅源均保留，不合并。"),
                identity_key=link, versions=[], entries=[],
            ))

    # 实体目录：以 (feed_identity_key, entry_identity_key) 归并。
    # 同一快照内重复身份的条目绝不互相覆盖：通过重复序号拆成独立目录条目，
    # 同时 duplicate_identity 冲突记录保留双方证据。
    index: dict[tuple[str, str, int], CatalogEntry] = {}
    seen_in_snapshot: dict[tuple[str, str], int] = {}
    for vi, feed in enumerate(feeds):
        catalog.conflicts += _detect_snapshot_conflicts(feed.entries, vi)
        snapshot_seen: set[tuple[str, str]] = set()
        for entry in feed.entries:
            base_key = (feed.feed_identity_key, entry.chosen_identity_key)
            dup_seq = seen_in_snapshot.get(base_key, 0)
            if base_key in snapshot_seen:
                # 同一快照内再次出现相同身份键：递增序号，保留为独立实体
                dup_seq += 1
                seen_in_snapshot[base_key] = dup_seq
            snapshot_seen.add(base_key)
            map_key = (base_key[0], base_key[1], dup_seq)
            enclosure_urls = [en.resolved_url for en in entry.enclosures]
            revision = RevisionRecord(
                version_index=vi,
                version_label=version_labels[vi],
                feed_url=entry.feed_url,
                title=entry.title,
                published=entry.published,
                updated=entry.updated,
                enclosure_urls=enclosure_urls,
                permalink=entry.permalinks[0] if entry.permalinks else None,
                fragment=entry.raw_fragment,
                identity_tier=entry.chosen_identity_tier,
            )
            existing = index.get(map_key)
            if existing is None:
                explanation = next(
                    (c.explanation for c in entry.identity_candidates
                     if c.key == entry.chosen_identity_key), "")
                existing = CatalogEntry(
                    identity_key=entry.chosen_identity_key,
                    identity_tier=entry.chosen_identity_tier,
                    identity_evidence_explanation=explanation,
                    feed_identity_key=feed.feed_identity_key,
                    title=entry.title,
                    first_seen_version=vi,
                    latest_version=vi,
                    cover_urls=[c.resolved_url for c in entry.covers],
                    raw_entry=None,
                )
                index[map_key] = existing
                catalog.entries.append(existing)

            # 媒体更换检测：身份不变、附件地址变化（含相对地址解析后变化）
            if existing.revisions:
                prev_urls = existing.revisions[-1].enclosure_urls
                for new_url in enclosure_urls:
                    if new_url not in prev_urls:
                        old_frag = existing.revisions[-1].fragment
                        new_frag = entry.raw_fragment
                        origin = (entry.enclosures[0].origin
                                  if entry.enclosures else "unknown")
                        existing.media_changes.append(MediaChange(
                            version_index=vi,
                            version_label=version_labels[vi],
                            enclosure_origin=origin,
                            old_url=prev_urls[0] if prev_urls else "(无附件)",
                            new_url=new_url,
                            old_fragment=old_frag,
                            new_fragment=new_frag,
                        ))
            existing.revisions.append(revision)
            existing.latest_version = vi
            existing.title = entry.title
            existing.current_enclosure_urls = enclosure_urls
            existing.current_permalinks = entry.permalinks
            existing.cover_urls = [c.resolved_url for c in entry.covers]
            from .models import to_jsonable
            existing.raw_entry = to_jsonable(entry)

    # 稳定排序：先订阅源，再首次出现版本与文档顺序
    catalog.entries.sort(key=lambda c: (c.feed_identity_key,
                                        c.first_seen_version, c.identity_key))
    return catalog
