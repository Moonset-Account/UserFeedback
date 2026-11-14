"""Rendering and DOM-text fidelity tests."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from support import SAMPLE_DATA  # noqa: F401  (also sets sys.path)

from diagnostics import dom
from diagnostics.link_policy import LinkPolicy
from diagnostics.model import Dataset, DocLink, Record, load_dataset, parse_dataset
from diagnostics.render import build_report, render_page, split_highlights

POLICY = LinkPolicy(["docs.example.com"])

MIXED = Dataset(
    title="诊断 <title> 检查",
    records=(
        Record(
            record_id="r1",
            group="组 <一>",
            summary="设备返回 <error code=\"E42\"> & 'single' \"double\"",
            details="line1 <tag> & stuff\nline2\n  indented",
            doc=DocLink(label="说明 <E42>", url="https://docs.example.com/e42"),
        ),
        Record(
            record_id="r2",
            group="组 <一>",
            summary="日本語 тексты العربية 🛠️",
            details=None,
            doc=DocLink(label="坏链接", url="https://user:pass@docs.example.com/x"),
        ),
    ),
)


def render(query: str | None = None) -> str:
    return render_page(MIXED, POLICY, query=query)


class TextIsPreservedTests(unittest.TestCase):
    """Rendered textContent must equal the dataset string exactly."""

    def test_title_and_group_text_preserved(self) -> None:
        root = dom.parse_html(render())
        self.assertEqual(dom.text_content(dom.find_all(root, "h1")[0]), MIXED.title)
        headings = [dom.text_content(h) for h in dom.find_all(root, "h2")]
        self.assertEqual(headings, ["组 <一>"])

    def test_summary_and_details_preserved(self) -> None:
        root = dom.parse_html(render())
        articles = dom.find_all(root, "article")
        first, second = articles
        self.assertEqual(dom.text_content(dom.find_all(first, "p", class_="summary")[0]),
                         MIXED.records[0].summary)
        self.assertEqual(dom.text_content(dom.find_all(first, "pre")[0]),
                         MIXED.records[0].details)
        self.assertEqual(dom.text_content(dom.find_all(second, "p", class_="summary")[0]),
                         MIXED.records[1].summary)

    def test_markup_characters_are_escaped_in_source(self) -> None:
        markup = render()
        self.assertNotIn("<error", markup)
        self.assertNotIn("<tag>", markup)
        self.assertIn("&lt;error", markup)

    def test_script_payload_is_inert_text(self) -> None:
        dataset = Dataset(
            title="t",
            records=(
                Record("r", "g", "<script>alert(1)</script>", None,
                       DocLink("label", "javascript:alert(1)")),
            ),
        )
        markup = render_page(dataset, POLICY)
        root = dom.parse_html(markup)
        self.assertEqual(dom.find_all(root, "script"), [])
        self.assertEqual(dom.find_all(root, "a"), [])
        summary = dom.find_all(root, "p", class_="summary")[0]
        self.assertEqual(dom.text_content(summary), "<script>alert(1)</script>")

    def test_attribute_breaking_label_cannot_escape_anchor(self) -> None:
        dataset = Dataset(
            title="t",
            records=(
                Record("r", "g", "s", None,
                       DocLink('x"><img src=x onerror=alert(1)>', "https://docs.example.com/a")),
            ),
        )
        root = dom.parse_html(render_page(dataset, POLICY))
        self.assertEqual(dom.find_all(root, "img"), [])
        anchor = dom.find_all(root, "a", class_="doc-link")[0]
        self.assertEqual(dom.text_content(anchor), 'x"><img src=x onerror=alert(1)>')
        self.assertEqual(anchor.get("href"), "https://docs.example.com/a")

    def test_pre_leading_newline_survives(self) -> None:
        dataset = Dataset(
            title="t",
            records=(Record("r", "g", "s", "\nfirst\nsecond", None),),
        )
        root = dom.parse_html(render_page(dataset, POLICY))
        self.assertEqual(dom.text_content(dom.find_all(root, "pre")[0]), "\nfirst\nsecond")


class HighlightTests(unittest.TestCase):
    def test_highlight_splits_preserve_characters(self) -> None:
        segments = split_highlights("a<b>c", "b")
        self.assertEqual("".join(s.text for s in segments), "a<b>c")

    def test_highlight_case_insensitive_uses_original_text(self) -> None:
        segments = split_highlights("Value VALUE value", "value")
        self.assertEqual([(s.text, s.match) for s in segments], [
            ("Value", True), (" ", False), ("VALUE", True), (" ", False), ("value", True),
        ])

    def test_highlight_across_non_latin(self) -> None:
        text = "ошибка «Е42» — テスト Е42"
        segments = split_highlights(text, "Е42")
        self.assertEqual("".join(s.text for s in segments), text)
        self.assertEqual([s.text for s in segments if s.match], ["Е42", "Е42"])

    def test_highlight_dotted_capital_i_keeps_length(self) -> None:
        # "İ" lowercases to two code points; spans must still come from the source.
        text = "İstanbul"
        segments = split_highlights(text, "istanbul")
        self.assertEqual("".join(s.text for s in segments), text)

    def test_highlight_special_regex_chars_are_literal(self) -> None:
        text = "a.b*c (x)"
        segments = split_highlights(text, ".b*")
        self.assertEqual("".join(s.text for s in segments), text)
        self.assertEqual([s.text for s in segments if s.match], [".b*"])

    def test_highlight_does_not_alter_dom_text(self) -> None:
        markup = render(query="error")
        root = dom.parse_html(markup)
        first = dom.find_all(root, "article")[0]
        summary = dom.find_all(first, "p", class_="summary")[0]
        self.assertEqual(dom.text_content(summary), MIXED.records[0].summary)
        marks = dom.find_all(summary, "mark")
        self.assertEqual([dom.text_content(m) for m in marks], ["error"])
        self.assertEqual(dom.text_content(dom.find_all(first, "pre")[0]), MIXED.records[0].details)

    def test_highlight_escaped_markup_text(self) -> None:
        markup = render(query="<error")
        root = dom.parse_html(markup)
        first = dom.find_all(root, "article")[0]
        marks = dom.find_all(dom.find_all(first, "p", class_="summary")[0], "mark")
        self.assertEqual([dom.text_content(m) for m in marks], ["<error"])

    def test_query_echoed_into_banner_is_escaped(self) -> None:
        markup = render(query='"><script>alert(1)</script>')
        root = dom.parse_html(markup)
        self.assertEqual(dom.find_all(root, "script"), [])
        banner = dom.find_all(root, "p", class_="build-note")[0]
        self.assertIn('"><script>alert(1)</script>', dom.text_content(banner))

    def test_no_script_element_anywhere(self) -> None:
        root = dom.parse_html(render())
        self.assertEqual(dom.find_all(root, "script"), [])

    def test_empty_query_adds_no_marks(self) -> None:
        root = dom.parse_html(render(query="   "))
        self.assertEqual(dom.find_all(root, "mark"), [])


class LinkRenderingTests(unittest.TestCase):
    def test_allowed_link_is_anchor_with_parsed_host(self) -> None:
        root = dom.parse_html(render())
        anchors = dom.find_all(root, "a", class_="doc-link")
        self.assertEqual(len(anchors), 1)
        anchor = anchors[0]
        self.assertEqual(anchor.get("href"), "https://docs.example.com/e42")
        self.assertEqual(anchor.get("data-doc-host"), "docs.example.com")
        self.assertIn("noopener", (anchor.get("rel") or ""))
        self.assertEqual(dom.text_content(anchor), "说明 <E42>")

    def test_credentialed_link_is_plain_text_and_record_survives(self) -> None:
        root = dom.parse_html(render())
        articles = dom.find_all(root, "article")
        self.assertEqual(len(articles), 2)
        blocked = dom.find_all(articles[1], "p", class_="doc-blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].get("data-doc-reason"), "credentials")
        self.assertEqual(dom.find_all(articles[1], "a"), [])
        self.assertEqual(
            dom.text_content(dom.find_all(articles[1], "p", class_="summary")[0]),
            MIXED.records[1].summary,
        )
        self.assertIn(
            "https://user:pass@docs.example.com/x",
            dom.text_content(blocked[0]),
        )


class SampleDatasetTests(unittest.TestCase):
    """End-to-end assertions on the shipped sample, read from the DOM."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(SAMPLE_DATA)
        cls.markup = render_page(cls.dataset, LinkPolicy(["docs.example.com"]))
        cls.report = build_report(cls.markup)
        cls.records = {r.record_id: r for r in cls.dataset.records}

    def test_all_records_rendered_in_order(self) -> None:
        ids = [r["id"] for group in self.report["groups"] for r in group["records"]]
        self.assertEqual(ids, [r.record_id for r in self.dataset.records])
        self.assertEqual(self.report["record_count"], len(self.dataset.records))

    def test_angle_bracket_record_round_trips(self) -> None:
        found = dom.find_by_id(dom.parse_html(self.markup),
                               "record-rec-html-angle-brackets")
        assert found is not None
        record = self.records["rec-html-angle-brackets"]
        self.assertEqual(dom.text_content(dom.find_all(found, "p", class_="summary")[0]),
                         record.summary)
        self.assertEqual(dom.text_content(dom.find_all(found, "pre")[0]), record.details)

    def test_non_latin_record_round_trips(self) -> None:
        found = dom.find_by_id(dom.parse_html(self.markup), "record-rec-non-latin")
        assert found is not None
        record = self.records["rec-non-latin"]
        self.assertEqual(dom.text_content(dom.find_all(found, "p", class_="summary")[0]),
                         record.summary)
        self.assertEqual(dom.text_content(dom.find_all(found, "pre")[0]), record.details)

    def test_only_allowlisted_hosts_are_anchored(self) -> None:
        for href in self.report["anchors"]:
            with self.subTest(href=href):
                decision = LinkPolicy(["docs.example.com"]).check(href)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.host, "docs.example.com")

    def test_blocked_links_report_reasons(self) -> None:
        expected = {
            "rec-link-userinfo": "credentials",
            "rec-link-evil-suffix": "host_not_allowed",
            "rec-link-http": "scheme_not_https",
            "rec-link-ip-literal": "host_not_allowed",
            "rec-link-tab-bypass": "control_chars",
        }
        docs = {
            r["id"]: r["doc"] for group in self.report["groups"] for r in group["records"]
        }
        for record_id, reason in expected.items():
            with self.subTest(record_id=record_id):
                doc = docs[record_id]
                self.assertEqual(doc["kind"], "plain-text")
                self.assertEqual(doc["reason"], reason)
                self.assertEqual(doc["url_text"], self.records[record_id].doc.url)

    def test_valid_links_remain_anchors_beside_blocked_ones(self) -> None:
        anchors = {
            r["id"]: r["doc"]["href"]
            for group in self.report["groups"]
            for r in group["records"]
            if r["doc"] and r["doc"]["kind"] == "anchor"
        }
        self.assertEqual(anchors, {
            "rec-html-angle-brackets": self.records["rec-html-angle-brackets"].doc.url,
            "rec-non-latin": self.records["rec-non-latin"].doc.url,
            "rec-link-uppercase-host": self.records["rec-link-uppercase-host"].doc.url,
            "rec-link-trailing-dot": self.records["rec-link-trailing-dot"].doc.url,
        })

    def test_sample_search_highlights_without_changing_text(self) -> None:
        markup = render_page(self.dataset, LinkPolicy(["docs.example.com"]), query="E42")
        root = dom.parse_html(markup)
        for record in self.dataset.records:
            node = dom.find_by_id(root, f"record-{record.record_id}")
            assert node is not None
            self.assertEqual(
                dom.text_content(dom.find_all(node, "p", class_="summary")[0]),
                record.summary,
                msg=record.record_id,
            )
            if record.details is not None:
                self.assertEqual(
                    dom.text_content(dom.find_all(node, "pre")[0]),
                    record.details,
                    msg=record.record_id,
                )
        total_marks = len(dom.find_all(root, "mark"))
        self.assertGreater(total_marks, 0)


class DataValidationTests(unittest.TestCase):
    def test_duplicate_ids_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_dataset({"records": [
                {"id": "a", "group": "g", "summary": "s"},
                {"id": "a", "group": "g", "summary": "s"},
            ]})

    def test_missing_required_field_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_dataset({"records": [{"id": "a", "summary": "s"}]})

    def test_null_byte_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_dataset({"records": [{"id": "a", "group": "g", "summary": "x\x00y"}]})

    def test_doc_null_is_allowed(self) -> None:
        dataset = parse_dataset({"records": [
            {"id": "a", "group": "g", "summary": "s", "doc": None},
        ]})
        self.assertIsNone(dataset.records[0].doc)


if __name__ == "__main__":
    unittest.main()
