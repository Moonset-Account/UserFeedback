"""端到端测试：启动本地源站 → 经 HTTP 真实采集 → 对照独立真值断言。

测试中的期望值来自 samplegen 清单的 expected_*（仅供测试的独立真值），
采集器运行时不读取这些字段。
"""
import tempfile
import unittest
from pathlib import Path

from samplegen.scenarios import generate_fixtures
from samplegen.source_server import build_server

from feedcollector.collector import run_collection
from feedcollector.fetcher import probe_url


class EndToEndFixtureTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="feed-e2e-")
        cls.server = build_server(cls._tmp.name, slow_delay=6.0)
        cls.server._thread.start()
        cls.manifest = generate_fixtures(cls._tmp.name, cls.server.base_url)
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._tmp.cleanup()

    def _run(self, scenario_key, **kwargs):
        sc = self.manifest["scenarios"][scenario_key]
        return run_collection(
            sc["sources"], scenario=scenario_key,
            timeout=kwargs.pop("timeout", sc.get("timeout", 5.0)),
            **kwargs)


class NormalScenarioTests(EndToEndFixtureTestBase):
    def setUp(self):
        self.report = self._run("demo-normal")
        self.expected = self.manifest["scenarios"]["demo-normal"]["expected"]

    def test_status_success_and_entity_count(self):
        self.assertEqual(self.report["status"], "success")
        self.assertEqual(len(self.report["catalog"]["entries"]),
                         self.expected["catalog_entity_count"])
        self.assertEqual(self.report["errors"], [])

    def test_all_enclosures_available(self):
        for feed in self.report["feeds"]:
            for entry in feed["feed"]["entries"]:
                for enc in entry["enclosures"]:
                    self.assertTrue(
                        enc["available"],
                        f"附件应可用但探测失败：{enc['resolved_url']} / {enc['error']}")

    def test_duplicate_titles_kept_as_distinct_entities(self):
        warnings = [c for c in self.report["catalog"]["conflicts"]
                    if c["kind"] == "duplicate_title"]
        self.assertEqual(len(warnings), self.expected["duplicate_title_pairs"])
        titles = [e["title"] for e in self.report["catalog"]["entries"]]
        self.assertEqual(titles.count("量子纠错入门"), 2)
        keys = {e["identity_key"] for e in self.report["catalog"]["entries"]
                if e["title"] == "量子纠错入门"}
        self.assertEqual(len(keys), 2)

    def test_permalink_mismatch_both_urls_preserved(self):
        target = self.expected["permalink_mismatch_entry_guid"]
        entity = next(e for e in self.report["catalog"]["entries"]
                      if e["identity_key"] == target)
        all_links = set(entity["current_permalinks"])
        self.assertIn(f"{self.base}/permalink/ocean-stable-022", all_links)
        self.assertIn(f"{self.base}/site/talks/ocean-page", all_links)

    def test_atom_multi_relation_links_classified(self):
        atom_feed = next(f for f in self.report["feeds"]
                         if f["feed"]["format"] == "Atom 1.0")
        uhi = next(e for e in atom_feed["feed"]["entries"]
                   if e["identity_candidates"][0]["key"]
                   == self.expected["atom_multi_relation_entry_id"])
        rels = {l["rel"] for l in uhi["links"]}
        self.assertEqual(
            rels, {"alternate", "enclosure", "related", "via", "payment"})

    def test_relative_enclosure_resolved_through_xml_base(self):
        atom_feed = next(f for f in self.report["feeds"]
                         if f["feed"]["format"] == "Atom 1.0")
        urls = {enc["resolved_url"]
                for e in atom_feed["feed"]["entries"]
                for enc in e["enclosures"]}
        # 根 xml:base + 相对 href（uhi 条目）
        self.assertIn(f"{self.base}/media/uhi-001.mp3", urls)
        # 条目级 xml:base + 元素级 xml:base 的两级解析（battery 条目）
        battery = next(enc for e in atom_feed["feed"]["entries"]
                       for enc in e["enclosures"]
                       if enc["resolved_url"].endswith("battery-002.wav"))
        self.assertEqual(battery["resolved_url"],
                         f"{self.base}/media/battery-002.wav")
        self.assertEqual(battery["base_chain"][-1], "../media/")

    def test_fingerprint_fallback_for_identifierless_entry(self):
        # 午间沙龙条目无 guid，但有 link，因此按设计落到 P3 permalink；
        # 真正无任何标识的 P5 场景见 test_parser 的专门用例
        rss_feed = next(f for f in self.report["feeds"]
                        if f["feed"]["format"] == "RSS 2.0")
        no_id = next(e for e in rss_feed["feed"]["entries"]
                     if "沙龙" in (e["title"] or ""))
        self.assertEqual(no_id["chosen_identity_tier"], 3)
        self.assertTrue(any(c["tier"] == 5 for c in no_id["identity_candidates"]))

    def test_feed_identity_uses_atom_id(self):
        atom_feed = next(f for f in self.report["feeds"]
                         if f["feed"]["format"] == "Atom 1.0")["feed"]
        self.assertEqual(atom_feed["feed_identity_tier"], 1)
        self.assertTrue(atom_feed["feed_identity_key"].startswith("atom-feed-id::"))

    def test_cover_evidence_retained(self):
        rss_feed = next(f for f in self.report["feeds"]
                        if f["feed"]["format"] == "RSS 2.0")["feed"]
        self.assertTrue(any(c["owner"] == "feed" for c in rss_feed["covers"]))
        qec = next(e for e in rss_feed["entries"] if e["title"] == "量子纠错入门（上）")
        self.assertTrue(qec["covers"])
        self.assertTrue(qec["covers"][0]["available"])

    def test_raw_fragments_are_evidence(self):
        for feed in self.report["feeds"]:
            self.assertIn("<", feed["feed"]["raw_document"])
            for entry in feed["feed"]["entries"]:
                self.assertIn("<", entry["raw_fragment"])


