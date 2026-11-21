"""Shared assertion helpers for the authoritative-kernel test suite."""

from __future__ import annotations

from typing import Sequence

from authoritative_dns import AnswerStatus


class LookupAssertions:
    """Mixin adding readable assertions over :class:`Response` objects."""

    def assert_status(self, response, expected: str) -> None:
        self.assertEqual(
            response.status,
            expected,
            f"{response.qname} {response.qtype}: expected {expected}, "
            f"got {response.status} (source={response.source})",
        )

    def assert_answer(
        self,
        zone,
        name: str,
        rtype: str,
        values: Sequence,
        *,
        wildcard: bool = False,
    ) -> None:
        response = zone.lookup(name, rtype)
        self.assert_status(response, AnswerStatus.ANSWER)
        self.assertEqual(response.wildcard, wildcard)
        self.assertEqual(
            [(rr.value) for rr in response.records], list(values)
        )
        if wildcard:
            self.assertNotEqual(response.source, response.qname)
        else:
            self.assertEqual(response.source, response.qname)
        # Synthesis must always be answered under the original query name.
        for rr in response.records:
            self.assertEqual(rr.name, response.qname)

    def assert_nodata(self, zone, name: str, rtype: str) -> None:
        response = zone.lookup(name, rtype)
        self.assert_status(response, AnswerStatus.NODATA)
        self.assertEqual(list(response.records), [])

    def assert_nxdomain(self, zone, name: str, rtype: str) -> None:
        response = zone.lookup(name, rtype)
        self.assert_status(response, AnswerStatus.NXDOMAIN)
        self.assertEqual(list(response.records), [])
        self.assertIsNone(response.source)

    def assert_referral(
        self, zone, name: str, rtype: str, ns_values: Sequence[str]
    ) -> None:
        response = zone.lookup(name, rtype)
        self.assert_status(response, AnswerStatus.REFERRAL)
        self.assertEqual(
            sorted(rr.value for rr in response.records), sorted(ns_values)
        )
        self.assertTrue(all(rr.rtype == "NS" for rr in response.records))
        self.assertIsNotNone(response.source)
