# grelmicro-core

The compiled hot paths behind grelmicro.

Only work that earns the crossing lives here. A call into Rust costs roughly
20 to 30 nanoseconds, so anything doing less than about a microsecond of real
work belongs in Python. The token cache and the ban table were measured both
ways and stayed there.

What is here today is JWT verification, where a signature check is some twelve
microseconds: selecting the key the token's `kid` names, verifying the
signature, checking the registered claims, and decoding the claim set, all in
one call. Configuration, the claim wrapper, the cache and the error taxonomy
stay in Python.

Install it through grelmicro rather than on its own:

```bash
pip install "grelmicro[jwt]"
```

## Design notes

- `aws-lc-rs` is the only crypto provider enabled, so the slower pure-Rust
  backend cannot be selected by accident. AWS-LC is a fork of BoringSSL. Its
  `fips` feature is not enabled, so this build claims no FIPS validation.
- Verification releases the GIL, so a thread pool verifies in parallel.
- The module declares `gil_used = false`, so importing it does not turn the
  GIL back on under a free-threaded interpreter.
- `Verifier` is a frozen class holding only what construction put in it, which
  is what makes it safe to share across threads without a lock.

The measurements behind each of these choices are written up in
`docs/architecture/jwt.md` in the grelmicro repository.
