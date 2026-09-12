"""A JWT signer for the tests, built straight on `cryptography`.

grelmicro never issues a token, so this lives in the suite rather than in the
package. Signing by hand rather than through a JWT library is deliberate: the
security tests need tokens a library refuses to produce, such as an unsigned
token, a token whose header disagrees with its signature, or a token signed
with the wrong key for its stated algorithm.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

HASHES = {
    "256": hashes.SHA256(),
    "384": hashes.SHA384(),
    "512": hashes.SHA512(),
}
DIGESTS = {"256": hashlib.sha256, "384": hashlib.sha384, "512": hashlib.sha512}
CURVE_SIZE = {"ES256": 32, "ES384": 48}


def b64u(raw: bytes) -> str:
    """Return `raw` as unpadded base64url, the encoding a JWS uses."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_json(value: Any) -> str:  # noqa: ANN401
    """Return `value` as a base64url-encoded compact JSON segment."""
    return b64u(json.dumps(value, separators=(",", ":")).encode())


class Signer:
    """Signs tokens, and forges the ones a JWT library would not emit."""

    def __init__(self) -> None:
        """Generate one key per algorithm family."""
        self.rsa = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        self.ec256 = ec.generate_private_key(ec.SECP256R1())
        self.ec384 = ec.generate_private_key(ec.SECP384R1())
        self.ed = ed25519.Ed25519PrivateKey.generate()
        self.secret = b"grelmicro-test-secret-0123456789abcdef"

    def public_pem(self, algorithm: str) -> bytes:
        """Return the verification key for `algorithm`, as grelmicro takes it."""
        if algorithm.startswith("HS"):
            return self.secret
        return (
            self._private(algorithm)
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    def public_jwk(
        self,
        algorithm: str = "RS256",
        **extra: Any,  # noqa: ANN401
    ) -> dict[str, Any]:
        """Return the RSA verification key as a JWK, the way a provider serves it."""
        numbers = self.rsa.public_key().public_numbers()
        jwk = {
            "kty": "RSA",
            "use": "sig",
            "alg": algorithm,
            "n": b64u(
                numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
            ),
            "e": b64u(
                numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
            ),
        }
        jwk.update(extra)
        return {name: value for name, value in jwk.items() if value is not None}

    def _private(self, algorithm: str) -> Any:  # noqa: ANN401
        """Return the private key that signs `algorithm`."""
        if algorithm.startswith(("RS", "PS")):
            return self.rsa
        if algorithm == "ES256":
            return self.ec256
        if algorithm == "ES384":
            return self.ec384
        if algorithm == "EdDSA":
            return self.ed
        msg = f"no key for {algorithm}"
        raise AssertionError(msg)

    def signature(self, algorithm: str, message: bytes) -> bytes:
        """Return the raw JWS signature of `message` under `algorithm`."""
        if algorithm.startswith("HS"):
            digest = DIGESTS[algorithm[2:]]
            return hmac.new(self.secret, message, digest).digest()
        if algorithm.startswith("RS"):
            return self.rsa.sign(
                message, padding.PKCS1v15(), HASHES[algorithm[2:]]
            )
        if algorithm.startswith("PS"):
            chosen = HASHES[algorithm[2:]]
            return self.rsa.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(chosen),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                chosen,
            )
        if algorithm.startswith("ES"):
            key = self._private(algorithm)
            der = key.sign(message, ec.ECDSA(HASHES[algorithm[2:]]))
            r, s = decode_dss_signature(der)
            size = CURVE_SIZE[algorithm]
            return r.to_bytes(size, "big") + s.to_bytes(size, "big")
        if algorithm == "EdDSA":
            return self.ed.sign(message)
        msg = f"cannot sign {algorithm}"
        raise AssertionError(msg)

    def token(
        self,
        claims: Mapping[str, Any],
        *,
        algorithm: str = "RS256",
        header: Mapping[str, Any] | None = None,
        sign_with: str | None = None,
        secret: bytes | None = None,
    ) -> str:
        """Return a signed token.

        `sign_with` signs under a different algorithm than the header states,
        and `secret` signs an `HS*` token with key material of your choosing.
        Both exist to build algorithm-confusion tokens.
        """
        head = {"alg": algorithm, "typ": "JWT"}
        if header:
            head = {**head, **header}
            head = {k: v for k, v in head.items() if v is not None}
        payload = {k: v for k, v in claims.items() if v is not None}
        message = f"{b64u_json(head)}.{b64u_json(payload)}"
        using = sign_with or algorithm
        if secret is not None:
            previous, self.secret = self.secret, secret
            try:
                raw = self.signature(using, message.encode())
            finally:
                self.secret = previous
        else:
            raw = self.signature(using, message.encode())
        return f"{message}.{b64u(raw)}"

    def unsigned(
        self,
        claims: Mapping[str, Any],
        *,
        algorithm: str = "none",
        signature: str = "",
    ) -> str:
        """Return a token with no valid signature, for the `alg:none` family."""
        head = b64u_json({"alg": algorithm, "typ": "JWT"})
        payload = {k: v for k, v in claims.items() if v is not None}
        return f"{head}.{b64u_json(payload)}.{signature}"
