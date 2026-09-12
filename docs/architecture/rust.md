# Rust

Some of grelmicro is compiled. Not much, and the line between what is and what
is not was drawn by measuring rather than by taste.

## The line

**Rust is for work grelmicro does itself. It is not for talking to anything.**

A client that speaks to Redis, Postgres or an HTTP endpoint stays Python,
whatever it costs, because what it costs is not ours to fix. Computation
grelmicro performs on the request path is a candidate, and only if it is big
enough to pay for the crossing.

## What a crossing costs

A call into Rust costs **55 ns** measured, argument and return value included.
Everything follows from that one number:

| Work behind the call | Verdict |
| --- | --- |
| Under about 200 ns | Rust cannot win, the crossing is the cost |
| Around 1 microsecond | Worth measuring, usually no |
| Over 10 microseconds | Worth doing |

This is not theoretical. The verified-token cache was written both ways and
Python won: a `dict` hit is 78 ns against 134 ns for a Rust map behind a
mutex, and 15.1M against 8.0M hits per second across eight threads. A hit in
Rust still pays the crossing, a lock and a reference count, where a `dict` hit
is one hash lookup the interpreter already makes atomic.

`cachebox` is the same story from another direction. It is written in Rust and
uses the same SwissTable, and it loses to a plain `dict` on the same key: 40 ns
against 23 ns. Its published comparison is against `cachetools`, which is a
fair claim and a different baseline.

## Why clients stay Python

The obvious question is whether a Rust Redis client would make the distributed
rate limiter faster. Measured, on loopback:

| | ns | Share |
| --- | ---: | ---: |
| Raw RESP round trip over asyncio | 201,820 | 83% |
| The client library, over that floor | 13,685 | 5.6% |
| The limiter's own Lua script | 28,337 | 12% |

The library is 5.6% of the cost. A Rust client could attack only that, and it
would still be driven from asyncio, so the round trip does not move. Replacing
it would buy noise.

Speed is also the smallest of the reasons. A Python client carries things a
replacement would have to rebuild:

- **Tracing.** `Trace` sweeps the `opentelemetry_instrumentor` entry points, so
  any installed `opentelemetry-instrumentation-*` package attaches itself.
  A client that is not Python is invisible to that, and a slow dependency
  stops appearing in traces.
- **Trust and transport.** Certificate authorities, mutual TLS, and proxy
  settings are configured once, for the client the application already has. A
  second implementation means a second trust store to audit.
- **Compatibility.** Owning a protocol client means owning it against every
  server version, forever.

The same reasoning is why `JWKSVerifier` takes a `fetch` argument rather than
embedding an HTTP client. The default is built on `httpx`, and passing your
own keeps the request inside whatever instrumentation and retry policy it
already has.

## What is compiled today

JWT verification, and nothing else. A signature check is about 12 microseconds,
which is four hundred times the crossing, and the compiled core answers in
11,600 ns where a pure-Python library takes 45,800.

Worth noting what that measurement did **not** buy. The biggest win in the JWT
work was not Rust at all, it was not doing the work twice: a verified-token
cache answers a repeat in 299 ns. Reaching for a faster implementation before
asking whether the work is needed gets the smaller half.

The crypto provider also mattered more than the language. The same crate on
its pure-Rust backend takes 87,822 ns on RS256, slower than the Python it
replaced, so the crate enables `aws_lc_rs` and nothing else.

## What is next, and what is not

One candidate has been measured and is worth doing:

| | Python | Rust | |
| --- | ---: | ---: | --- |
| `sha256` of a short key | 252 ns | 77 ns | Six call sites already hash this way |

Only 12 ns of that 252 is hashing. The rest is object machinery: `hashlib`
offers no one-shot call, so eighty bytes cost a hasher object, a digest
object, and a string. Rust returns the digest from one call.

Hashing a large body is not a candidate. At 64 KiB `hashlib` takes 19,082 ns
at 3.44 GB/s, which is the same accelerated primitive, so this is for keys and
fingerprints rather than for request bodies.

### Address parsing, which looked like a candidate and is not

`resolve_client_address` parses an address per request, and the standard
library is slow at it: 680 ns for IPv4 and 1,269 ns for IPv6. That reads like
twelve to twenty times the crossing, so it was proposed.

Measuring where the time goes killed it. The cost is constructing the Python
`ipaddress` object, not reading the string. pydantic was checked first, since
`pydantic-core` is already a Rust dependency, and it is no faster: 864 ns for
IPv4 against the standard library's 680, because it returns the same
`ipaddress.IPv6Address` and adds validation dispatch on top of building it.

A Rust parser meets the same wall unless it stops returning an `ipaddress`
object, which means `clientip.py` working in canonical strings and a prebuilt
matcher instead. That is a restructuring of security-sensitive parsing to save
about a microsecond on a request that spends forty to sixty microseconds in
Python. The standard library is correct, well tested, and stays.

Measured and rejected:

| | Measured | Why not |
| --- | ---: | --- |
| Token cache | 78 ns | Written both ways, Python won |
| Client ban check | 55 ns | Equal to the crossing |
| Rate limiter, in memory | 906 ns | Not the bottleneck it sheds |
| Rate limiter, over Redis | 243,059 ns | Network bound |
| Address parsing | 680 to 1,269 ns | The cost is the Python object, not the parse |
| Redis, Postgres, HTTP clients | | Not ours, and the library is 5.6% of the cost |
| JSON | | `orjson` exists and is already the optional path |

Anything else comes with a measurement first. Most of grelmicro does too little
work per call to pay for the boundary, which is the useful result rather than a
disappointing one.

## Packaging

Compiled code ships as `grelmicro-core`, a wheel of its own, pulled in by the
extra that needs it. `grelmicro` stays a pure Python wheel built by hatchling,
so the release, the provenance and the Python matrix are unchanged.

One wheel holds anything crypto-adjacent, because the binary is 2.38 MB and
almost all of it is AWS-LC. A second wheel would put BoringSSL on disk twice
for people who call it once. A pattern needing no crypto is the case for a
separate crate instead, and the wheel matrix it costs is CI minutes rather than
a download.

There is no pure-Python fallback. `pydantic-core` already makes grelmicro a
compiled-wheel install, so a fallback adds no platform, and two implementations
of a credential check would be two behaviours to prove identical on every
release.

## Writing it

- Declare `gil_used = false`. CPython turns the GIL back on when it imports a
  module that does not, which would quietly cost every other extension in the
  process its parallelism.
- Release the GIL for work that touches no Python object. That is what lets a
  thread pool scale: detached, verification goes 6.69x from one thread to eight
  on a free-threaded interpreter.
- Keep types `frozen`, so the compiler enforces that a shared object is not
  mutated rather than a reviewer having to.
- `unsafe_code = "forbid"`, and clippy at pedantic plus nursery with warnings
  denied, which is the Rust side of running ruff with every rule selected.
