"""Tests for the documentation link allowlist policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (importing it puts the project root on sys.path)

from diagnostics import link_policy


def policy(*hosts: str) -> link_policy.LinkPolicy:
    return link_policy.LinkPolicy(hosts or ("docs.example.com",))


class AllowCases(unittest.TestCase):
    def test_simple_https_url_allowed(self) -> None:
        decision = policy().check("https://docs.example.com/errors/E42")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, link_policy.OK)
        self.assertEqual(decision.host, "docs.example.com")

    def test_query_and_fragment_allowed(self) -> None:
        url = "https://docs.example.com/a/b?x=1&y=%3Cscript%3E#frag"
        self.assertTrue(policy().check(url).allowed)

    def test_uppercase_scheme_and_host_allowed(self) -> None:
        decision = policy().check("HTTPS://DOCS.EXAMPLE.COM/errors/E42")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.host, "docs.example.com")

    def test_trailing_root_dot_canonicalised(self) -> None:
        decision = policy().check("https://docs.example.com./errors/E42")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.host, "docs.example.com")

    def test_non_default_port_allowed(self) -> None:
        self.assertTrue(policy().check("https://docs.example.com:8443/errors").allowed)

    def test_idn_allowlist_entry_matches_punycode_url(self) -> None:
        checker = policy("例え.example")
        decision = checker.check("https://xn--r8jz45g.example/docs")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.host, "xn--r8jz45g.example")

    def test_multiple_hosts(self) -> None:
        checker = policy("a.example", "b.example")
        self.assertTrue(checker.check("https://b.example/x").allowed)
        self.assertFalse(checker.check("https://c.example/x").allowed)


class RejectCases(unittest.TestCase):
    def assertRejected(self, url: str, reason: str, *, hosts: str = "docs.example.com") -> None:
        decision = policy(hosts).check(url)
        self.assertFalse(decision.allowed, msg=f"{url!r} should be rejected")
        self.assertEqual(decision.reason, reason, msg=f"{url!r} reason")
        self.assertTrue(decision.note)

    def test_http_rejected(self) -> None:
        self.assertRejected("http://docs.example.com/x", link_policy.SCHEME_NOT_HTTPS)

    def test_scheme_relative_rejected(self) -> None:
        self.assertRejected("//docs.example.com/x", link_policy.SCHEME_NOT_HTTPS)

    def test_javascript_rejected(self) -> None:
        self.assertRejected("javascript:alert(1)", link_policy.SCHEME_NOT_HTTPS)

    def test_data_url_rejected(self) -> None:
        self.assertRejected(
            "data:text/html,<script>alert(1)</script>", link_policy.SCHEME_NOT_HTTPS
        )

    def test_userinfo_rejected(self) -> None:
        self.assertRejected(
            "https://user:pass@docs.example.com/private", link_policy.CREDENTIALS
        )

    def test_user_only_credential_rejected(self) -> None:
        self.assertRejected("https://user@docs.example.com/x", link_policy.CREDENTIALS)

    def test_empty_password_credential_rejected(self) -> None:
        self.assertRejected("https://user:@docs.example.com/x", link_policy.CREDENTIALS)

    def test_evil_suffix_rejected(self) -> None:
        self.assertRejected(
            "https://docs.example.com.evil.test/x", link_policy.HOST_NOT_ALLOWED
        )

    def test_evil_prefix_rejected(self) -> None:
        self.assertRejected(
            "https://evildocs.example.com/x", link_policy.HOST_NOT_ALLOWED
        )

    def test_ip_literal_rejected(self) -> None:
        self.assertRejected("https://192.0.2.10/x", link_policy.HOST_NOT_ALLOWED)

    def test_ipv6_literal_rejected(self) -> None:
        self.assertRejected("https://[2001:db8::1]/x", link_policy.HOST_NOT_ALLOWED)

    def test_tab_in_host_rejected_before_parsing(self) -> None:
        self.assertRejected("https://docs.example.com\t.evil.test/x", link_policy.CONTROL_CHARS)

    def test_newline_in_url_rejected(self) -> None:
        self.assertRejected("https://docs.example.com/\nevil.test", link_policy.CONTROL_CHARS)

    def test_nbsp_rejected(self) -> None:
        self.assertRejected(
            "https://docs.example.com .evil.test/x", link_policy.CONTROL_CHARS
        )

    def test_bidi_override_rejected(self) -> None:
        self.assertRejected(
            "https://docs.example.com/‮evil", link_policy.CONTROL_CHARS
        )

    def test_backslash_rejected(self) -> None:
        self.assertRejected(
            "https://docs.example.com\\@evil.test/x", link_policy.BACKSLASH
        )

    def test_empty_url_rejected(self) -> None:
        self.assertRejected("", link_policy.EMPTY_URL)

    def test_missing_host_rejected(self) -> None:
        self.assertRejected("https:///path-only", link_policy.MISSING_HOST)

    def test_invalid_port_rejected(self) -> None:
        self.assertRejected("https://docs.example.com:notaport/x", link_policy.INVALID_PORT)

    def test_bad_host_shape_rejected(self) -> None:
        self.assertRejected("https://docs..example.com/x", link_policy.MALFORMED_HOST)

    def test_underscore_host_rejected(self) -> None:
        self.assertRejected("https://doc_s.example.com/x", link_policy.MALFORMED_HOST)


class UrlConfusionTests(unittest.TestCase):
    """Host-confusion shapes that must not reach the allowlist."""

    def setUp(self) -> None:
        self.checker = policy()

    def assertNotAllowed(self, url: str) -> None:
        decision = self.checker.check(url)
        self.assertFalse(decision.allowed, msg=f"{url!r} must not be allowed")

    def test_percent_encoded_dot_in_host_rejected(self) -> None:
        self.assertNotAllowed("https://docs%2Eexample.com/x")

    def test_percent_encoded_suffix_rejected(self) -> None:
        self.assertNotAllowed("https://docs.example.com%2eevil.test/x")

    def test_port_then_at_rejected_as_credentials(self) -> None:
        decision = self.checker.check("https://docs.example.com:443@evil.test/x")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, link_policy.CREDENTIALS)

    def test_ideographic_full_stop_host_rejected(self) -> None:
        # U+3002 is treated as a label separator by browsers/IDNA.
        self.assertNotAllowed("https://docs.example.com。evil.test/x")

    def test_encoded_nul_in_host_rejected(self) -> None:
        self.assertNotAllowed("https://docs.example.com%00.evil.test/x")

    def test_unicode_host_not_in_allowlist_rejected(self) -> None:
        self.assertNotAllowed("https://xn--e1afmkfd.example/x")

    def test_at_in_path_or_fragment_does_not_confuse_host_check(self) -> None:
        # The host really is the allowlisted one; '@' later in the URL is data.
        self.assertTrue(self.checker.check("https://docs.example.com/x?next=@evil.test").allowed)
        self.assertTrue(self.checker.check("https://docs.example.com#@evil.test").allowed)

    def test_at_in_fragment_does_not_rescue_evil_host(self) -> None:
        decision = self.checker.check("https://evil.test#@docs.example.com")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, link_policy.HOST_NOT_ALLOWED)
        self.assertEqual(decision.host, "evil.test")

    def test_allowlisted_lookalike_subdomain_rejected(self) -> None:
        self.assertNotAllowed("https://docs.example.com.attacker.test/x")


class ConfigTests(unittest.TestCase):
    def test_load_object_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(
                json.dumps({"allowed_doc_hosts": ["Docs.Example.com"]}), encoding="utf-8"
            )
            loaded = link_policy.load_policy(path)
            self.assertEqual(loaded.allowed_hosts, ("docs.example.com",))

    def test_load_array_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(json.dumps(["a.example"]), encoding="utf-8")
            self.assertEqual(link_policy.load_policy(path).allowed_hosts, ("a.example",))

    def test_empty_allowlist_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(link_policy.ConfigError):
                link_policy.load_policy(path)

    def test_missing_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allow.json"
            path.write_text(json.dumps({"hosts": ["a.example"]}), encoding="utf-8")
            with self.assertRaises(link_policy.ConfigError):
                link_policy.load_policy(path)

    def test_allowlist_entry_with_path_rejected(self) -> None:
        with self.assertRaises(link_policy.ConfigError):
            link_policy.LinkPolicy(["https://docs.example.com/path"])

    def test_allowlist_entry_with_space_rejected(self) -> None:
        with self.assertRaises(link_policy.ConfigError):
            link_policy.LinkPolicy([" docs.example.com"])


if __name__ == "__main__":
    unittest.main()
