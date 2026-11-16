"""Unit tests for AWSSigner using a mocked AWS KMS client.

These tests do not require AWS credentials or a running KMS. The mocked KMS
client signs the digest it receives with the matching local private key, so
that the resulting signatures can be verified with the securesystemslib
verifier (which always verifies against the original payload).
"""

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from securesystemslib.exceptions import UnverifiedSignatureError
from securesystemslib.signer import AWSSigner, SSlibKey, Signer

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    # Map hashlib hash names to the matching pyca/cryptography hash classes
    HASH_CLASSES = {
        "sha256": hashes.SHA256,
        "sha384": hashes.SHA384,
        "sha512": hashes.SHA512,
    }

    BOTO_IMPORT_ERROR = None
except ImportError:
    BOTO_IMPORT_ERROR = "boto3/botocore and cryptography are required for these tests"


PEMS_DIR = Path(__file__).parent / "data" / "pems"
AWS_KEY_ID = "alias/test-key"

# A short payload and a payload larger than the AWS KMS raw message limit
# (4096 bytes): both must produce verifiable signatures via the digest flow.
PAYLOADS = [b"DATA", b"DATA" * 2048]

# All securesystemslib schemes supported by AWSSigner with their expected
# AWS KMS signing algorithm and hash algorithm.
SCHEMES = [
    ("ecdsa-sha2-nistp256", "ECDSA_SHA_256", "sha256"),
    ("ecdsa-sha2-nistp384", "ECDSA_SHA_384", "sha384"),
    ("rsassa-pss-sha256", "RSASSA_PSS_SHA_256", "sha256"),
    ("rsassa-pss-sha384", "RSASSA_PSS_SHA_384", "sha384"),
    ("rsassa-pss-sha512", "RSASSA_PSS_SHA_512", "sha512"),
    ("rsa-pkcs1v15-sha256", "RSASSA_PKCS1_V1_5_SHA_256", "sha256"),
    ("rsa-pkcs1v15-sha384", "RSASSA_PKCS1_V1_5_SHA_384", "sha384"),
    ("rsa-pkcs1v15-sha512", "RSASSA_PKCS1_V1_5_SHA_512", "sha512"),
]


class MockKMSClient:
    """Mock of the boto3 KMS client used by AWSSigner.

    ``sign`` records the request and signs the received digest with the
    matching local private key, emulating AWS KMS digest signing.
    """

    def __init__(self, private_keys):
        self._private_keys = private_keys
        self.sign_calls = []

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
        self.sign_calls.append(
            {
                "KeyId": KeyId,
                "Message": Message,
                "MessageType": MessageType,
                "SigningAlgorithm": SigningAlgorithm,
            }
        )
        private_key = self._private_keys[KeyId]
        hash_name = SigningAlgorithm.split("_")[-1].lower()
        hash_class = HASH_CLASSES[hash_name]

        if isinstance(private_key, rsa.RSAPrivateKey):
            if "PSS" in SigningAlgorithm:
                rsa_padding = padding.PSS(
                    mgf=padding.MGF1(hash_class()),
                    salt_length=padding.PSS.AUTO,
                )
            else:
                rsa_padding = padding.PKCS1v15()
            signature = private_key.sign(
                Message, rsa_padding, Prehashed(hash_class())
            )
        else:
            signature = private_key.sign(
                Message, ec.ECDSA(Prehashed(hash_class()))
            )

        return {"Signature": signature}


