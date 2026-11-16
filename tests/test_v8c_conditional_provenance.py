"""RED tests for V8c conditional fetch and source provenance."""

import hashlib
import json
import unittest
from urllib.error import HTTPError

from src.vulnerability.updater import (
    FetchConditions,
    FetchPolicy,
    build_source_provenance,
    fetch_json_artifact,
)


class Headers(dict):
    def get_content_length(self):
        value = self.get("Content-Length")
        return int(value) if value is not None else None


class Response:
    def __init__(self, payload, url, headers=None):
        self.payload = payload
        self.url = url
        self.headers = Headers(headers or {})

    def __enter__(self): return self
    def __exit__(self, *_): return None
    def geturl(self): return self.url
    def read(self, size=-1):
        result, self.payload = self.payload, b""
        return result


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    def open(self, request, timeout):
        self.request = request
        if self.error:
            raise self.error
        return self.response


class TestV8cConditionalProvenance(unittest.TestCase):
    def test_sends_conditional_headers_and_preserves_source_metadata(self):
        opener = Opener(Response(b'{"ok":true}', "https://updates.example.test/feed.json", {"ETag": '"v2"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"}))
        result = fetch_json_artifact(
            "https://updates.example.test/feed.json",
            FetchPolicy(frozenset({"updates.example.test"})),
            conditions=FetchConditions(etag='"v1"', last_modified="Tue, 31 Dec 2024 00:00:00 GMT"),
            opener=opener,
        )
        self.assertEqual(opener.request.headers["If-none-match"], '"v1"')
        self.assertEqual(opener.request.headers["If-modified-since"], "Tue, 31 Dec 2024 00:00:00 GMT")
        self.assertFalse(result.not_modified)
        self.assertEqual(result.etag, '"v2"')

    def test_returns_explicit_not_modified_result_for_304(self):
        headers = Headers({"ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
        error = HTTPError("https://updates.example.test/feed.json", 304, "Not Modified", headers, None)
        result = fetch_json_artifact(
            "https://updates.example.test/feed.json",
            FetchPolicy(frozenset({"updates.example.test"})),
            conditions=FetchConditions(etag='"v1"'),
            opener=Opener(error=error),
        )
        self.assertTrue(result.not_modified)
        self.assertIsNone(result.payload)
        self.assertEqual(result.etag, '"v1"')

    def test_provenance_is_metadata_only_deterministic_and_hashed(self):
        opener = Opener(Response(b'{"ok":true}', "https://updates.example.test/feed.json", {"ETag": '"v2"', "Content-Type": "application/json"}))
        result = fetch_json_artifact("https://updates.example.test/feed.json", FetchPolicy(frozenset({"updates.example.test"})), opener=opener)
        first = build_source_provenance("osv", result)
        second = build_source_provenance("osv", result)
        self.assertEqual(first, second)
        self.assertNotIn("payload", first)
        self.assertEqual(first["payload_sha256"], "sha256:" + hashlib.sha256(b'{"ok":true}').hexdigest())
        canonical = json.dumps({k: v for k, v in first.items() if k != "provenance_sha256"}, sort_keys=True, separators=(",", ":")) + "\n"
        self.assertEqual(first["provenance_sha256"], "sha256:" + hashlib.sha256(canonical.encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()
