# JWT verification

Verifying a bearer token is on the path of every authenticated request, so the
cost of getting it wrong is paid per request forever. Every choice below was
measured before it was made.

Numbers come from an Apple M4, macOS arm64, CPython 3.12.12, RS256 with a
2048-bit key, best of seven runs with the garbage collector off. Treat the
ratios as the result and the absolute figures as one machine's.

## Why a compiled core

Pure-Python JWT libraries spend most of a verification in Python, not in
cryptography. Measured against the same token and the same claim policy, with
every key pre-parsed so no candidate re-reads a PEM per call:

| Library | ns per verification | vs PyJWT |
| --- | ---: | ---: |
| grelmicro core (Rust) | 11,600 | 4.0x |
| pyjwt-rs 1.2.2 | 13,600 | 3.4x |
| Authlib 1.8.0 | 27,100 | 1.7x |
| joserfc 1.7.5 | 29,500 | 1.6x |
| PyJWT 2.14.0 | 45,800 | 1.0x |
| jwcrypto 1.6.0 | 53,700 | 0.9x |
| python-jose 3.5.0 | 64,400 | 0.7x |

One detail costs more than the library choice: passing PyJWT a raw PEM instead
of a loaded key object costs 1.8x on RS256, because the key is parsed again on
every call. grelmicro parses every key once, when the verifier is built.

## Crypto provider

`jsonwebtoken` ships no crypto provider by default. It has two, and the choice
matters more than the choice of language:

| Provider | HS256 | RS256 | ES256 | EdDSA |
| --- | ---: | ---: | ---: | ---: |
| aws-lc-rs | 2,477 | 11,296 | 26,667 | 19,235 |
| RustCrypto | 3,312 | 87,822 | 129,100 | 20,730 |

RustCrypto is 7.8x slower on RS256, which lands it at 0.52x PyJWT. A Rust
rewrite that is slower than the Python it replaced is an easy mistake to make
silently, so the crate enables `aws_lc_rs` and nothing else. The backend cannot
be selected by accident.

Speed is not the only reason it wins, and it is not the first one. The first
is that it verifies what providers actually sign with. The core accepts twelve
algorithms, covering the RSA, ECDSA, RSA-PSS and Ed25519 families, so a token
from any of the providers the suite tests against verifies without the caller
choosing anything.