@unittest.skipIf(BOTO_IMPORT_ERROR, BOTO_IMPORT_ERROR)
class TestAWSSigner(unittest.TestCase):
    """Test AWSSigner digest signing against a mocked KMS client"""

    @classmethod
    def setUpClass(cls):
        with open(PEMS_DIR / "rsa_private.pem", "rb") as f:
            cls.rsa_private_key = load_pem_private_key(f.read(), password=None)
        with open(PEMS_DIR / "ecdsa_private.pem", "rb") as f:
            cls.ecdsa_p256_private_key = load_pem_private_key(
                f.read(), password=None
            )
        with open(PEMS_DIR / "ecdsa_secp384r1_private.pem", "rb") as f:
            cls.ecdsa_p384_private_key = load_pem_private_key(
                f.read(), password=None
            )

    def _make_signer(self, scheme, mock_client):
        if scheme.startswith("ecdsa-sha2-nistp256"):
            private_key = self.ecdsa_p256_private_key
        elif scheme.startswith("ecdsa"):
            private_key = self.ecdsa_p384_private_key
        else:
            private_key = self.rsa_private_key

        public_key = SSlibKey.from_crypto(
            private_key.public_key(), scheme=scheme
        )

        with patch.object(boto3, "client", return_value=mock_client):
            return AWSSigner(AWS_KEY_ID, public_key)

    def test_sign_digest_request_and_verify_for_all_schemes_and_lengths(self):
        # One mock KMS client serves every key id used below
        private_keys = {AWS_KEY_ID: None}
        mock_client = MockKMSClient(private_keys)

        for scheme, aws_algo, hash_name in SCHEMES:
            if scheme == "ecdsa-sha2-nistp256":
                private_keys[AWS_KEY_ID] = self.ecdsa_p256_private_key
            elif scheme.startswith("ecdsa"):
                private_keys[AWS_KEY_ID] = self.ecdsa_p384_private_key
            else:
                private_keys[AWS_KEY_ID] = self.rsa_private_key

            signer = self._make_signer(scheme, mock_client)

            # The signer pairs the scheme with the expected KMS algorithm
            # and hash algorithm
            self.assertEqual(signer.aws_algo, aws_algo)
            self.assertEqual(signer.hash_algorithm, hash_name)

            for payload in PAYLOADS:
                signature = signer.sign(payload)

                # The KMS request sends the payload digest, never the raw
                # payload, regardless of payload length
                request = mock_client.sign_calls[-1]
                expected_digest = hashlib.new(hash_name, payload).digest()

                self.assertEqual(request["KeyId"], AWS_KEY_ID)
                self.assertEqual(request["MessageType"], "DIGEST")
                self.assertEqual(request["SigningAlgorithm"], aws_algo)
                self.assertEqual(request["Message"], expected_digest)
                self.assertNotEqual(request["Message"], payload)
                # A digest is fixed length and always small
                self.assertEqual(
                    len(request["Message"]),
                    hashlib.new(hash_name).digest_size,
                )
                self.assertLessEqual(len(request["Message"]), 64)

                # The signature result structure is unchanged
                self.assertEqual(signature.keyid, signer.public_key.keyid)
                self.assertEqual(
                    signature.signature,
                    bytes.fromhex(signature.signature).hex(),
                )

                # The verifier still takes the original payload as input
                self.assertIsNone(
                    signer.public_key.verify_signature(signature, payload)
                )
                with self.assertRaises(UnverifiedSignatureError):
                    signer.public_key.verify_signature(
                        signature, b"NOT " + payload
                    )

    def test_long_payload_does_not_exceed_raw_message_limit(self):
        # Explicitly cover a payload above the KMS RAW message size limit
        mock_client = MockKMSClient({AWS_KEY_ID: self.rsa_private_key})
        signer = self._make_signer("rsassa-pss-sha256", mock_client)

        payload = b"x" * 10000
        signature = signer.sign(payload)

        request = mock_client.sign_calls[-1]
        self.assertEqual(request["MessageType"], "DIGEST")
        self.assertEqual(
            request["Message"], hashlib.sha256(payload).digest()
        )
        self.assertLess(len(request["Message"]), len(payload))

        self.assertIsNone(
            signer.public_key.verify_signature(signature, payload)
        )

    def test_from_priv_key_uri(self):
        mock_client = MockKMSClient({AWS_KEY_ID: self.rsa_private_key})
        public_key = SSlibKey.from_crypto(
            self.rsa_private_key.public_key(), scheme="rsassa-pss-sha256"
        )
        with patch.object(boto3, "client", return_value=mock_client):
            signer = Signer.from_priv_key_uri(
                f"{AWSSigner.SCHEME}:{AWS_KEY_ID}", public_key
            )

        self.assertIsInstance(signer, AWSSigner)
        signature = signer.sign(b"DATA")
        self.assertIsNone(public_key.verify_signature(signature, b"DATA"))

    def test_service_errors_are_propagated(self):
        # Existing error handling must remain compatible: BotoCoreError and
        # ClientError raised by the client are propagated unchanged.
        error_client = _ErrorKMSClient(
            ClientError(
                {"Error": {"Code": "ValidationException", "Message": "boom"}},
                "Sign",
            )
        )
        signer = self._make_signer("rsassa-pss-sha256", error_client)
        with self.assertRaises(ClientError):
            signer.sign(b"DATA")

        boto_error_client = _ErrorKMSClient(BotoCoreError())
        signer = self._make_signer("rsassa-pss-sha256", boto_error_client)
        with self.assertRaises(BotoCoreError):
            signer.sign(b"DATA")


class _ErrorKMSClient:
    """KMS client mock whose ``sign`` always raises the given error."""

    def __init__(self, error):
        self._error = error

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
        raise self._error


if __name__ == "__main__":
    unittest.main(verbosity=2)
