"""解析器单元测试：xml:base 链、guid permalink 差异、Atom id、多链接关系。

断言基于测试内自行构造的 XML（独立真值），不读取样例生成器的 expected 字段。
"""
import unittest

from feedcollector.errors import FeedParseError
from feedcollector.parser import parse_feed

BASE = "http://lectures.local"


class XmlBaseTests(unittest.TestCase):
    def test_nested_xml_base_chain_resolves_relative_enclosure(self):
        xml = f"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xml:base="{BASE}/root/">
          <entry xml:base="feeds/">
            <id>urn:uuid:1</id>
            <title>t</title>
            <link xml:base="../media/" href="a.wav" rel="enclosure" type="audio/wav"/>
          </entry>
        </feed>"""
        feed = parse_feed(xml, BASE)
        enc = feed.entries[0].enclosures[0]
        self.assertEqual(enc.resolved_url, f"{BASE}/root/media/a.wav")
        # base 链证据必须完整保留三级
        self.assertEqual(enc.base_chain, [f"{BASE}/root/", "feeds/", "../media/"])
        self.assertEqual(enc.raw_url, "a.wav")

    def test_rss_enclosure_relative_url_uses_feed_url_fallback(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>x</title>
          <item><title>i</title><guid>g-1</guid>
          <enclosure url="audio/x.wav" type="audio/wav"/></item>
        </channel></rss>"""
        feed = parse_feed(xml, f"{BASE}/feeds/r.xml")
        self.assertEqual(
            feed.entries[0].enclosures[0].resolved_url,
            f"{BASE}/feeds/audio/x.wav")

    def test_relative_feed_link_self(self):
        xml = f"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xml:base="{BASE}/">
          <id>urn:uuid:f</id><title>f</title>
          <link href="feeds/self.xml" rel="self"/>
          <entry><id>urn:uuid:e</id><title>e</title></entry>
        </feed>"""
        feed = parse_feed(xml, BASE)
        self.assertEqual(feed.self_link, f"{BASE}/feeds/self.xml")


class GuidPermalinkTests(unittest.TestCase):
    def test_permalink_differs_from_link_both_retained(self):
        xml = f"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>c</title>
          <item>
            <title>海洋讲座</title>
            <link>{BASE}/page/loose-link</link>
            <guid isPermaLink="true">{BASE}/permalink/stable-9</guid>
            <enclosure url="{BASE}/m.wav" type="audio/wav"/>
          </item>
        </channel></rss>"""
        entry = parse_feed(xml, BASE).entries[0]
        self.assertIn(f"{BASE}/permalink/stable-9", entry.permalinks)
        self.assertIn(f"{BASE}/page/loose-link", entry.permalinks)
        # 选中身份是 P2 guid，而不是 link
        self.assertEqual(entry.chosen_identity_tier, 2)
        self.assertTrue(entry.chosen_identity_key.startswith("guid::"))
        guid_evidence = next(c for c in entry.identity_candidates if c.tier == 2)
        self.assertTrue(guid_evidence.is_permalink)

    def test_opaque_guid_is_tier2_but_not_url(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>c</title>
          <item><title>i</title><guid isPermaLink="false">opaque-123</guid></item>
        </channel></rss>"""
        entry = parse_feed(xml, BASE).entries[0]
        self.assertEqual(entry.chosen_identity_key, "guid::opaque-123")
        self.assertEqual(entry.chosen_identity_tier, 2)
        # 不透明 guid 不得被当作 permalink
        self.assertNotIn("opaque-123", entry.permalinks)

    def test_default_guid_is_treated_as_identifier_string(self):
        xml = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>c</title>
          <item><title>i</title><guid>https://example.test/x</guid></item>
        </channel></rss>"""
        entry = parse_feed(xml, BASE).entries[0]
        self.assertIsNone(
            next(c for c in entry.identity_candidates if c.tier == 2).is_permalink)
        self.assertEqual(entry.chosen_identity_tier, 2)


class AtomIdentityAndLinksTests(unittest.TestCase):
    def test_atom_id_is_p1(self):
        xml = f"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <id>urn:uuid:feed-1</id><title>f</title>
          <entry><id>urn:uuid:entry-7</id><title>e</title>
            <link href="{BASE}/x" rel="alternate"/></entry>
        </feed>"""
        entry = parse_feed(xml, BASE).entries[0]
        self.assertEqual(entry.chosen_identity_tier, 1)
        self.assertEqual(entry.chosen_identity_key, "atom-id::urn:uuid:entry-7")

    def test_multiple_link_relations_labeled(self):
        xml = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xml:base="%s/">
          <id>urn:uuid:f</id><title>f</title>
          <entry><id>urn:uuid:e</id><title>e</title>
            <link href="p" rel="alternate" type="text/html"/>
            <link href="m.mp3" rel="enclosure" type="audio/mpeg"/>
            <link href="r" rel="related"/>
            <link href="pay" rel="payment"/>
            <link href="edit" rel="edit"/>
            <link href="weird-rel"/>
          </entry>
        </feed>""" % BASE
        entry = parse_feed(xml, BASE).entries[0]
        rels = {l.rel for l in entry.links}
        self.assertEqual(
            rels, {"alternate", "enclosure", "related", "payment", "edit", "unknown"})
        enclosure = entry.enclosures[0]
        self.assertEqual(enclosure.origin, "atom:link[rel=enclosure]")
        self.assertEqual(enclosure.resolved_url, f"{BASE}/m.mp3")
        # 缺 rel 的链接必须显式标记 unknown，不允许臆测
        self.assertIn("未识别", next(l for l in entry.links if l.rel == "unknown").purpose_label)

    def test_feed_identity_priority_atom_id_over_self(self):
        xml = f"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <id>urn:uuid:the-feed</id><title>f</title>
          <link href="{BASE}/self.xml" rel="self"/>
          <entry><id>urn:uuid:e</id><title>e</title></entry>
        </feed>"""
        feed = parse_feed(xml, BASE)
        self.assertEqual(feed.feed_identity_tier, 1)
        self.assertEqual(feed.feed_identity_key, "atom-feed-id::urn:uuid:the-feed")


class FallbackFingerprintTests(unittest.TestCase):
    def test_entry_with_link_but_no_id_uses_permalink_tier3(self):
        # 有 link 但无 guid/id：permalink（P3）优先于结构指纹（P5）
        xml = f"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>c</title>
          <item><title>仅有链接的讲座</title><link>{BASE}/p</link>
          <enclosure url="{BASE}/m.wav" type="audio/wav"/></item>
        </channel></rss>"""
        entry = parse_feed(xml, BASE).entries[0]
        self.assertEqual(entry.chosen_identity_tier, 3)
        self.assertTrue(entry.chosen_identity_key.startswith("permalink::"))
        # P5 指纹仍作为候选证据保留
        self.assertTrue(any(c.tier == 5 for c in entry.identity_candidates))

    def test_entry_without_any_identifier_uses_tier5_fingerprint(self):
        # 既无 guid/id 也无 link：只能落到 P5
        xml = f"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>c</title>
          <item><title>无标识讲座</title><pubDate>Mon, 01 Jan 2024 00:00:00 +0800</pubDate>
          <enclosure url="{BASE}/m.wav" type="audio/wav"/></item>
        </channel></rss>"""
        entry = parse_feed(xml, BASE).entries[0]
        self.assertEqual(entry.chosen_identity_tier, 5)
        self.assertTrue(entry.chosen_identity_key.startswith("fp::sha256:"))

    def test_title_change_alone_does_not_change_guid_identity(self):
        template = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>c</title>
          <item><title>{title}</title><guid isPermaLink="false">same-guid</guid></item>
        </channel></rss>"""
        e1 = parse_feed(template.format(title="旧标题"), BASE).entries[0]
        e2 = parse_feed(template.format(title="新标题"), BASE).entries[0]
        self.assertEqual(e1.chosen_identity_key, e2.chosen_identity_key)
        self.assertNotEqual(e1.title, e2.title)


class RejectionTests(unittest.TestCase):
    def test_malformed_xml_raises(self):
        with self.assertRaises(FeedParseError):
            parse_feed("<rss><channel><title>x", BASE)

    def test_unsupported_root_raises(self):
        with self.assertRaises(FeedParseError):
            parse_feed(
                '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>',
                BASE)

    def test_atom_link_without_href_raises(self):
        xml = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <id>f</id><title>f</title>
          <entry><id>e</id><link rel="alternate"/></entry>
        </feed>"""
        with self.assertRaises(FeedParseError):
            parse_feed(xml, BASE)


if __name__ == "__main__":
    unittest.main()