The second is that the implementation is one worth trusting.
[AWS-LC](https://github.com/aws/aws-lc-rs) is a fork of BoringSSL maintained
by a team that does this full time, it is what
[rustls](https://docs.rs/rustls/latest/rustls/) uses by default, and it
carries a FIPS 140-3 validated module. That last one is cited as evidence of
how the code is reviewed and tested, not as a goal: the `fips` feature is not
enabled here and this build claims no validation. Enabling it would cost
EdDSA, which is a reason not to rather than a reason to.

## One implementation, no fallback

An earlier plan kept PyJWT as a fallback for platforms with no compiled wheel.
It was dropped, for two reasons.

grelmicro already requires a compiled wheel. `pydantic` is a core dependency and
`pydantic-core` is a Rust extension shipping 136 wheels across 16 platform
families. There is no platform where grelmicro installs today but a compiled
extension could not.

The stronger reason is that two implementations of a credential check is two
behaviours to keep identical. Every algorithm, every claim edge case, every
malformed token would have to be proven to be refused the same way by both, on
every release. One backend is one behaviour to prove.

The cost is a wheel-building job that has to track pydantic-core's matrix. Where
it does not, those users get grelmicro but not `grelmicro[jwt]`.

## Reaching asyncio

A verification is CPU work on the event loop thread. Seven ways to move it off
were measured end to end through FastAPI under uvicorn, at 128 keep-alive
connections, as a share of the same endpoint with no authentication:

| Strategy | share of no-auth throughput | p99 |
| --- | ---: | ---: |
| inline | 72% | 12.9 ms |
| Rust verifies, Python parses the JSON | 71% | 14.8 ms |
| Rust worker threads on a channel | 52% | 26.6 ms |
| Rust worker threads, batched | 44% | 29.0 ms |
| `asyncio.to_thread` | 44% | 17.5 ms |
| `loop.run_in_executor` | 42% | 16.7 ms |
| `anyio.to_thread.run_sync` | 42% | 25.7 ms |

Inline won every run, on throughput and on p99. At around 12 microseconds a
verification cannot repay a 40 to 60 microsecond event loop wakeup, and the
bottleneck is the Python in the ASGI stack, which offloading does not touch.

The argument for offloading was fairness: verification on the loop thread
should delay other tasks. That was tested directly, by saturating the JWT
endpoint while probing an unrelated cheap endpoint:

| Saturating strategy | co-tenant p99 (3 runs) |
| --- | ---: |
| no load, for reference | 12.2 / 10.4 ms |
| inline | 14.2 / 10.7 / 9.7 ms |
| `anyio.to_thread.run_sync` | 23.2 / 18.7 / 15.6 ms |
| Rust worker threads on a channel | 24.4 / 24.1 / 21.0 ms |

Inline costs co-tenant traffic nothing. The worker pool makes it twice as bad,
because it serves fewer requests per second and the backlog drains slower. The
fairness argument for offloading did not survive being measured, so `verify` is
a plain synchronous call.

The core still releases the GIL during verification, which is what lets a thread
pool scale if an application wants one: detached, throughput scales 3.1x from
one thread to four, where holding the GIL flatlines at 1.03x.

## Where the cache lives

A client resends one token until it expires, so most verifications are repeats.
The cache is the largest single win available, worth more than the choice of
library: a hit is 226 ns against about 12,000 ns for a full verification.

That 226 ns is measured with the token decoded fresh from bytes on every call,
which is what a request does. Measuring with one reused `str` gives 68 ns,
because CPython caches a string's hash on the object after the first use. The
reused figure is not what a service pays.

Putting it in Rust was measured and lost:

| Cache | single hit | 500-token working set | hits/s, 1 thread | 8 threads |
| --- | ---: | ---: | ---: | ---: |
| Python dict | 78 ns | 99 ns | 15.1M | 14.4M |
| Rust, behind a mutex | 134 ns | 159 ns | 8.0M | 8.1M |

A hit in Rust still pays a call across the boundary, a lock and a reference
count, where a dict hit is one hash lookup that the GIL already makes atomic.
The cache is Python.

## Where the digest is computed

The cache can key on the encoded token or on a digest of it. Keying on a digest
means no live bearer token sits in memory for the lifetime of an entry. The
question is where the hashing happens. Net of the 40 ns that decoding the
header costs either way:

| Placement | ns per hit |
| --- | ---: |
| No digest, Python dict on the raw token | 92 |
| No digest, Rust map on the raw token | 103 |
| Digest in Rust, Python dict on the digest | 170 |
| Digest in Rust, Rust map on the digest | 162 |
| Digest in Python, Python dict on the digest | 314 |

Hashing in Python is the worst option by a wide margin. `hashlib.sha256` costs
272 ns on a 480-byte token, of which only 106 ns is hashing: the rest is call
overhead. The same digest through `aws-lc-rs`, which the verifier already
links, costs 164 ns including the call into Rust. Hashing in Rust also leaves
the map hashing 32 bytes rather than 480.

Computing the digest in Rust and keeping the cache in Python costs 8 ns more
than moving the whole cache into Rust, which is inside the noise. Keeping the
cache in Python keeps the TTL, the eviction policy and the settings in one
place, so that is where it stays.

Returning the digest as raw bytes matters more than it looks. An earlier
version returned lowercase hex and cost 767 ns per hit rather than 94 ns,
because formatting 32 bytes one at a time costs several times what hashing
them costs.

## Is SHA-512 a better default

No. It is slower here, 409 ns against 272 ns through `hashlib`, and the gap is
wider on x86, where SHA-NI accelerates SHA-256 and generally not SHA-512. A
cache key needs collision resistance and nothing else, and SHA-256 gives 128
bits of it against a map holding at most a few thousand entries. SHA-512 buys
no property this cache can use.

Python's builtin `hash()` is not an option at any speed. It is 64-bit and not
collision resistant, so two colliding tokens would share one entry and one
caller would be handed another caller's claims.

## Which cache

Five replacement policies were measured on the workload that matters, which is
tokens rotating as they expire. Capacity 1024, 800 clients, 200 second token
lifetime, skewed client popularity:

| Policy | hit cost | hit rate | entries held | entries dead | TTL honoured |
| --- | ---: | ---: | ---: | ---: | --- |
| FIFO, draining expired first | 38 ns | 92.7% | 706 | 132 | yes |
| FIFO | 38 ns | 92.7% | 1,024 | 450 | yes |
| LRU on an `OrderedDict` | 49 ns | 92.7% | 1,024 | 450 | yes |
| LRU by reinserting into a dict | 56 ns | 92.7% | 1,024 | 450 | yes |
| Generational, two rotating dicts | 38 ns | 91.3% | 641 | 97 | no |
| Evict newest | 37 ns | 13.8% | 1,024 | 1,023 | yes |

Evicting the newest entry looks excellent until tokens rotate. It then fills
with entries nothing will ask for again and never evicts them, collapsing to a
13.8% hit rate with 1,023 of 1,024 slots dead. It is the reason this table is
measured on rotation rather than on a fixed population, where it scored 83.1%
and tied the best.

LRU buys nothing here. Under rotation it ties FIFO, because the entry a token
cache wants to drop is the oldest one rather than the least recently used, and
it costs 29% more per hit to maintain the ordering.

The selected policy is FIFO that first drains entries past their deadline.
Deadlines are written in insertion order, so the oldest entries are the ones
most likely expired. It matches the best hit rate while holding 31% fewer
entries, and the draining happens when a token is stored, never on a hit.

## The default keys on a digest

Keying on a digest costs 94 ns on a hit, 0.78% of a verification, and a hit is
still 38 times cheaper than verifying. For that price a process holds no live
bearer token beyond the request that presented it, which matters wherever a
heap dump, a core dump or swap is in the threat model. It is the default.
`cache_key="token"` keeps the encoded token as the key and takes the 94 ns
back.

## The cache is shared, so it never walks itself

A verifier is shared across a thread pool, so two threads reach the cache at
once. The first version used a plain dict and iterated it to evict. Under
twelve threads with the switch interval turned down it raised eleven times in
six seconds, `KeyError` from deleting a key another thread had already taken,
and `RuntimeError` from a dict changing size while it was being read.

Seven ways of fixing it were measured. Anything that raises is out whatever it
costs, so correctness was measured first:

| Variant | raised | hit ns | hit rate | 8-thread hits/s |
| --- | ---: | ---: | ---: | ---: |
| Plain dict, iterating to evict | 11 | 39 | 92.7% | 18.8M |
| Plain dict, atomic operations only | 11 | 41 | 92.7% | 17.0M |
| Lock on writes, lock-free reads | 1 | 38 | 92.7% | 18.3M |
| Dict plus a deque for order | 0 | 41 | 92.7% | 18.6M |
| Two generations, rebinding to evict | 0 | 40 | 91.3% | 5.2M |
| One lock on reads and writes | 0 | 99 | 92.7% | 6.0M |
| Sixteen sharded locks | 0 | 165 | 92.7% | 2.3M |

Using only operations the interpreter applies whole is not enough on its own.
`next(iter(cache), None)` still reads the dict, and a resize underneath it
raises. Locking writes alone is not enough either, because an unlocked reader
deleting an expired entry is a write.

The selected shape keeps the eviction order in a `deque` beside the cache, so
nothing ever walks the dict. A hit is one `dict.get` and nothing else: no
lock, no bookkeeping, no writes. It costs the same as the version that raced,
holds the best hit rate, and is the only correct variant that keeps the
throughput.

A lock is not free here. One lock on reads and writes costs 2.4 times more per
hit and a third of the throughput, because the hit path is short enough that
the lock dominates it.

## Rust locks, and other people's caches

Moving the cache back into Rust was measured again with the digest key, since
the map then hashes 32 bytes rather than 480. A read-write lock was measured
too, because a hit is a read:

| Cache | ns per hit, net |
| --- | ---: |
| Rust digest, Python dict | 180 |
| Rust digest, Rust map behind a mutex | 158 |
| Rust digest, Rust map behind a read-write lock | 163 |

The read-write lock loses to the plain mutex. An uncontended read lock is a
more expensive atomic operation than taking a mutex, and the GIL means there
are no concurrent readers to repay it. The 22 ns the Rust map saves is not
worth moving the TTL, the eviction policy and the settings across the
boundary.

`cachebox` was measured for the same reason, because it is written in Rust and
uses the same SwissTable. Like for like, on the same key with the same work:

| Operation | Python dict | cachebox |
| --- | ---: | ---: |
| Lookup, key already in hand | 23 ns | 40 ns |
| Lookup, string decoded per call | 140 ns | 156 ns |
| Digest and lookup | 223 ns | 238 ns |
| Hits per second on eight threads | 41.2M | 24.5M |

Its published comparison is against `cachetools`, which is pure Python and
keeps a linked list, and against that it is much faster. It is not faster than
a dict, because it has to cross into Rust and call back into Python to hash
the key and to move reference counts, where `dict.get` is one operation inside
the interpreter. The cache here never pays that, because the hit path does no
bookkeeping at all.

## Rejections are not cached

Refusing a token costs about as much as accepting one, because the signature
has to be checked before any claim can be trusted:

| Outcome | ns |
| --- | ---: |
| Forged signature | 11,171 |
| Wrong key | 11,109 |
| Expired | 11,738 |
| Wrong audience | 11,837 |
| Malformed | 571 |
| Unknown key | 1,021 |

So a caller flooding forged signatures costs a core about 89,000 rejections a
second, and caching them looks like the answer. It is not.

A flood uses a fresh token each time, so a cache of rejections never hits.
Worse, entries for tokens nobody will present again would evict the verified
tokens real callers depend on, turning a cost in processor time into a cost in
hit rate for everyone else. A separate cache avoids that, but then it is a
cache that only helps when the same bad token arrives repeatedly, which is a
misconfigured client rather than an attack, and that client is already answered
in microseconds.

Some reasons could never be cached anyway. `not-yet-valid` becomes valid when
the clock reaches `nbf`, and `unknown-key` becomes valid when the provider's
next key set arrives, so remembering either would refuse traffic that should
pass.

The answer to a flood is to rate limit it, which `RateLimitedRequests` already
does, and which works whether the tokens repeat or not.

## Shipping the wheel

The core is its own distribution, `grelmicro-core`, released on its own tag.
grelmicro stays a pure Python wheel built by hatchling, and the extra pulls the
compiled one in.

It is named for what it is rather than for JWT, because the crossing it pays
for is not unique to tokens. Six places already hash with `hashlib`, for cache
keys, ETags, idempotency fingerprints and shield keys, and `sha256_digest` in
this crate is 164 ns against `hashlib`'s 272 ns on the same input, most of that
difference being call overhead rather than hashing. If any of those ever moves,
it belongs in this wheel rather than in a second one that would duplicate
BoringSSL on disk. A pattern needing no crypto is the case for a separate
crate, because the binary here is 2.38 MB and almost all of it is AWS-LC.

Its wheel matrix has to track pydantic-core, which grelmicro already requires
through pydantic. Anywhere pydantic-core has a wheel and this does not,
`grelmicro[jwt]` falls back to the sdist and needs a Rust toolchain, so a gap
in the matrix is a gap in what the extra installs on. That is the cost of
dropping the pure-Python fallback, and it is paid once, in CI.

The release workflow builds no cache. A build cache is writable by any job
that can run on the repository, and everything the workflow produces is signed
and attested, so a poisoned cache would reach users as a trusted artifact. A
release is rare and a cold build is cheap against that.

It also runs the security suite against the wheel it is about to publish,
rather than against a core rebuilt from the working tree. A wheel that imports
is not a wheel that verifies.

## Free-threaded Python

The design has to hold when the GIL is gone, because the argument for the
lock-free cache was never that the GIL protects it. It was that every
operation on the cache is one the interpreter applies whole, and that holds
under free threading too.

Two things had to be true. The first is that the extension must not turn the
GIL back on. CPython re-enables it when it imports a module that does not
declare otherwise, which would quietly cost every other extension in the
process its parallelism, so the module declares `gil_used = false`. The second
is that the Rust side must be safe without it: `Verifier` is a frozen class
holding only what construction put in it, and `frozen` makes the compiler
enforce that nothing mutates it.

Measured on CPython 3.14.7 free-threaded, twelve threads for eight seconds
against a cache far too small to hold the tokens in play: 2,260,736
verifications, no exception escaped, no request was served another request's
claims, and the cache and its queue both stayed at their bound.

Verification itself scales with cores once nothing serialises it:

| Threads | 1 | 2 | 4 | 8 | scale |
| --- | ---: | ---: | ---: | ---: | ---: |
| Verifications per second | 79,629 | 151,012 | 272,337 | 532,814 | 6.69x |

That is the payoff for releasing the GIL inside the core. On the build with a
GIL the same work flattens out around four threads.

The cache hit does not scale the same way. It reaches 6.8M hits per second on
four threads and falls back to 1.9M on eight, because at roughly 300 ns the
call is dominated by the allocations it makes, a string decoded from the
header, a digest, and a tuple, rather than by the lookup. It still answers
about four times faster than verifying does at the same thread count, so the
cache remains worth having. Sizing the thread pool to the cores that verify,
rather than higher, is the useful conclusion.

## The TTL is not the token's expiry

An entry expires at whichever comes first, the token's own `exp` or
`cache_ttl`. Bounding on `exp` alone would let a token with a 24 hour lifetime
sit in memory for 24 hours, so a credential withdrawn upstream would keep being
accepted from cache long after it stopped being accepted anywhere else.
`cache_ttl` caps that window. A token carrying no `exp` is never cached at all,
because nothing would bound it.

## What stays in Python

The Python layer costs 684 ns on top of the core, 5.9% of a full verification,
of which 500 ns is building the claims object. Moving that to Rust would save
nothing on a cache hit, because the hit returns the object already built. At a
92.7% hit rate the saving is about 37 ns per request out of roughly 900 ns
average. It is not worth a second claims representation, so configuration,
claim wrapping, the cache and the error taxonomy stay in Python and the core
does key selection, signature verification, registered claim checks and
decoding.

Letting the core decode the JSON is worth keeping: handing back a JSON string
for Python to parse costs 1,108 ns more per token, 8.0% on top of a
verification.
