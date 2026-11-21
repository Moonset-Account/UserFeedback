"""Constants shared by the resolver, the tests and the CLI."""

from __future__ import annotations

from enum import StrEnum


class AnswerStatus(StrEnum):
    """Coarse outcome of an authoritative lookup.

    The four values mirror the DNS response categories this kernel models:
    a positive answer, a NOERROR response with no data of the requested type,
    a name that does not exist, and a referral to a child zone at a
    delegation point.
    """

    ANSWER = "ANSWER"
    NODATA = "NODATA"
    NXDOMAIN = "NXDOMAIN"
    REFERRAL = "REFERRAL"


#: Supported query types.  NS is intentionally excluded: NS data is only
#: surfaced through referrals (and as ordinary apex data).
QUERY_TYPES = frozenset({"A", "AAAA", "TXT"})

#: Supported record types, including the delegation/apex NS RRset.
RECORD_TYPES = frozenset({"A", "AAAA", "TXT", "NS"})