class EdgeScenarioTests(EndToEndFixtureTestBase):
    def setUp(self):
        self.report = self._run("demo-edge")
        self.expected = self.manifest["scenarios"]["demo-edge"]["expected"]
        self.conflicts = self.report["catalog"]["conflicts"]

    def test_duplicate_identity_conflicts_both_retained(self):
        dup = [c for c in self.conflicts if c["kind"] == "duplicate_identity"]
        self.assertEqual(len(dup), self.expected["duplicate_identity_conflicts"])
        keys = [e["identity_key"] for e in self.report["catalog"]["entries"]]
        self.assertEqual(keys.count(self.expected["collision_identity_key"]), 2)
        atom_dup = [k for k in keys if k == "atom-id::urn:uuid:fictional-dup-atom-id"]
        self.assertEqual(len(atom_dup), 2)

    def test_identifier_overlap_conflict(self):
        overlap = [c for c in self.conflicts if c["kind"] == "identifier_overlap"]
        self.assertEqual(len(overlap),
                         self.expected["identifier_overlap_conflicts"])
        # 重叠双方仍各自独立建目（RSS 10 条 + Atom 3 条）
        self.assertEqual(len(self.report["catalog"]["entries"]), 13)

    def test_identifierless_entry_uses_p5_fingerprint(self):
        rss = next(f for f in self.report["feeds"]
                   if f["feed"]["format"] == "RSS 2.0")["feed"]
        no_id = next(e for e in rss["entries"]
                     if "结构指纹兜底" in (e["title"] or ""))
        self.assertEqual(no_id["chosen_identity_tier"], 5)
        self.assertTrue(no_id["chosen_identity_key"].startswith("fp::"))

    def test_duplicate_title_warning(self):
        warnings = [c for c in self.conflicts if c["kind"] == "duplicate_title"]
        self.assertEqual(len(warnings),
                         self.expected["duplicate_title_warnings"])

    def test_relative_enclosure_in_conflict_feed(self):
        urls = {u for e in self.report["catalog"]["entries"]
                for u in e["current_enclosure_urls"]}
        self.assertIn(self.expected["relative_enclosure_resolves_to"], urls)

    def test_unavailable_media_recorded_with_status_codes(self):
        errors = self.report["errors"]
        bad_urls = {e["source"] for e in errors
                    if e["stage"] == "enclosure_probe"}
        for url in self.expected["unavailable_enclosure_urls"]:
            self.assertIn(url, bad_urls)
        statuses = set()
        for feed in self.report["feeds"]:
            for entry in feed["feed"]["entries"]:
                for enc in entry["enclosures"]:
                    if enc["available"] is False:
                        statuses.add(enc["http_status"])
        self.assertEqual(set(self.expected["unavailable_status_codes"]), statuses)

    def test_unavailable_cover_also_recorded(self):
        rss = next(f for f in self.report["feeds"]
                   if f["feed"]["format"] == "RSS 2.0")["feed"]
        entry = next(e for e in rss["entries"] if "不可用" in (e["title"] or ""))
        cover = next(c for c in entry["covers"] if "cover-missing" in c["resolved_url"])
        self.assertFalse(cover["available"])
        self.assertEqual(cover["http_status"], 404)

    def test_no_silent_overwrite_flag(self):
        # 每个冲突都必须携带双方片段证据
        for conflict in self.conflicts:
            if conflict["severity"] == "conflict":
                self.assertGreaterEqual(len(conflict["entries"]), 2)
                for entry in conflict["entries"]:
                    self.assertIn("<", entry["raw_fragment"])


