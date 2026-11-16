"""Test HSMSigner"""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import pkcs11
from asn1crypto.keys import (
    ECDomainParameters,
    NamedCurve,
)
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, SECP384R1
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
)
from cryptography.hazmat.primitives.hashes import SHA256

from securesystemslib.exceptions import UnverifiedSignatureError
from securesystemslib.signer import HSMSigner, Signer, SSlibKey


@unittest.skipUnless(os.environ.get("PYKCS11LIB"), "set PYKCS11LIB to SoftHSM lib path")
class TestHSM(unittest.TestCase):
    """Test HSMSigner with SoftHSM

    Requirements:
    - install SoftHSM2
    - set environment variable ``PYKCS11LIB`` to SoftHSM library path

    See .github/workflows/hsm.yml for how this can be done on Linux, macOS and Windows.
    """

    hsm_keyid = 1
    hsm_keyid_default = 2
    hsm_keyid_odd = 258
    hsm_keyid_priv = 512
    hsm_user_pin = "123456"
    token_label = "Test SoftHSM"

    @staticmethod
    def _generate_key_pair(session, keyid, curve, is_pubkey_private=False):
        "Create ecdsa key pair on hsm"
        params = ECDomainParameters(name="named", value=NamedCurve(curve.name)).dump()
        cka_id = pkcs11.util.biginteger(keyid)

        public_template = {
            pkcs11.Attribute.CLASS: pkcs11.ObjectClass.PUBLIC_KEY,
            pkcs11.Attribute.PRIVATE: is_pubkey_private,
            pkcs11.Attribute.TOKEN: True,
            pkcs11.Attribute.ENCRYPT: False,
            pkcs11.Attribute.VERIFY: True,
            pkcs11.Attribute.WRAP: False,
            pkcs11.Attribute.KEY_TYPE: pkcs11.KeyType.EC,
            pkcs11.Attribute.EC_PARAMS: params,
            pkcs11.Attribute.LABEL: curve.name,
            pkcs11.Attribute.ID: cka_id,
        }
        private_template = {
            pkcs11.Attribute.CLASS: pkcs11.ObjectClass.PRIVATE_KEY,
            pkcs11.Attribute.KEY_TYPE: pkcs11.KeyType.EC,
            pkcs11.Attribute.TOKEN: True,
            pkcs11.Attribute.SENSITIVE: True,
            pkcs11.Attribute.DECRYPT: False,
            pkcs11.Attribute.SIGN: True,
            pkcs11.Attribute.UNWRAP: False,
            pkcs11.Attribute.LABEL: curve.name,
            pkcs11.Attribute.ID: cka_id,
        }

        session.generate_keypair(
            pkcs11.KeyType.EC,
            256,
            store=True,
            mechanism=pkcs11.Mechanism.EC_KEY_PAIR_GEN,
            public_template=public_template,
            private_template=private_template,
        )

    @classmethod
    def setUpClass(cls):
        """Initialize SoftHSM token and generate ecdsa test keys"""

        # Configure SoftHSM to create test token in temporary test directory
        cls.original_cwd = os.getcwd()
        cls.test_dir = os.path.realpath(tempfile.mkdtemp())
        os.chdir(cls.test_dir)

        with open("softhsm2.conf", "w", encoding="utf-8") as f:
            f.write("directories.tokendir = " + os.path.join(cls.test_dir, ""))
        os.environ["SOFTHSM2_CONF"] = os.path.join(cls.test_dir, "softhsm2.conf")

        # Initialize the token using softhsm2-util
        subprocess.run(
            [
                "softhsm2-util",
                "--init-token",
                "--slot",
                "0",
                "--label",
                cls.token_label,
                "--so-pin",
                "abcd",
                "--pin",
                cls.hsm_user_pin,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

        lib = pkcs11.lib(os.environ["PYKCS11LIB"])
        token = lib.get_token(token_label=cls.token_label)

        with token.open(rw=True, user_pin=cls.hsm_user_pin) as session:
            # Generate test ecdsa key pairs for curves secp256r1 and secp384r1 on test token
            cls._generate_key_pair(session, cls.hsm_keyid, SECP256R1)
            cls._generate_key_pair(session, cls.hsm_keyid_default, SECP384R1)
            cls._generate_key_pair(session, cls.hsm_keyid_odd, SECP256R1)
            cls._generate_key_pair(
                session, cls.hsm_keyid_priv, SECP256R1, is_pubkey_private=True
            )

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.original_cwd)
        shutil.rmtree(cls.test_dir)
        del os.environ["SOFTHSM2_CONF"]

    def test_hsm(self):
        """Test HSM key export and signing."""

        for hsm_keyid in [self.hsm_keyid, self.hsm_keyid_default, self.hsm_keyid_odd]:
            _, key = HSMSigner.import_(hsm_keyid, self.token_label)
            signer = HSMSigner(
                hsm_keyid, key, lambda sec: self.hsm_user_pin, self.token_label
            )
            sig = signer.sign(b"DATA")
            key.verify_signature(sig, b"DATA")

            with self.assertRaises(UnverifiedSignatureError):
                key.verify_signature(sig, b"NOT DATA")

    def test_hsm_uri(self):
        """Test HSM default key export and signing from URI."""

        # default import
        uri, key = HSMSigner.import_()
        signer = Signer.from_priv_key_uri(uri, key, lambda sec: self.hsm_user_pin)
        sig = signer.sign(b"DATA")
        key.verify_signature(sig, b"DATA")
        with self.assertRaises(UnverifiedSignatureError):
            key.verify_signature(sig, b"NOT DATA")

        # import using arguments
        uri, key = HSMSigner.import_(self.hsm_keyid_default, self.token_label)
        signer = Signer.from_priv_key_uri(uri, key, lambda sec: self.hsm_user_pin)
        sig = signer.sign(b"DATA")
        key.verify_signature(sig, b"DATA")
        with self.assertRaises(UnverifiedSignatureError):
            key.verify_signature(sig, b"NOT DATA")

    def test_hsm_private_pubkey(self):
        """Test import_() PIN fallback when public key has CKA_PRIVATE=True."""

        # Test pub key not found without pin
        with self.assertRaises(Exception) as context:
            _, _ = HSMSigner.import_(self.hsm_keyid_priv, self.token_label)
        self.assertIn("No keys found unauthenticated", str(context.exception))

        # Test with bad pin
        bad_pinh = mock.Mock(return_value="987")
        with self.assertRaises(Exception) as context:
            _, _ = HSMSigner.import_(
                self.hsm_keyid_priv, self.token_label, secrets_handler=bad_pinh
            )
        bad_pinh.assert_called_once()

        # And check sign ok cases (good pin or not required pin)
        for hsm_keyid in [
            self.hsm_keyid,
            self.hsm_keyid_default,
            self.hsm_keyid_odd,
            self.hsm_keyid_priv,
        ]:
            good_pinh = mock.Mock(return_value=self.hsm_user_pin)
            _, key = HSMSigner.import_(
                hsm_keyid, self.token_label, secrets_handler=good_pinh
            )
            # Pin handler must be called only if public key is private
            if hsm_keyid == self.hsm_keyid_priv:
                good_pinh.assert_called_once()
            else:
                good_pinh.assert_not_called()

            signer = HSMSigner(hsm_keyid, key, good_pinh, self.token_label)
            sig = signer.sign(b"DATA")
            key.verify_signature(sig, b"DATA")

            with self.assertRaises(UnverifiedSignatureError):
                key.verify_signature(sig, b"NOT DATA")


class _FakeSession:
    """Minimal stand-in for a pkcs11.Session context manager."""

    def __init__(self, objects=()):
        self._objects = list(objects)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def get_objects(self, template=None):
        return iter(list(self._objects))


class _FakeToken:
    """Minimal stand-in for a pkcs11.Token."""

    def __init__(self, label, session=None):
        self.label = label
        self.session = session

    def open(self, user_pin=None, rw=False):
        return self.session


class _FakeLib:
    """Minimal stand-in for a pkcs11.Lib, filters tokens by label."""

    def __init__(self, tokens):
        self._tokens = list(tokens)

    def get_tokens(self, token_label=None, token_flags=None, **kwargs):
        tokens = self._tokens
        if token_label is not None:
            tokens = [t for t in tokens if t.label == token_label]
        return iter(tokens)


class TestHSMLookup(unittest.TestCase):
    """Test HSMSigner token/key lookups with a mocked PKCS#11 device.

    Unlike TestHSM this does not need SoftHSM: it verifies that token,
    public key and signing key lookups distinguish between no match, a
    unique match and multiple matches, and that opened sessions are closed.
    """

    token_label = "Mock Token"
    hsm_keyid = 1

    @classmethod
    def setUpClass(cls):
        cls.crypto_key = ec.generate_private_key(SECP256R1())
        cls.public_key = SSlibKey.from_crypto(cls.crypto_key.public_key())

    def setUp(self):
        env_patcher = mock.patch.dict(
            os.environ, {"PYKCS11LIB": "/fake/pkcs11.so"}
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _patch_lib(self, tokens):
        lib_patcher = mock.patch.object(
            pkcs11, "lib", return_value=_FakeLib(tokens)
        )
        lib_patcher.start()
        self.addCleanup(lib_patcher.stop)

    def _signing_key(self):
        """Mock pkcs11 signing key that produces real ECDSA signatures."""
        key = mock.Mock(spec=pkcs11.SignMixin)

        def sign(data, mechanism=None):
            der_sig = self.crypto_key.sign(data, ec.ECDSA(Prehashed(SHA256())))
            r, s = decode_dss_signature(der_sig)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")

        key.sign.side_effect = sign
        return key

    def _signer(self, pin_handler=None):
        return HSMSigner(
            self.hsm_keyid,
            self.public_key,
            pin_handler or (lambda _: "123456"),
            self.token_label,
        )

    def test_find_token_no_match(self):
        self._patch_lib([])
        with self.assertRaises(ValueError) as ctx:
            HSMSigner._find_token(self.token_label)
        self.assertIn("No PKCS#11 token found", str(ctx.exception))

    def test_find_token_multiple_matches(self):
        self._patch_lib([_FakeToken(self.token_label), _FakeToken(self.token_label)])
        with self.assertRaises(ValueError) as ctx:
            HSMSigner._find_token(self.token_label)
        self.assertIn("Multiple PKCS#11 tokens found", str(ctx.exception))

    def test_find_token_unique_match(self):
        token = _FakeToken(self.token_label)
        self._patch_lib([token, _FakeToken("Other Token")])
        self.assertIs(HSMSigner._find_token(self.token_label), token)

    def test_sign_unique_match(self):
        """Unique token and key: signing works and the session is closed."""
        session = _FakeSession([self._signing_key()])
        self._patch_lib([_FakeToken(self.token_label, session)])

        pin_handler = mock.Mock(return_value="123456")
        sig = self._signer(pin_handler).sign(b"DATA")

        self.public_key.verify_signature(sig, b"DATA")
        pin_handler.assert_called_once_with(HSMSigner.SECRETS_HANDLER_MSG)
        self.assertTrue(session.closed)

    def test_sign_duplicate_token_label(self):
        """Two tokens with the same label: the error names the token stage."""
        session = _FakeSession([self._signing_key()])
        self._patch_lib(
            [
                _FakeToken(self.token_label, session),
                _FakeToken(self.token_label, session),
            ]
        )

        with self.assertRaises(ValueError) as ctx:
            self._signer().sign(b"DATA")
        self.assertIn("Multiple PKCS#11 tokens found", str(ctx.exception))
        # the lookup failed before any session was opened
        self.assertFalse(session.closed)

    def test_sign_multiple_private_keys(self):
        session = _FakeSession([self._signing_key(), self._signing_key()])
        self._patch_lib([_FakeToken(self.token_label, session)])

        with self.assertRaises(ValueError) as ctx:
            self._signer().sign(b"DATA")
        self.assertIn("2 ECDSA private keys", str(ctx.exception))
        # the session opened before the failed lookup must be closed
        self.assertTrue(session.closed)

    def test_sign_no_private_key(self):
        session = _FakeSession([])
        self._patch_lib([_FakeToken(self.token_label, session)])

        with self.assertRaises(ValueError) as ctx:
            self._signer().sign(b"DATA")
        self.assertIn("could not find ECDSA private key", str(ctx.exception))
        self.assertTrue(session.closed)

    def test_import_multiple_public_keys(self):
        session = _FakeSession(
            [mock.Mock(spec=pkcs11.PublicKey), mock.Mock(spec=pkcs11.PublicKey)]
        )
        self._patch_lib([_FakeToken(self.token_label, session)])

        with self.assertRaises(ValueError) as ctx:
            HSMSigner.import_(
                self.hsm_keyid,
                self.token_label,
                secrets_handler=lambda _: "123456",
            )
        self.assertIn("2 ECDSA public keys", str(ctx.exception))
        self.assertTrue(session.closed)

    def test_import_no_public_key_unauthenticated(self):
        session = _FakeSession([])
        self._patch_lib([_FakeToken(self.token_label, session)])

        with self.assertRaises(ValueError) as ctx:
            HSMSigner.import_(self.hsm_keyid, self.token_label)
        self.assertIn("No keys found unauthenticated", str(ctx.exception))
        self.assertTrue(session.closed)


# Run the unit tests.
if __name__ == "__main__":
    unittest.main()
