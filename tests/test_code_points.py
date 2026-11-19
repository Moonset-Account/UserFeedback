"""Acceptance: character positions are Unicode code points, not UTF-16 or bytes."""

from __future__ import annotations

import unittest

from docredact import Document, RedactionError, extract_page_text
from tests import support


ASTRAL = "\U0001D11E"  # one code point, two UTF-16 code units, four UTF-8 bytes
COMBINED = "é"  # base letter + combining acute: two code points


def _single_text_document(text: str, speech: list[dict]) -> Document:
    return Document(
        {
            "t": {"kind": "text", "text": text, "speech": speech},
            "i": {"kind": "instance", "ref": "t"},
            "root": {"kind": "container", "children": {"n": "i"}},
        },
        {"p": {"root": "root", "public": True}},
    )


class CodePointTest(unittest.TestCase):
    def test_astral_symbol_counts_as_one_position(self) -> None:
        text = "甲" + ASTRAL + "乙" + ASTRAL + "丙"
        document = _single_text_document(
            text,
            [
                {"start": 0, "end": 1, "utterance": "甲"},
                {"start": 1, "end": 2, "utterance": "高音谱号"},
                {"start": 2, "end": 3, "utterance": "乙"},
                {"start": 3, "end": 4, "utterance": "第二个高音谱号"},
                {"start": 4, "end": 5, "utterance": "丙"},
            ],
        )
        result = document.redact("p", ("n",), 1, 2)

        self.assertEqual(result.removed_text, ASTRAL)
        self.assertEqual(extract_page_text(result.document, "p"), "甲乙" + ASTRAL + "丙")
        # Only the first of the two identical symbols is removed.
        self.assertEqual([s["utterance"] for s in result.removed_speech], ["高音谱号"])
        node = result.document.objects[result.new_text_id]
        last = node["speech"][-1]
        self.assertEqual(node["text"][last["start"]:last["end"]], "丙")
        self.assertEqual(last["start"], 3)

    def test_utf16_offsets_would_pick_the_wrong_character(self) -> None:
        text = ASTRAL + "ab"
        self.assertEqual(len(text.encode("utf-16-le")) // 2, 4)  # UTF-16 sees 4 units
        document = _single_text_document(text, [])
        result = document.redact("p", ("n",), 0, 1)
        self.assertEqual(result.removed_text, ASTRAL)
        self.assertEqual(extract_page_text(result.document, "p"), "ab")

    def test_combining_mark_is_its_own_position(self) -> None:
        text = "caf" + COMBINED  # 5 code points: c a f e ́
        self.assertEqual(len(text), 5)
        document = _single_text_document(
            text,
            [{"start": 3, "end": 5, "utterance": "e with acute"}],
        )
        result = document.redact("p", ("n",), 4, 5)
        self.assertEqual(result.removed_text, "́")
        self.assertEqual(extract_page_text(result.document, "p"), "cafe")
        # The segment [3, 5) intersected the removed code point, so it is gone.
        self.assertEqual([s["utterance"] for s in result.removed_speech], ["e with acute"])

    def test_cjk_range_uses_code_points(self) -> None:
        document = Document.from_dict(support.build_document())
        start, end = support.HEAD_SPANS["代号「曙光」"]
        result = document.redact("p1", ("main", "lead"), start, end)
        self.assertEqual(result.removed_text, "代号「曙光」")
        self.assertEqual(
            extract_page_text(result.document, "p1"),
            support.HEAD_TEXT
            + support.head_without("代号「曙光」")
            + support.COPY_TEXT,
        )

    def test_physical_astral_length_does_not_widen_the_accepted_range(self) -> None:
        text = ASTRAL + ASTRAL  # 2 code points, 4 UTF-16 units, 8 bytes
        document = _single_text_document(text, [])
        with self.assertRaises(RedactionError):
            document.redact("p", ("n",), 0, 3)
        result = document.redact("p", ("n",), 0, 2)
        self.assertEqual(extract_page_text(result.document, "p"), "")

    def test_emoji_with_variation_selector(self) -> None:
        text = "a❤️b"  # heart + VS16: two code points for one glyph
        self.assertEqual(len(text), 4)
        document = _single_text_document(text, [{"start": 1, "end": 3, "utterance": "heart"}])
        result = document.redact("p", ("n",), 2, 3)
        self.assertEqual(result.removed_text, "️")
        self.assertEqual(extract_page_text(result.document, "p"), "a❤b")
        self.assertEqual([s["utterance"] for s in result.removed_speech], ["heart"])

    def test_emoji_redaction_keeps_surrounding_text(self) -> None:
        text = "前\U0001F600后"
        document = _single_text_document(text, [{"start": 1, "end": 2, "utterance": "grin"}])
        result = document.redact("p", ("n",), 1, 2)
        self.assertEqual(extract_page_text(result.document, "p"), "前后")
        self.assertEqual([s["utterance"] for s in result.removed_speech], ["grin"])


if __name__ == "__main__":
    unittest.main()