class FullAcceptanceTests(EndToEndFixtureTestBase):
    def setUp(self):
        self.report = self._run("full-acceptance")
        self.expected = self.manifest["scenarios"]["full-acceptance"]["expected"]

    def test_parsed_and_error_counts(self):
        parsed = [f for f in self.report["feeds"] if f.get("parse_ok")]
        self.assertEqual(len(parsed), self.expected["parsed_feed_count"])
        self.assertEqual(len(self.report["errors"]),
                         self.expected["fetch_or_parse_error_count"])

    def test_timeout_fetch_is_recorded_as_error(self):
        err = next(e for e in self.report["errors"] if "超时" in e["message"])
        self.assertEqual(err["stage"], "fetch")

    def test_malformed_and_unknown_protocol_rejected(self):
        messages = " ".join(e["message"] for e in self.report["errors"])
        self.assertIn("XML 语法", messages)
        self.assertIn("协议子集", messages)

    def test_missing_feed_404(self):
        err = next(e for e in self.report["errors"]
                   if "missing-feed" in e.get("source", ""))
        self.assertIn("404", err["message"])

    def test_media_url_change_entity_unchanged(self):
        entity = next(e for e in self.report["catalog"]["entries"]
                      if e["identity_key"] == self.expected["stable_guid"])
        self.assertEqual(len(entity["revisions"]),
                         self.expected["stable_entity_revision_count"])
        self.assertEqual(len(entity["media_changes"]),
                         self.expected["stable_entity_media_change_count"])
        change = entity["media_changes"][0]
        self.assertEqual(change["old_url"], self.expected["v1_media_url"])
        self.assertEqual(change["new_url"], self.expected["v2_media_url"])
        self.assertEqual(entity["revisions"][0]["title"], "可重复讲座（初稿标题）")
        self.assertEqual(entity["revisions"][1]["title"], "可重复讲座（定稿标题）")

    def test_correction_version_merged_as_revisions(self):
        entity = next(e for e in self.report["catalog"]["entries"]
                      if e["identity_key"] == self.expected["correction_guid"])
        self.assertEqual(len(entity["revisions"]),
                         self.expected["correction_revision_count"])
        self.assertIn("勘误", entity["revisions"][1]["title"])

    def test_cross_feed_identifier_conflict(self):
        conflicts = [c for c in self.report["catalog"]["conflicts"]
                     if c["kind"] == "cross_feed_identifier"]
        self.assertEqual(len(conflicts),
                         self.expected["cross_feed_identifier_conflicts"])
        # 两个镜像源的条目都保留
        ids = {e["identity_key"] for e in self.report["catalog"]["entries"]}
        self.assertIn("atom-id::urn:uuid:fictional-mirror-alpha-entry", ids)
        self.assertIn("atom-id::urn:uuid:fictional-mirror-beta-entry", ids)


class FetcherProbeTests(EndToEndFixtureTestBase):
    def test_head_not_allowed_falls_back_to_range_get(self):
        result = probe_url(f"{self.base}/error/head-not-allowed.mp3", timeout=5)
        self.assertTrue(result.available)
        self.assertEqual(result.method, "GET")
        self.assertEqual(result.http_status, 200)

    def test_503_reported_unavailable(self):
        result = probe_url(f"{self.base}/error/503.mp3", timeout=5)
        self.assertFalse(result.available)
        self.assertEqual(result.http_status, 503)

    def test_local_file_probe(self):
        from samplegen.mediafactory import write_wav
        path = Path(self._tmp.name) / "local-probe.wav"
        write_wav(path)
        result = probe_url(str(path), timeout=5)
        self.assertTrue(result.available)
        self.assertEqual(result.method, "FILE")


if __name__ == "__main__":
    unittest.main()
