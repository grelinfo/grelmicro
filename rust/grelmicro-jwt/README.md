# grelmicro-jwt-core

The compiled verification core behind `grelmicro.security.jwt`.

It owns the hot path of checking an inbound JWT: selecting the key the token's
`kid` names, verifying the signature, checking the registered claims, and
decoding the claim set, all in one call across the boundary. Configuration,
the claim wrapper, the cache and the error taxonomy stay in Python.

Install it through grelmicro rather than on its own:

```bash
pip install "grelmicro[jwt]"
```

## Design notes

- `aws-lc-rs` is the only crypto provider enabled, so the slower pure-Rust
  backend cannot be selected by accident. AWS-LC is a fork of BoringSSL with a
  FIPS 140-3 validated module.
- Verification releases the GIL, so a thread pool verifies in parallel.
- The module declares `gil_used = false`, so importing it does not turn the
  GIL back on under a free-threaded interpreter.
- `Verifier` is a frozen class holding only what construction put in it, which
  is what makes it safe to share across threads without a lock.

The measurements behind each of these choices are written up in
`docs/architecture/jwt.md` in the grelmicro repository.
