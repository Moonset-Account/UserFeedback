"""RED tests for V8b HTTPS source fetching."""

import unittest
from urllib.error import URLError

from src.vulnerability.updater import FetchPolicy, fetch_json_artifact


class FakeHeaders(dict):
    def get_content_length(self):
        value = self.get("Content-Length")
        return int(value) if value is not None else None


class FakeResponse:
    def __init__(self, payload, url, headers=None):
        self._payload = payload
        self._url = url
        self.headers = FakeHeaders(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def geturl(self):
        return self._url

    def read(self, size=-1):
        if size < 0:
            result, self._payload = self._payload, b""
            return result
        result, self._payload = self._payload[:size], self._payload[size:]
        return result


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requested = None

    def open(self, request, timeout):
        self.requested = (request, timeout)
        if self.error:
            raise self.error
        return self.response


class TestV8bSafeSourceFetch(unittest.TestCase):
    def test_fetches_https_allowlisted_source_with_metadata(self):
        opener = FakeOpener(FakeResponse(b'{"ok":true}', "https://updates.example.test/feed.json", {"ETag": '"v1"', "Content-Length": "11"}))
        result = fetch_json_artifact(
            "https://updates.example.test/feed.json",
            FetchPolicy(allowed_hosts=frozenset({"updates.example.test"})),
            opener=opener,
        )
        self.assertEqual(result.payload, b'{"ok":true}')
        self.assertEqual(result.final_url, "https://updates.example.test/feed.json")
        self.assertEqual(result.etag, '"v1"')
        self.assertEqual(opener.requested[0].headers["Accept"], "application/json")

    def test_rejects_http_and_disallowed_redirect_destination(self):
        policy = FetchPolicy(allowed_hosts=frozenset({"updates.example.test"}))
        with self.assertRaises(ValueError):
            fetch_json_artifact("http://updates.example.test/feed.json", policy, opener=FakeOpener())
        opener = FakeOpener(FakeResponse(b"{}", "https://evil.example.test/feed.json"))
        with self.assertRaises(ValueError):
            fetch_json_artifact("https://updates.example.test/feed.json", policy, opener=opener)

    def test_rejects_content_length_and_stream_over_limit(self):
        with self.assertRaises(ValueError):
            fetch_json_artifact(
                "https://updates.example.test/feed.json",
                FetchPolicy(allowed_hosts=frozenset({"updates.example.test"}), max_bytes=4),
                opener=FakeOpener(FakeResponse(b"{}", "https://updates.example.test/feed.json", {"Content-Length": "5"})),
            )
        with self.assertRaises(ValueError):
            fetch_json_artifact(
                "https://updates.example.test/feed.json",
                FetchPolicy(allowed_hosts=frozenset({"updates.example.test"}), max_bytes=4),
                opener=FakeOpener(FakeResponse(b"12345", "https://updates.example.test/feed.json")),
            )

    def test_wraps_transport_error_without_falling_back(self):
        opener = FakeOpener(error=URLError("offline"))
        with self.assertRaises(ConnectionError):
            fetch_json_artifact(
                "https://updates.example.test/feed.json",
                FetchPolicy(allowed_hosts=frozenset({"updates.example.test"})),
                opener=opener,
            )


if __name__ == "__main__":
    unittest.main()
