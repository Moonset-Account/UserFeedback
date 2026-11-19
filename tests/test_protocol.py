"""Wire contract: prefix-only requests, input rejection, and the HTTP transport."""

from __future__ import annotations

import json
import threading
import unittest

from password_screening import (
    DIGEST_LENGTH,
    DigestError,
    HTTPTransport,
    InvalidPrefix,
    LocalTransport,
    ScreeningClient,
    ScreeningService,
    VersionStore,
    serve,
)
from tests.fixtures import make_bucket


class RequestPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionStore(prefix_length=5, page_size=8)
        self.records = make_bucket("A1B2C", 3, label="privacy")
        self.store.publish(self.records, version_id="v-privacy")
        self.transport = LocalTransport(ScreeningService(self.store))
        self.client = ScreeningClient(self.transport, prefix_length=5)

    def test_client_sends_only_the_prefix(self) -> None:
        target = self.records[0]
        secret_suffix = target[5:]
        self.client.screen_digest(target)

        self.assertTrue(self.transport.calls)
        for call in self.transport.calls:
            blob = json.dumps(call)
            self.assertNotIn(secret_suffix, blob)
            self.assertNotIn(target, blob)
            self.assertEqual(call["prefix"], target[:5])
            self.assertEqual(set(call) - {"op", "prefix", "page", "version"}, set())

    def test_password_never_leaves_the_client(self) -> None:
        self.client.screen("correct horse battery staple")
        for call in self.transport.calls:
            self.assertNotIn("correct horse battery staple", json.dumps(call))

    def test_digest_is_not_truncated_to_less_than_configured_prefix(self) -> None:
        with self.assertRaises(DigestError):
            self.client.screen_digest("ABC")

    def test_client_prefix_length_validation(self) -> None:
        for bad in (0, DIGEST_LENGTH, -1, "5", 2.5, True):
            with self.assertRaises(DigestError):
                ScreeningClient(self.transport, prefix_length=bad)


class ServiceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionStore(prefix_length=4, page_size=4)
        self.store.publish(make_bucket("AAAA", 2, label="svc"), version_id="v-svc")
        self.service = ScreeningService(self.store)

    def test_valid_page_request_is_accepted(self) -> None:
        response = self.service.handle({"op": "page", "prefix": "AAAA", "page": 0})
        self.assertTrue(response["ok"])
        self.assertEqual(len(response["entries"]), 4)
        self.assertEqual(response["served_version"], "v-svc")

    def test_lowercase_prefix_is_canonicalised(self) -> None:
        response = self.service.handle({"op": "page", "prefix": "aaaa", "page": 0})
        self.assertTrue(response["ok"])
        self.assertEqual(response["prefix"], "AAAA")

    def test_malformed_prefixes_are_rejected(self) -> None:
        cases = {
            "wrong length": "AAA",
            "too long": "AAAAA",
            "non hex": "GGGG",
            "empty": "",
            "not a string": 1234,
            "missing": None,
        }
        for label, prefix in cases.items():
            request = {"op": "page", "page": 0}
            if prefix is not None:
                request["prefix"] = prefix
            response = self.service.handle(request)
            with self.subTest(label=label):
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "invalid_prefix")

    def test_page_out_of_range_is_rejected(self) -> None:
        response = self.service.handle({"op": "page", "prefix": "AAAA", "page": 1})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "page_out_of_range")

    def test_negative_and_non_integer_pages_are_rejected(self) -> None:
        for page in (-1, "0", 1.5, True):
            response = self.service.handle({"op": "page", "prefix": "AAAA", "page": page})
            with self.subTest(page=page):
                self.assertFalse(response["ok"])
                self.assertIn(response["error"], {"page_out_of_range", "protocol_error"})

    def test_unknown_operation_and_prefix_length_mismatch_are_rejected(self) -> None:
        unknown = self.service.handle({"op": "dump", "prefix": "AAAA"})
        self.assertFalse(unknown["ok"])

        mismatch = self.service.handle(
            {"op": "page", "prefix": "AAAA", "page": 0, "prefix_length": 5}
        )
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["error"], "invalid_prefix")

    def test_invalid_digest_list_never_becomes_a_version(self) -> None:
        for bad in (["not-a-digest"], ["A" * 63], ["Z" * 64], "A" * 64):
            with self.subTest(value=bad):
                with self.assertRaises(DigestError):
                    self.store.publish(bad, version_id="v-bad-%s" % id(bad))

    def test_store_requires_a_published_version(self) -> None:
        empty_store = VersionStore(prefix_length=4, page_size=4)
        response = ScreeningService(empty_store).handle(
            {"op": "page", "prefix": "AAAA", "page": 0}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "no_current_version")


class HTTPTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = VersionStore(prefix_length=4, page_size=4)
        self.records = make_bucket("FEED", 2, label="http")
        self.store.publish(self.records, version_id="v-http")
        self.server = serve(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.transport = HTTPTransport(f"http://{host}:{port}")
        self.client = ScreeningClient(self.transport, prefix_length=4)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_round_trip_finds_a_record(self) -> None:
        result = self.client.screen_digest(self.records[1])
        self.assertTrue(result.matched)
        self.assertEqual(result.version_id, "v-http")
        self.assertEqual(result.pages_read, 1)

    def test_round_trip_misses_on_empty_bucket_with_same_shape(self) -> None:
        result = self.client.screen_digest("0" * DIGEST_LENGTH)
        self.assertFalse(result.matched)
        self.assertEqual(result.entries_inspected, 4)

    def test_http_error_payload_is_surfaced(self) -> None:
        response = self.transport.request({"op": "page", "prefix": "ZZ", "page": 0})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "invalid_prefix")

    def test_client_turns_service_rejection_into_an_exception(self) -> None:
        class RejectingTransport:
            def request(self, payload):
                return {"ok": False, "error": "invalid_prefix", "message": "nope"}

        client = ScreeningClient(RejectingTransport(), prefix_length=4)
        with self.assertRaises(InvalidPrefix):
            client.screen_digest("A" * DIGEST_LENGTH)


class RawHTTPContractTests(unittest.TestCase):
    """The HTTP layer itself enforces the request envelope."""

    def setUp(self) -> None:
        self.store = VersionStore(prefix_length=4, page_size=4)
        self.store.publish(make_bucket("1234", 1, label="raw"), version_id="v-raw")
        self.server = serve(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _post(self, path: str, body: bytes):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_unknown_path_is_404(self) -> None:
        status, payload = self._post("/nope", b"{}")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_invalid_json_is_400(self) -> None:
        status, payload = self._post("/lookup", b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_request")

    def test_non_object_json_is_400(self) -> None:
        status, payload = self._post("/lookup", b"[1,2,3]")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_request")

    def test_oversized_body_is_rejected(self) -> None:
        status, payload = self._post("/lookup", b"{" + b" " * 5000 + b"}")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_request")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
