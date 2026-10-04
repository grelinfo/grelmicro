import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from grelmicro.security import JWTKey, JWTVerifier

private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
public_pem = private_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign(claims: dict[str, object]) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = ".".join(
        b64url(json.dumps(part).encode()) for part in (header, claims)
    )
    signature = private_key.sign(
        signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{b64url(signature)}"


verifier = JWTVerifier.keys(
    JWTKey.pem(public_pem, algorithm="RS256"), audience="orders-api"
)


def test_a_signed_token_verifies() -> None:
    token = sign(
        {"sub": "alice", "aud": "orders-api", "exp": int(time.time()) + 60}
    )
    assert verifier.verify(token).subject == "alice"
