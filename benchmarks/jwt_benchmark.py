"""Benchmark the `JWTVerifier` request path.

Covers the per-algorithm verification cost and the cache hit that most
requests take. The design decisions these numbers drove are written up in
`docs/architecture/jwt.md`.

Run with: python benchmarks/jwt_benchmark.py
"""

from __future__ import annotations

import base64
import gc
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)

from grelmicro.security import JWTConfig, JWTKey, JWTVerifier

if TYPE_CHECKING:
    from collections.abc import Callable

ALGORITHMS = ("HS256", "RS256", "ES256", "EdDSA")
AUDIENCE = "grelmicro-api"
ISSUER = "https://auth.grel.info/"
SECRET = b"grelmicro-benchmark-secret-0123456789abcdef"
HOUR = 3600
BATCH_FLOOR = 0.01
ROUNDS = 7
WARMUP = 200


def _b64u(raw: bytes) -> str:
    """Return `raw` as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _keys() -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return one signing key and one verification key per algorithm."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ed_key = ed25519.Ed25519PrivateKey.generate()

    def public(key: Any) -> bytes:  # noqa: ANN401
        return key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    signing = {
        "HS256": SECRET,
        "RS256": rsa_key,
        "ES256": ec_key,
        "EdDSA": ed_key,
    }
    verifying = {
        "HS256": SECRET,
        "RS256": public(rsa_key),
        "ES256": public(ec_key),
        "EdDSA": public(ed_key),
    }
    return signing, verifying


SIGNING, VERIFYING = _keys()


def _sign(algorithm: str, message: bytes) -> bytes:
    """Return the JWS signature of `message`."""
    key = SIGNING[algorithm]
    if algorithm == "HS256":
        return hmac.new(SECRET, message, hashlib.sha256).digest()
    if algorithm == "RS256":
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    if algorithm == "ES256":
        r, s = decode_dss_signature(
            key.sign(message, ec.ECDSA(hashes.SHA256()))
        )
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return key.sign(message)


def _token(algorithm: str, subject: str = "user-1") -> str:
    """Return a signed token for `algorithm`."""
    now = int(time.time())
    header = _b64u(
        json.dumps(
            {"alg": algorithm, "typ": "JWT"}, separators=(",", ":")
        ).encode()
    )
    payload = _b64u(
        json.dumps(
            {
                "iss": ISSUER,
                "sub": subject,
                "aud": AUDIENCE,
                "exp": now + HOUR,
                "iat": now,
                "jti": "token-1",
                "scope": "orders:read orders:write profile:read",
                "roles": ["operator", "auditor"],
            },
            separators=(",", ":"),
        ).encode()
    )
    message = f"{header}.{payload}"
    return f"{message}.{_b64u(_sign(algorithm, message.encode()))}"


def _verifier(algorithm: str, **options: Any) -> JWTVerifier:  # noqa: ANN401
    """Return a verifier for `algorithm` with the benchmark's policy."""
    return JWTVerifier(
        JWTConfig(
            keys=[JWTKey(algorithm=algorithm, key=VERIFYING[algorithm])],
            audience=[AUDIENCE],
            issuer=[ISSUER],
            **options,
        )
    )


def _measure(fn: Callable[[], object]) -> float:
    """Return the best-of-seven ns/op for `fn`, in self-calibrating batches."""
    for _ in range(WARMUP):
        fn()
    batch = 1
    while True:
        start = time.perf_counter()
        for _ in range(batch):
            fn()
        if time.perf_counter() - start > BATCH_FLOOR:
            break
        batch *= 4
    best = None
    gc.disable()
    try:
        for _ in range(ROUNDS):
            start = time.perf_counter()
            for _ in range(batch):
                fn()
            sample = (time.perf_counter() - start) / batch * 1e9
            best = sample if best is None else min(best, sample)
    finally:
        gc.enable()
    return best or 0.0


def _bench_verify() -> None:
    """Measure a full verification per algorithm, with the cache off."""
    print("\n== verify, cache off (ns/op) ==")  # noqa: T201
    for algorithm in ALGORITHMS:
        verifier = _verifier(algorithm, cache_size=0)
        token = _token(algorithm)
        cost = _measure(lambda v=verifier, t=token: v.verify(t))
        print(f"  {algorithm:<12}{cost:>12,.0f}")  # noqa: T201


def _bench_cache() -> None:
    """Measure the repeated token that most requests present."""
    print("\n== verify, cache hit (ns/op) ==")  # noqa: T201
    print(f"  {'algorithm':<12}{'miss':>12}{'hit':>12}{'speedup':>11}")  # noqa: T201
    for algorithm in ALGORITHMS:
        token = _token(algorithm)
        cold = _verifier(algorithm, cache_size=0)
        warm = _verifier(algorithm)
        warm.verify(token)
        miss = _measure(lambda v=cold, t=token: v.verify(t))
        hit = _measure(lambda v=warm, t=token: v.verify(t))
        print(  # noqa: T201
            f"  {algorithm:<12}{miss:>12,.0f}{hit:>12,.0f}{miss / hit:>10.0f}x"
        )


def main() -> None:
    """Run every JWT benchmark."""
    print("Benchmarking grelmicro.security.jwt")  # noqa: T201
    _bench_verify()
    _bench_cache()


if __name__ == "__main__":
    main()
