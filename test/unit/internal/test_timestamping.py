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
import pytest
import requests
from rfc3161_client import PKIStatus, TimeStampResponse

from sigstore._internal.timestamp import (
    TimestampAuthorityClient,
    TimestampAuthorityError,
    TimestampError,
)
from sigstore._utils import sha256_digest


@pytest.mark.timestamp_authority
class TestTimestampAuthorityClient:
    def test_sign_request(self, tsa_url: str):
        tsa = TimestampAuthorityClient(tsa_url)
        response = tsa.request_timestamp(b"hello")
        assert response
        assert (
            response.tst_info.message_imprint.message == sha256_digest(b"hello").digest
        )
        assert (
            response.tst_info.message_imprint.hash_algorithm.dotted_string
            == "2.16.840.1.101.3.4.2.1"
        )  # SHA256 OID

    def test_sign_request_invalid_url(self):
        tsa = TimestampAuthorityClient("http://fake-url")
        with pytest.raises(TimestampError, match="error while sending"):
            tsa.request_timestamp(b"hello")

    def test_sign_request_invalid_request(self, tsa_url):
        tsa = TimestampAuthorityClient(tsa_url)
        with pytest.raises(TimestampError, match="invalid request"):
            tsa.request_timestamp(b"")  # empty value here

    def test_invalid_response(self, tsa_url, monkeypatch):
        monkeypatch.setattr(requests.Response, "content", b"invalid-response")

        tsa = TimestampAuthorityClient(tsa_url)
        with pytest.raises(TimestampError, match="invalid response"):
            tsa.request_timestamp(b"hello")


def _der_tlv(tag: int, content: bytes) -> bytes:
    """Encode a single DER TLV (short-form length only)."""
    assert len(content) < 128
    return bytes([tag, len(content)]) + content


def _tsa_refusal_response(status: int, reason: str) -> bytes:
    """
    Build a DER-encoded RFC 3161 TimeStampResp carrying a protocol-level
    refusal: a non-granted PKIStatusInfo and no TimeStampToken, as a TSA
    would return (typically with an HTTP 200 status code).
    """
    status_info = _der_tlv(
        0x30,  # SEQUENCE: PKIStatusInfo
        _der_tlv(0x02, bytes([status]))  # INTEGER: PKIStatus
        + _der_tlv(
            0x30,  # SEQUENCE OF: PKIFreeText
            _der_tlv(0x0C, reason.encode()),  # UTF8String
        ),
    )
    return _der_tlv(0x30, status_info)  # SEQUENCE: TimeStampResp


def _mock_tsa_response(monkeypatch, body: bytes, status_code: int = 200) -> None:
    """Make the client's HTTP POST return a fixed, offline response."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    monkeypatch.setattr(requests.Session, "post", lambda *args, **kwargs: response)


class TestTimestampAuthorityClientOffline:
    """
    Offline tests for `TimestampAuthorityClient`: TSA responses are canned,
    so no timestamp authority (or network) is required.
    """

    def test_refusal_response(self, monkeypatch):
        # A TSA may refuse a well-formed request with a protocol-level
        # rejection while still answering HTTP 200.
        _mock_tsa_response(
            monkeypatch,
            _tsa_refusal_response(PKIStatus.REJECTION, "unsupported algorithm"),
        )

        tsa = TimestampAuthorityClient("http://unused")
        with pytest.raises(TimestampAuthorityError) as excinfo:
            tsa.request_timestamp(b"hello")

        error = excinfo.value
        # Callers can classify it both as a generic TimestampError and as
        # a protocol-level refusal carrying the authority's status.
        assert isinstance(error, TimestampError)
        assert error.status == PKIStatus.REJECTION
        assert error.status_string == ["unsupported algorithm"]
        assert "unsupported algorithm" in str(error)

    def test_transport_failure(self, monkeypatch):
        def _post(*args, **kwargs):
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr(requests.Session, "post", _post)

        tsa = TimestampAuthorityClient("http://unused")
        with pytest.raises(TimestampError, match="error while sending"):
            tsa.request_timestamp(b"hello")

    def test_granted_response(self, monkeypatch, asset):
        # A real granted TimeStampResp captured from the sigstore TSA.
        body = asset("tsa/issue1482-timestamp-with-no-cert").read_bytes()
        _mock_tsa_response(monkeypatch, body)

        tsa = TimestampAuthorityClient("http://unused")
        response = tsa.request_timestamp(b"hello")

        assert isinstance(response, TimeStampResponse)
        assert response.status == PKIStatus.GRANTED
        # The token is present, so the response can be attached to a bundle.
        assert response.time_stamp_token()
        assert response.tst_info is not None
