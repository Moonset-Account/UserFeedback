"""Unit tests for the structured event log module."""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eventlog


class RoundTripTests(unittest.TestCase):
    def roundtrip(self, severity, event_id, message, fields=None):
        buf = io.BytesIO()
        eventlog.write_event(buf, severity, event_id, message, fields)
        buf.seek(0)
        events, truncation = eventlog.read_events(buf)
        self.assertIsNone(truncation)
        self.assertEqual(len(events), 1)
        return events[0]

    def test_multiline_body_preserved(self):
        body = "first line\nsecond line\n\nfourth line\r\nwith CRLF\n"
        event = self.roundtrip("ERROR", "EVT-1", body)
        self.assertEqual(event["message"], body)
        self.assertEqual(event["severity"], "ERROR")
        self.assertEqual(event["event_id"], "EVT-1")

    def test_unicode_control_characters_preserved(self):
        body = "nul=\x00 esc=\x1b bell=\x07 nel=\x85 ls=  ps=  tab=\t del=\x7f"
        event = self.roundtrip("INFO", "EVT-2", body)
        self.assertEqual(event["message"], body)

    def test_delimiter_like_content_preserved(self):
        # Bytes that look exactly like a record header inside the body must
        # not confuse the framing.
        body = "EVL1 12\n{\"severity\": \"DEBUG\"}\nEVL1 0\n"
        event = self.roundtrip("WARNING", "EVT-3", body)
        self.assertEqual(event["message"], body)

    def test_non_ascii_unicode_preserved(self):
        body = "中文正文 emoji=\U0001f600 combining=é"
        event = self.roundtrip("NOTICE", "EVT-4", body, {"k": "值"})
        self.assertEqual(event["message"], body)
        self.assertEqual(event["fields"], {"k": "值"})

    def test_empty_message_and_fields(self):
        event = self.roundtrip("DEBUG", "EVT-5", "")
        self.assertEqual(event["message"], "")
        self.assertEqual(event["fields"], {})

    def test_one_write_is_one_record(self):
        buf = io.BytesIO()
        eventlog.write_event(buf, "INFO", "EVT-6", "a\nb\nc")
        eventlog.write_event(buf, "INFO", "EVT-7", "d")
        buf.seek(0)
        events, truncation = eventlog.read_events(buf)
        self.assertIsNone(truncation)
        self.assertEqual([e["event_id"] for e in events], ["EVT-6", "EVT-7"])
        self.assertEqual(events[0]["message"], "a\nb\nc")


class ValidationTests(unittest.TestCase):
    def test_reserved_field_conflict_names_field(self):
        for reserved in ("severity", "event_id", "message", "timestamp"):
            with self.assertRaises(eventlog.ReservedFieldError) as ctx:
                eventlog.build_record("INFO", "EVT-10", "x", {reserved: "y"})
            self.assertEqual(ctx.exception.field, reserved)
            self.assertIn(reserved, str(ctx.exception))

    def test_invalid_severity_rejected(self):
        with self.assertRaises(eventlog.ValidationError):
            eventlog.build_record("HIGH", "EVT-11", "x")
        with self.assertRaises(eventlog.ValidationError):
            eventlog.build_record("info", "EVT-11", "x")  # case-sensitive

    def test_invalid_event_id_rejected(self):
        for bad in ("", "has space", "new\nline", "-leading-dash", "x" * 200, 42):
            with self.assertRaises(eventlog.ValidationError):
                eventlog.build_record("INFO", bad, "x")

    def test_non_string_message_rejected(self):
        with self.assertRaises(eventlog.ValidationError):
            eventlog.build_record("INFO", "EVT-12", {"not": "a string"})

    def test_unserializable_field_rejected(self):
        with self.assertRaises(eventlog.ValidationError):
            eventlog.build_record("INFO", "EVT-13", "x", {"bad": object()})


