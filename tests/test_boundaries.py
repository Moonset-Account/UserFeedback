"""Acceptance: boundaries where a range only touches a speech segment.

These cases separate "overlaps" from "merely touches": a range that ends
exactly where a segment starts, or starts exactly where one ends, must leave
that segment alive.  A range straddling several segments removes each of them.
"""

from __future__ import annotations

import unittest

from docredact import Document, extract_page_text
from tests import support

TITLE = "Project Aurora"
BODY = "launches in Q3."
OWNER = "Owner: "
EMAIL = "alice@example.com"


class SpeechBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())
        self.text = support.COPY_TEXT
        self.title = support.COPY_SPANS[TITLE]
        self.body = support.COPY_SPANS[BODY]
        self.owner = support.COPY_SPANS[OWNER]
        self.email = support.COPY_SPANS[EMAIL]

    def _redact(self, start: int, end: int):
        return self.document.redact("p1", ("main", "body"), start, end)

    def _node_text(self, result) -> dict:
        return result.document.objects[result.new_text_id]

    def test_fixture_segments_leave_single_code_point_gaps(self) -> None:
        # "Project Aurora" | " " | "launches in Q3." | " " | "Owner: " | "alice..."
        self.assertEqual(self.title[1] + 1, self.body[0])
        self.assertEqual(self.body[1] + 1, self.owner[0])
        self.assertEqual(self.owner[1], self.email[0])  # no gap before the address

    def test_range_ending_before_next_segment_start_keeps_it(self) -> None:
        result = self._redact(0, self.title[1])
        self.assertEqual([s["utterance"] for s in result.removed_speech], [TITLE])
        node = self._node_text(result)
        body = next(s for s in node["speech"] if s["utterance"] == BODY)
        self.assertEqual(node["text"][body["start"]:body["end"]], BODY)
        self.assertEqual(body["start"], 1)  # shifted from 15 by 14

    def test_range_starting_after_previous_segment_end_keeps_it(self) -> None:
        result = self._redact(self.body[0], self.body[1])
        self.assertEqual([s["utterance"] for s in result.removed_speech], [BODY])
        node = self._node_text(result)
        title = next(s for s in node["speech"] if s["utterance"] == TITLE)
        self.assertEqual((title["start"], title["end"]), self.title)

    def test_range_ending_exactly_at_segment_start_keeps_the_segment(self) -> None:
        # Remove the gap plus "Owner: " but stop exactly at the address start.
        result = self._redact(self.owner[0], self.email[0])
        self.assertEqual([s["utterance"] for s in result.removed_speech], ["Owner"])
        node = self._node_text(result)
        email = next(s for s in node["speech"] if s["utterance"] == "alice at example dot com")
        self.assertEqual(node["text"][email["start"]:email["end"]], EMAIL)
        self.assertEqual(email["start"], self.owner[0])

    def test_range_starting_exactly_at_segment_end_keeps_the_segment(self) -> None:
        # Remove the address; "Owner: " ends exactly where it begins.
        result = self._redact(self.email[0], self.email[1])
        self.assertEqual([s["utterance"] for s in result.removed_speech], ["alice at example dot com"])
        node = self._node_text(result)
        owner = next(s for s in node["speech"] if s["utterance"] == "Owner")
        self.assertEqual(node["text"][owner["start"]:owner["end"]], OWNER)
        self.assertEqual(node["text"], support.copy_without(EMAIL))

    def test_range_straddling_segments_removes_each_touched_segment(self) -> None:
        result = self._redact(self.body[0] + 1, self.email[0] + 1)
        self.assertEqual(
            [s["utterance"] for s in result.removed_speech],
            [BODY, "Owner", "alice at example dot com"],
        )
        node = self._node_text(result)
        self.assertEqual([s["utterance"] for s in node["speech"]], [TITLE])
        # Removing [16, 39) leaves "Project Aurora l" + "lice@example.com".
        self.assertEqual(
            extract_page_text(result.document, "p1"),
            support.p1_text(copy="Project Aurora llice@example.com"),
        )

    def test_range_covering_every_segment_leaves_no_speech(self) -> None:
        result = self._redact(0, len(self.text))
        node = self._node_text(result)
        self.assertEqual(node["text"], "")
        self.assertEqual(node["speech"], [])
        self.assertEqual(len(result.removed_speech), 4)
        self.assertEqual(extract_page_text(result.document, "p1"), support.HEAD_TEXT * 2)
        self.assertEqual(extract_page_text(result.document, "p2"), support.p2_text())

    def test_range_wholly_between_segments_removes_none(self) -> None:
        gap = self.title[1]  # the single space between title and body
        result = self._redact(gap, gap + 1)
        self.assertEqual(result.removed_speech, ())
        node = self._node_text(result)
        body = next(s for s in node["speech"] if s["utterance"] == BODY)
        self.assertEqual(node["text"][body["start"]:body["end"]], BODY)

    def test_astral_neighbourhood_boundary(self) -> None:
        # A segment starting immediately after an astral character survives
        # deleting the character and shifts by exactly one code point.
        text = "\U0001D11E" + "秘密"
        document = Document(
            {
                "t": {
                    "kind": "text",
                    "text": text,
                    "speech": [{"start": 1, "end": 3, "utterance": "秘密"}],
                },
                "i": {"kind": "instance", "ref": "t"},
                "root": {"kind": "container", "children": {"n": "i"}},
            },
            {"p": {"root": "root", "public": True}},
        )
        result = document.redact("p", ("n",), 0, 1)
        node = result.document.objects[result.new_text_id]
        self.assertEqual(node["text"], "秘密")
        self.assertEqual(node["speech"], [{"start": 0, "end": 2, "utterance": "秘密"}])


if __name__ == "__main__":
    unittest.main()
