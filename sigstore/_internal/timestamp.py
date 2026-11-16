# Copyright 2022 The Sigstore Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Utilities to deal with sources of signed time.
"""

import enum
import threading
from dataclasses import dataclass
from datetime import datetime

import requests
from rfc3161_client import (
    PKIStatus,
    TimestampRequestBuilder,
    TimeStampResponse,
    decode_timestamp_response,
)
from rfc3161_client.base import HashAlgorithm

from sigstore._internal import USER_AGENT

CLIENT_TIMEOUT: int = 5


class TimestampSource(enum.Enum):
    """Represents the source of a timestamp."""

    TIMESTAMP_AUTHORITY = enum.auto()
    TRANSPARENCY_SERVICE = enum.auto()


@dataclass
class TimestampVerificationResult:
    """Represents a timestamp used by the Verifier.

    A Timestamp either comes from a Timestamping Service (RFC3161) or the Transparency
    Service.
    """

    source: TimestampSource
    time: datetime


class TimestampError(Exception):
    """
    A generic error in the TimestampAuthority client.
    """


class TimestampAuthorityError(TimestampError):
    """
    The Timestamp Authority returned a protocol-level (RFC 3161) refusal:
    a `TimeStampResp` whose `PKIStatus` is neither `granted` nor
    `grantedWithMods`, and which therefore contains no timestamp token.

    TSAs commonly return these responses with an HTTP 200 status code, so
    they cannot be detected at the transport layer. This error carries the
    authority's `status` and human-readable `status_string` so that callers
    can classify the refusal.
    """

    def __init__(self, status: int, status_string: list[str]) -> None:
        self.status = status
        self.status_string = status_string

        try:
            status_name = PKIStatus(status).name.lower()
        except ValueError:
            status_name = f"unknown ({status})"

        reason = ", ".join(status_string) if status_string else "no reason given"
        super().__init__(
            f"Timestamp Authority refused the request (status={status_name}): {reason}"
        )


class TimestampAuthorityClient:
    """Internal client to deal with a Timestamp Authority"""

    def __init__(self, url: str) -> None:
        """
        Create a new `TimestampAuthorityClient` from the given URL.
        """
        self.url = url
        self._thread_local = threading.local()

    @property
    def _session(self) -> requests.Session:
        """
        Lazy-initialized thread-local session object
        """
        if not hasattr(self._thread_local, "session"):
            session = requests.Session()
            session.headers.update(
                {
                    "Content-Type": "application/timestamp-query",
                    "User-Agent": USER_AGENT,
                }
            )
            self._thread_local.session = session
        return self._thread_local.session  # type: ignore[no-any-return]

    def request_timestamp(self, signature: bytes) -> TimeStampResponse:
        """
        Timestamp the signature using the configured Timestamp Authority.

        This method generates a RFC3161 Timestamp Request and sends it to a TSA.
        The received response is parsed but *not* cryptographically verified.

        request_timestamp() can be called from multiple threads.

        Raises a TimestampError on failure, or a TimestampAuthorityError
        (a TimestampError subclass) when the TSA returns a protocol-level
        refusal.
        """
        # Build the timestamp request
        try:
            timestamp_request = (
                TimestampRequestBuilder()
                .hash_algorithm(HashAlgorithm.SHA256)
                .data(signature)
                .nonce(nonce=True)
                .build()
            )
        except ValueError as error:
            msg = f"invalid request: {error}"
            raise TimestampError(msg)

        # Send it to the TSA for signing
        try:
            response = self._session.post(
                self.url,
                data=timestamp_request.as_bytes(),
                timeout=CLIENT_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            msg = f"error while sending the request to the TSA: {error}"
            raise TimestampError(msg)

        # Check that we can parse the response but do not *verify* it
        try:
            timestamp_response = decode_timestamp_response(response.content)
        except ValueError as e:
            msg = f"invalid response: {e}"
            raise TimestampError(msg)

        # A TSA may answer a well-formed request with a protocol-level
        # refusal (RFC 3161 PKIStatus other than granted/grantedWithMods),
        # typically still with an HTTP 200 status code. Such a response
        # contains no timestamp token, so surface the refusal here rather
        # than letting the caller fail later on the missing token.
        if timestamp_response.status not in (
            PKIStatus.GRANTED,
            PKIStatus.GRANTED_WITH_MODS,
        ):
            raise TimestampAuthorityError(
                status=timestamp_response.status,
                status_string=timestamp_response.status_string,
            )

        return timestamp_response
