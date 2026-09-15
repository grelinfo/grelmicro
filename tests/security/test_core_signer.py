"""The compiled signer behind a client's signed assertion.

Every signature the core makes is checked by an implementation that did not
make it, `cryptography`, and by the core's own verifier, so a signer and a
verifier sharing one mistake cannot pass each other.

The refusals enumerate the keys a signer must never accept. Each is a key a
deployment can hand it by mistake, and each must fail when the signer is
built, not at its first signature.
"""

from __future__ import annotations

import base64
import importlib
import json
import threading
import time
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

type PrivateKey = (
    rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey
)

CORE: Any = importlib.import_module("grelmicro_core")
"""The compiled core, typed the way `grelmicro.security.jwt` reads it.

The extension ships no type stubs, so a type checker sees none of its classes.
"""

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
P256_KEY = ec.generate_private_key(ec.SECP256R1())
P384_KEY = ec.generate_private_key(ec.SECP384R1())
ED25519_KEY = ed25519.Ed25519PrivateKey.generate()

HASHES = {
    "256": hashes.SHA256(),
    "384": hashes.SHA384(),
    "512": hashes.SHA512(),
}
CURVE_BYTES = {"ES256": 32, "ES384": 48}

KEYS: dict[str, PrivateKey] = {
    "RS256": RSA_KEY,
    "RS384": RSA_KEY,
    "RS512": RSA_KEY,
    "PS256": RSA_KEY,
    "PS384": RSA_KEY,
    "PS512": RSA_KEY,
    "ES256": P256_KEY,
    "ES384": P384_KEY,
    "EdDSA": ED25519_KEY,
}
ALGORITHMS = sorted(KEYS)


def pkcs8(key: PrivateKey) -> bytes:
    """Return `key` as an unencrypted PKCS #8 PEM document."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def traditional(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> bytes:
    """Return `key` as PKCS #1 for RSA or SEC 1 for EC."""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def public_pem(key: PrivateKey) -> bytes:
    """Return the public half of `key` as the PEM a verifier takes."""
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def verify(algorithm: str, data: bytes, signature: bytes) -> None:
    """Verify `signature` over `data` with `cryptography`, or raise."""
    key = KEYS[algorithm]
    if isinstance(key, rsa.RSAPrivateKey):
        chosen = HASHES[algorithm[2:]]
        scheme = (
            padding.PKCS1v15()
            if algorithm.startswith("RS")
            else padding.PSS(
                mgf=padding.MGF1(chosen),
                salt_length=padding.PSS.DIGEST_LENGTH,
            )
        )
        key.public_key().verify(signature, data, scheme, chosen)
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        size = CURVE_BYTES[algorithm]
        r = int.from_bytes(signature[:size], "big")
        s = int.from_bytes(signature[size:], "big")
        key.public_key().verify(
            encode_dss_signature(r, s),
            data,
            ec.ECDSA(HASHES[algorithm[2:]]),
        )
    else:
        key.public_key().verify(signature, data)


