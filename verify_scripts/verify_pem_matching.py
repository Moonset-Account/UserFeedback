"""Synthetic-key verification for the CryptoSigner PEM matching fix.

Run from the repository root with:
    python3 -I verify_scripts/verify_pem_matching.py

Generates fresh key material locally (no external records are read), then
checks, for every supported key type:

* the same public key with different PEM line endings/newlines is accepted
* the caller-provided keyid and scheme are preserved
* a valid PEM of a *different* key pair raises KeyMismatchError
* a signer built from a re-formatted public key still signs/verifies
"""

from __future__ import annotations

import sys

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from securesystemslib.exceptions import KeyMismatchError
from securesystemslib.signer import CryptoSigner, SSlibKey

CALLER_KEYID = "caller-supplied-keyid"


def pem_variants(pem: str) -> list[tuple[str, str]]:
    return [
        ("canonical", pem),
        ("crlf", pem.replace("\n", "\r\n")),
        ("no-trailing-newline", pem.rstrip("\n")),
        ("extra-trailing-newline", pem + "\n"),
        ("crlf-no-final-break", pem.replace("\n", "\r\n").rstrip("\r\n")),
        ("leading-blank-line", "\n" + pem),
    ]


def public_pem(signer: CryptoSigner) -> str:
    return signer._private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def run_case(name: str, signer: CryptoSigner, other: CryptoSigner) -> None:
    keytype = signer.public_key.keytype
    scheme = signer.public_key.scheme
    matching_pem = public_pem(signer)
    foreign_pem = public_pem(other)

    # 1. same key, cosmetic PEM differences -> accepted, attributes preserved
    for label, variant in pem_variants(matching_pem):
        pub = SSlibKey(
            CALLER_KEYID, keytype, scheme, {"public": variant}
        )
        matched = CryptoSigner(signer._private_key, pub)
        assert matched.public_key is pub, f"{name}/{label}: object replaced"
        assert matched.public_key.keyid == CALLER_KEYID, (
            f"{name}/{label}: keyid not preserved"
        )
        assert matched.public_key.scheme == scheme, (
            f"{name}/{label}: scheme not preserved"
        )
        assert matched.public_key.keyval["public"] == variant, (
            f"{name}/{label}: keyval rewritten"
        )
        sig = matched.sign(b"DATA")
        pub.verify_signature(sig, b"DATA")
        print(f"  ok  same-key {name:<10} {label}")

    # 2. different key, valid PEM variants -> KeyMismatchError
    for label, variant in pem_variants(foreign_pem):
        pub = SSlibKey(
            CALLER_KEYID, keytype, scheme, {"public": variant}
        )
        try:
            CryptoSigner(signer._private_key, pub)
        except KeyMismatchError:
            print(f"  ok  foreign   {name:<10} {label} -> KeyMismatchError")
        else:
            raise AssertionError(
                f"{name}/{label}: foreign public key was accepted"
            )

    # 3. round-trip through private_bytes still works with a re-wrapped PEM
    loaded = SSlibKey(
        CALLER_KEYID,
        keytype,
        scheme,
        {"public": matching_pem.replace("\n", "\r\n")},
    )
    roundtrip = CryptoSigner(signer._private_key, loaded)
    assert roundtrip.private_bytes.startswith(b"-----BEGIN")
    print(f"  ok  roundtrip {name:<10}")


def main() -> int:
    cases = [
        ("rsa", CryptoSigner.generate_rsa()),
        ("ecdsa-p256", CryptoSigner.generate_ecdsa(
            scheme="ecdsa-sha2-nistp256")),
        ("ecdsa-p384", CryptoSigner.generate_ecdsa(
            scheme="ecdsa-sha2-nistp384")),
        ("ecdsa-p521", CryptoSigner.generate_ecdsa(
            scheme="ecdsa-sha2-nistp521")),
        ("ed25519", CryptoSigner.generate_ed25519()),
        ("mldsa44", CryptoSigner.generate_mldsa(scheme="ml-dsa-44/1")),
        ("mldsa65", CryptoSigner.generate_mldsa(scheme="ml-dsa-65/1")),
        ("mldsa87", CryptoSigner.generate_mldsa(scheme="ml-dsa-87/1")),
    ]

    others = {
        "rsa": CryptoSigner.generate_rsa(),
        "ecdsa-p256": CryptoSigner.generate_ecdsa(
            scheme="ecdsa-sha2-nistp256"),
        "ecdsa-p384": CryptoSigner.generate_ecdsa(
            scheme="ecdsa-sha2-nistp384"),
        "ecdsa-p521": CryptoSigner.generate_ecdsa(
            scheme="ecdsa-sha2-nistp521"),
        "ed25519": CryptoSigner.generate_ed25519(),
        "mldsa44": CryptoSigner.generate_mldsa(scheme="ml-dsa-44/1"),
        "mldsa65": CryptoSigner.generate_mldsa(scheme="ml-dsa-65/1"),
        "mldsa87": CryptoSigner.generate_mldsa(scheme="ml-dsa-87/1"),
    }

    for name, signer in cases:
        print(f"case {name}")
        run_case(name, signer, others[name])

    print("\nAll synthetic key checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
