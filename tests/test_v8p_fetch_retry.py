"""RED tests for V8p bounded fetch retry/backoff."""

import unittest
from urllib.error import HTTPError, URLError

from src.vulnerability.updater import FetchPolicy, fetch_json_artifact


class Headers(dict):
    def get_content_length(self):
        value = self.get("Content-Length")
        return int(value) if value is not None else None


class Response:
    def __init__(self, payload=b'{"ok":true}', url="https://updates.example.test/feed.json", headers=None):
        self.payload = payload
        self.url = url
        self.headers = Headers(headers or {})
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def geturl(self): return self.url
    def read(self, size=-1):
        result, self.payload = self.payload, b""
        return result


class SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
    def open(self, request, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TestV8pFetchRetry(unittest.TestCase):
    def test_retries_transport_error_with_exponential_backoff(self):
        opener = SequenceOpener([URLError("offline"), Response()])
        delays = []
        result = fetch_json_artifact(
            "https://updates.example.test/feed.json",
            FetchPolicy(frozenset({"updates.example.test"}), max_attempts=2, backoff_seconds=0.25),
            opener=opener,
            sleeper=delays.append,
        )
        self.assertEqual(result.payload, b'{"ok":true}')
        self.assertEqual(opener.calls, 2)
        self.assertEqual(delays, [0.25])

    def test_honors_bounded_retry_after_for_429(self):
        headers = Headers({"Retry-After": "7"})
        error = HTTPError("https://updates.example.test/feed.json", 429, "Too Many", headers, None)
        opener = SequenceOpener([error, Response()])
        delays = []
        fetch_json_artifact(
            "https://updates.example.test/feed.json",
            FetchPolicy(frozenset({"updates.example.test"}), max_attempts=2, backoff_seconds=1, max_retry_after=5),
            opener=opener,
            sleeper=delays.append,
        )
        self.assertEqual(delays, [5.0])

    def test_does_not_retry_non_transient_http_error(self):
        error = HTTPError("https://updates.example.test/feed.json", 404, "Missing", Headers(), None)
        opener = SequenceOpener([error, Response()])
        with self.assertRaises(ConnectionError):
            fetch_json_artifact(
                "https://updates.example.test/feed.json",
                FetchPolicy(frozenset({"updates.example.test"}), max_attempts=3),
                opener=opener,
                sleeper=lambda _: self.fail("unexpected retry"),
            )
        self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    unittest.main()