class BatchIsolationTests(unittest.TestCase):
    def test_bad_record_does_not_affect_others(self):
        events = [
            {"severity": "INFO", "event_id": "EVT-20", "message": "ok-1"},
            {"severity": "BOGUS", "event_id": "EVT-21", "message": "bad severity"},
            {"severity": "ERROR", "event_id": "EVT-22", "message": "ok-2",
             "fields": {"severity": "DEBUG"}},  # reserved conflict
            {"severity": "CRITICAL", "event_id": "EVT-23", "message": "ok-3"},
        ]
        buf = io.BytesIO()
        written, errors = eventlog.write_batch(buf, events)
        self.assertEqual(written, 2)
        self.assertEqual([i for i, _ in errors], [1, 2])
        self.assertIsInstance(errors[1][1], eventlog.ReservedFieldError)
        self.assertEqual(errors[1][1].field, "severity")

        buf.seek(0)
        read_back, truncation = eventlog.read_events(buf)
        self.assertIsNone(truncation)
        # Trusted metadata of the surviving records is exactly what was
        # submitted; the malformed neighbours changed nothing.
        self.assertEqual(
            [(e["severity"], e["event_id"], e["message"]) for e in read_back],
            [("INFO", "EVT-20", "ok-1"), ("CRITICAL", "EVT-23", "ok-3")],
        )

    def test_failed_record_emits_no_partial_bytes(self):
        buf = io.BytesIO()
        eventlog.write_event(buf, "INFO", "EVT-24", "before")
        size_before = len(buf.getvalue())
        with self.assertRaises(eventlog.EventLogError):
            eventlog.write_event(buf, "NOPE", "EVT-25", "bad")
        self.assertEqual(len(buf.getvalue()), size_before)


class TruncationTests(unittest.TestCase):
    def _log_with_tail(self, tail):
        buf = io.BytesIO()
        eventlog.write_event(buf, "INFO", "EVT-30", "complete")
        return buf.getvalue() + tail

    def test_truncated_payload_reported(self):
        full = eventlog.build_record("INFO", "EVT-31", "x" * 100)
        data = self._log_with_tail(full[: len(full) // 2])
        events, truncation = eventlog.read_events(io.BytesIO(data))
        self.assertEqual(len(events), 1)
        self.assertIsNotNone(truncation)
        self.assertEqual(truncation["index"], 1)
        self.assertLess(truncation["received"], truncation["expected"])

    def test_partial_header_reported(self):
        data = self._log_with_tail(b"EVL1 12")  # no newline terminator
        events, truncation = eventlog.read_events(io.BytesIO(data))
        self.assertEqual(len(events), 1)
        self.assertIsNotNone(truncation)
        self.assertEqual(truncation["index"], 1)

    def test_missing_trailer_reported(self):
        full = eventlog.build_record("INFO", "EVT-32", "abc")
        data = self._log_with_tail(full[:-1])  # drop trailing newline
        events, truncation = eventlog.read_events(io.BytesIO(data))
        self.assertEqual(len(events), 1)
        self.assertIsNotNone(truncation)
        self.assertEqual(truncation["index"], 1)

    def test_clean_file_has_no_truncation(self):
        buf = io.BytesIO()
        for i in range(3):
            eventlog.write_event(buf, "INFO", "EVT-33", "msg %d" % i)
        buf.seek(0)
        events, truncation = eventlog.read_events(buf)
        self.assertEqual(len(events), 3)
        self.assertIsNone(truncation)

    def test_mid_stream_corruption_raises(self):
        good = eventlog.build_record("INFO", "EVT-34", "a")
        data = good + b"GARBAGE HEADER\n" + good
        with self.assertRaises(eventlog.CorruptLogError):
            eventlog.read_events(io.BytesIO(data))


class CliTests(unittest.TestCase):
    def test_cli_write_read_roundtrip(self):
        import app

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.log")
            rc = app.main([
                "write", path, "--severity", "ERROR", "--event-id", "EVT-40",
                "--message", "line1\nline2", "--field", "source=cli",
            ])
            self.assertEqual(rc, 0)
            with open(path, "rb") as fh:
                events, truncation = eventlog.read_events(fh)
            self.assertIsNone(truncation)
            self.assertEqual(events[0]["message"], "line1\nline2")
            self.assertEqual(events[0]["fields"], {"source": "cli"})

    def test_cli_read_reports_truncation_exit_code(self):
        import app

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.log")
            with open(path, "wb") as fh:
                fh.write(eventlog.build_record("INFO", "EVT-41", "ok")[:8])
            rc = app.main(["read", path])
            self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