def b64u(raw: bytes) -> str:
    """Return `raw` as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def refusal(algorithm: str, key: bytes) -> str:
    """Assert building a signer from `key` is refused, and return why."""
    with pytest.raises(ValueError, match=r".+") as caught:
        CORE.Signer(algorithm, key)
    return str(caught.value)


class TestSignatures:
    """A signature the core makes verifies everywhere it should."""

    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_cryptography_verifies_it(self, algorithm: str) -> None:
        """An independent implementation accepts every algorithm's signature."""
        signer = CORE.Signer(algorithm, pkcs8(KEYS[algorithm]))
        data = b"header.claims"

        verify(algorithm, data, signer.sign(data))

    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_core_verifier_accepts_a_token_it_signed(
        self, algorithm: str
    ) -> None:
        """A token signed by the core verifies under the core's own verifier."""
        signer = CORE.Signer(algorithm, pkcs8(KEYS[algorithm]))
        header = b64u(json.dumps({"alg": algorithm, "typ": "JWT"}).encode())
        claims = b64u(
            json.dumps(
                {"sub": "orders-api", "exp": int(time.time()) + 60}
            ).encode()
        )
        signing_input = f"{header}.{claims}"
        token = f"{signing_input}.{b64u(signer.sign(signing_input.encode()))}"
        verifier = CORE.Verifier(
            [(None, algorithm, public_pem(KEYS[algorithm]), "pem")]
        )

        assert verifier.verify(token)["sub"] == "orders-api"

    @pytest.mark.parametrize(
        ("algorithm", "width"), [("ES256", 64), ("ES384", 96)]
    )
    def test_ecdsa_signature_is_fixed_width(
        self, algorithm: str, width: int
    ) -> None:
        """An ECDSA signature is `r || s`, the form a JWS carries, never DER."""
        signer = CORE.Signer(algorithm, pkcs8(KEYS[algorithm]))

        assert len(signer.sign(b"data")) == width

    def test_rsa_signature_is_modulus_width(self) -> None:
        """An RSA signature is as long as the key's modulus."""
        signer = CORE.Signer("RS256", pkcs8(RSA_KEY))

        assert len(signer.sign(b"data")) == RSA_KEY.key_size // 8

    def test_pss_is_randomized_and_pkcs1_is_not(self) -> None:
        """PSS salts every signature, and PKCS #1 v1.5 signs deterministically."""
        pss = CORE.Signer("PS256", pkcs8(RSA_KEY))
        pkcs1 = CORE.Signer("RS256", pkcs8(RSA_KEY))

        assert pss.sign(b"data") != pss.sign(b"data")
        assert pkcs1.sign(b"data") == pkcs1.sign(b"data")

    @pytest.mark.parametrize("algorithm", ["RS256", "PS256", "ES256", "EdDSA"])
    @settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.binary(max_size=4096))
    def test_any_bytes_sign_and_verify(
        self, algorithm: str, data: bytes
    ) -> None:
        """Whatever the input, the signature verifies over exactly that input."""
        signer = CORE.Signer(algorithm, pkcs8(KEYS[algorithm]))

        verify(algorithm, data, signer.sign(data))

    def test_one_signer_serves_many_threads(self) -> None:
        """A signer shared across threads signs correctly on every one of them."""
        signer = CORE.Signer("ES256", pkcs8(P256_KEY))
        failures: list[BaseException] = []

        def work(index: int) -> None:
            data = f"thread-{index}".encode()
            try:
                for _ in range(50):
                    verify("ES256", data, signer.sign(data))
            except Exception as error:  # noqa: BLE001
                failures.append(error)

        threads = [
            threading.Thread(target=work, args=(index,)) for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []


class TestKeyFormats:
    """Every format a provider or a tool hands out is read."""

    def test_rsa_pkcs1_is_read(self) -> None:
        """An `RSA PRIVATE KEY` document signs as well as PKCS #8 does."""
        signer = CORE.Signer("RS256", traditional(RSA_KEY))

        verify("RS256", b"data", signer.sign(b"data"))

    def test_ec_sec1_is_read(self) -> None:
        """An `EC PRIVATE KEY` document signs as well as PKCS #8 does."""
        signer = CORE.Signer("ES256", traditional(P256_KEY))

        verify("ES256", b"data", signer.sign(b"data"))

    def test_ed25519_pkcs8_v1_is_read(self) -> None:
        """An Ed25519 key without its public half, as most tools write it, signs."""
        signer = CORE.Signer("EdDSA", pkcs8(ED25519_KEY))

        verify("EdDSA", b"data", signer.sign(b"data"))

    def test_crlf_line_endings_are_read(self) -> None:
        """A PEM saved with Windows line endings is the same key."""
        document = pkcs8(P256_KEY).replace(b"\n", b"\r\n")
        signer = CORE.Signer("ES256", document)

        verify("ES256", b"data", signer.sign(b"data"))


def corrupt_rsa_key(**changes: int) -> bytes:
    """Return `RSA_KEY` with some private numbers changed, as PKCS #8."""
    numbers = RSA_KEY.private_numbers()
    fields = {
        "p": numbers.p,
        "q": numbers.q,
        "d": numbers.d,
        "dmp1": numbers.dmp1,
        "dmq1": numbers.dmq1,
        "iqmp": numbers.iqmp,
    }
    fields.update(changes)
    broken = rsa.RSAPrivateNumbers(
        public_numbers=numbers.public_numbers, **fields
    ).private_key(unsafe_skip_rsa_key_validation=True)
    return pkcs8(broken)


class TestRefusals:
    """A key that cannot sign correctly is refused when the signer is built."""

    def test_encrypted_key_is_refused_and_says_so(self) -> None:
        """A passphrase-protected key is refused with a message naming why."""
        document = P256_KEY.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        )

        assert "encrypted" in refusal("ES256", document)

    def test_non_pem_is_refused(self) -> None:
        """Bytes that are not a PEM document are refused."""
        refusal("RS256", b"not a key")

    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_public_key_is_refused(self, algorithm: str) -> None:
        """A public key cannot sign, whatever the algorithm."""
        refusal(algorithm, public_pem(KEYS[algorithm]))

    @pytest.mark.parametrize(
        ("algorithm", "document"),
        [
            ("RS256", pkcs8(P256_KEY)),
            ("PS256", pkcs8(ED25519_KEY)),
            ("RS256", traditional(P256_KEY)),
            ("ES256", pkcs8(RSA_KEY)),
            ("ES256", traditional(RSA_KEY)),
            ("ES256", pkcs8(P384_KEY)),
            ("ES384", pkcs8(P256_KEY)),
            ("ES384", traditional(P256_KEY)),
            ("EdDSA", pkcs8(RSA_KEY)),
            ("EdDSA", pkcs8(P256_KEY)),
            ("EdDSA", traditional(P256_KEY)),
        ],
        ids=lambda value: value if isinstance(value, str) else "key",
    )
    def test_key_of_another_kind_is_refused(
        self, algorithm: str, document: bytes
    ) -> None:
        """A key never signs under an algorithm or a curve it does not belong to."""
        refusal(algorithm, document)

    def test_short_rsa_key_is_refused(self) -> None:
        """An RSA key under 2048 bits is refused."""
        short = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505

        refusal("RS256", pkcs8(short))

    @pytest.mark.parametrize(
        "changes",
        [{"d": 12345}, {"dmp1": 3}, {"iqmp": 7}],
        ids=["d", "dmp1", "iqmp"],
    )
    def test_corrupt_rsa_key_is_refused(self, changes: dict[str, int]) -> None:
        """An RSA key whose numbers disagree fails at build, not at first use."""
        refusal("RS256", corrupt_rsa_key(**changes))

    @pytest.mark.parametrize(
        "algorithm", ["HS256", "none", "ES512", "rs256", "", "EDDSA"]
    )
    def test_unsupported_algorithm_is_refused(self, algorithm: str) -> None:
        """Only the asymmetric algorithms verification accepts can sign."""
        assert "unsupported" in refusal(algorithm, pkcs8(RSA_KEY))

    def test_no_refusal_quotes_the_key(self) -> None:
        """A refusal message never carries key material, since it reaches logs."""
        cases = [
            ("ES256", pkcs8(RSA_KEY)),
            ("RS256", pkcs8(P256_KEY)),
            ("EdDSA", traditional(P256_KEY)),
            ("RS256", corrupt_rsa_key(d=12345)),
            ("HS256", pkcs8(RSA_KEY)),
        ]
        for algorithm, document in cases:
            message = refusal(algorithm, document)
            body = [
                line
                for line in document.decode().splitlines()
                if line and not line.startswith("-----")
            ]
            assert not any(line in message for line in body)


P256_PARAMETERS = (
    b"-----BEGIN EC PARAMETERS-----\n"
    b"BggqhkjOPQMBBw==\n"
    b"-----END EC PARAMETERS-----\n"
)
"""The P-256 curve parameters `openssl ecparam -genkey` writes ahead of a key."""


class TestKeyFiles:
    """A key file can hold other blocks, and the private key is the one read."""

    def test_key_after_curve_parameters_is_read(self) -> None:
        """The file `openssl ecparam -genkey` writes signs as its key alone does."""
        signer = CORE.Signer("ES256", P256_PARAMETERS + traditional(P256_KEY))

        verify("ES256", b"data", signer.sign(b"data"))

    def test_key_after_another_block_is_read(self) -> None:
        """A public key or certificate ahead of the private key is passed over."""
        document = public_pem(RSA_KEY) + pkcs8(RSA_KEY)
        signer = CORE.Signer("RS256", document)

        verify("RS256", b"data", signer.sign(b"data"))

    def test_file_with_no_private_key_is_refused(self) -> None:
        """Curve parameters alone are not a key, and the refusal says so."""
        assert "no private key" in refusal("ES256", P256_PARAMETERS)
