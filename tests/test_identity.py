"""身份归并与冲突检测单元测试（直接构造 ParsedEntry，独立于样例生成器）。"""
import unittest

from feedcollector.identity import build_catalog
from feedcollector.models import (
    EnclosureRecord, IdentityEvidence, ParsedEntry, ParsedFeed,
)


def _evidence(tier: int, key: str, explanation: str = "") -> IdentityEvidence:
    return IdentityEvidence(
        tier=tier, key=key, raw_value=key, element=f"tier{tier}",
        is_permalink=None, explanation=explanation or f"P{tier} 测试线索",
        source_fragment=f"<-- {key} -->")


def _entry(idx: int, identity: str, tier: int, title: str,
           enclosures: list[str] | None = None,
           extra_candidates: list[IdentityEvidence] | None = None) -> ParsedEntry:
    cands = [_evidence(tier, identity)]
    if extra_candidates:
        cands += extra_candidates
    cands.sort(key=lambda c: c.tier)
    return ParsedEntry(
        feed_url="http://f/feed.xml", feed_identity_key="self::http://f/feed.xml",
        feed_identity_tier=2, entry_index=idx, title=title,
        published=None, updated=None, authors=[], summary=None,
        links=[],
        enclosures=[EnclosureRecord(
            raw_url=u, resolved_url=u, media_type="audio/wav", length=None,
            origin="test", base_chain=[], source_fragment=f"<e>{u}</e>")
            for u in (enclosures or [])],
        covers=[], identity_candidates=cands,
        chosen_identity_key=identity, chosen_identity_tier=tier,
        permalinks=[], structural_fingerprint="fp::test",
        raw_fragment=f"<item>{title}</item>", xml_base=None)


def _feed(entries: list[ParsedEntry], url: str = "http://f/feed.xml") -> ParsedFeed:
    return ParsedFeed(
        feed_url=url, format="RSS 2.0",
        feed_identity_key="self::http://f/feed.xml", feed_identity_tier=2,
        feed_identity_evidence=[_evidence(2, "self::http://f/feed.xml")],
        title="测试订阅源", self_link="http://f/feed.xml", site_link=None,
        declared_base=None, links=[], covers=[], entries=entries)


class TitleAndMediaUrlTests(unittest.TestCase):
    def test_same_title_different_identities_are_separate_entities(self):
        feed = _feed([
            _entry(0, "guid::a", 2, "同名讲座", ["http://f/a.wav"]),
            _entry(1, "guid::b", 2, "同名讲座", ["http://f/b.wav"]),
        ])
        cat = build_catalog([feed])
        self.assertEqual(len(cat.entries), 2)
        warning = next(c for c in cat.conflicts if c.kind == "duplicate_title")
        self.assertEqual(warning.severity, "warning")
        self.assertEqual(len(warning.entries), 2)

    def test_media_url_change_keeps_identity_and_records_change(self):
        v1 = _feed([_entry(0, "guid::x", 2, "讲座", ["http://f/old.wav"])])
        v2 = _feed([_entry(0, "guid::x", 2, "讲座（修订）", ["http://f/new.wav"])])
        cat = build_catalog([v1, v2], ["v1", "v2"])
        self.assertEqual(len(cat.entries), 1)
        entity = cat.entries[0]
        self.assertEqual(len(entity.revisions), 2)
        self.assertEqual(len(entity.media_changes), 1)
        change = entity.media_changes[0]
        self.assertEqual(change.old_url, "http://f/old.wav")
        self.assertEqual(change.new_url, "http://f/new.wav")

    def test_same_media_url_different_identities_are_separate(self):
        feed = _feed([
            _entry(0, "guid::a", 2, "讲座A", ["http://f/shared.wav"]),
            _entry(1, "guid::b", 2, "讲座B", ["http://f/shared.wav"]),
        ])
        cat = build_catalog([feed])
        self.assertEqual(len(cat.entries), 2)


class ConflictTests(unittest.TestCase):
    def test_duplicate_identity_in_one_snapshot_both_kept(self):
        feed = _feed([
            _entry(0, "guid::dup", 2, "冲突A"),
            _entry(1, "guid::dup", 2, "冲突B"),
        ])
        cat = build_catalog([feed])
        # 关键验收点：不静默覆盖，两个实体都在
        self.assertEqual(len(cat.entries), 2)
        conflict = next(c for c in cat.conflicts if c.kind == "duplicate_identity")
        self.assertEqual(conflict.severity, "conflict")
        self.assertEqual({e["title"] for e in conflict.entries}, {"冲突A", "冲突B"})

    def test_identifier_overlap_detected(self):
        # C 选中 guid::C，但它的 P3 permalink 线索等于 D 的普通 link
        c = _entry(0, "guid::C", 2, "C",
                   extra_candidates=[_evidence(3, "permalink::http://f/U")])
        d = _entry(1, "permalink::http://f/U", 3, "D")
        cat = build_catalog([_feed([c, d])])
        overlap = next(cf for cf in cat.conflicts if cf.kind == "identifier_overlap")
        self.assertEqual(overlap.severity, "conflict")
        self.assertEqual(len(cat.entries), 2)  # 不合并

    def test_revision_chain_preserves_both_versions(self):
        v1 = _feed([_entry(0, "guid::r", 2, "初稿", ["http://f/1.wav"])])
        v2 = _feed([_entry(0, "guid::r", 2, "勘误", ["http://f/1.wav"])])
        cat = build_catalog([v1, v2], ["一版", "二版"])
        entity = next(e for e in cat.entries if e.identity_key == "guid::r")
        self.assertEqual([rv.version_label for rv in entity.revisions], ["一版", "二版"])
        self.assertEqual(entity.revisions[0].title, "初稿")
        self.assertEqual(entity.revisions[1].title, "勘误")
        self.assertEqual(len(entity.media_changes), 0)


if __name__ == "__main__":
    unittest.main()
