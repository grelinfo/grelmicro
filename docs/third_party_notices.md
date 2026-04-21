# Third-Party Notices

grelmicro includes source material adapted from third-party projects.
This file lists their copyright and license information.

All third-party material listed here is MIT-licensed, which is
compatible with grelmicro's own MIT license. A single copy of the MIT
License text appears at the end of this file and applies to every
copyright holder listed above it.

## Upstash Ratelimit

The Redis Lua scripts in `grelmicro/resilience/redis.py` are adapted
from [Upstash Ratelimit](https://github.com/upstash/ratelimit):

- `_RedisTokenBucket._LUA_ACQUIRE` and `_LUA_PEEK`: hash-based
  storage pattern (`HMGET` / `HSET` with `tokens` and `last`
  fields) and overall structural shape.
- `_RedisGCRA._LUA_ACQUIRE` and `_LUA_PEEK`: GCRA formulation
  (emission interval, TAT, burst offset) and the Jan 1 2017
  timestamp offset used to preserve double-precision accuracy in
  Redis server-time arithmetic.

Adaptations for grelmicro:

- Server-side `redis.call("TIME")` for cross-process clock
  consistency.
- Continuous token-bucket refill by `refill_rate` (tokens per
  second) rather than Upstash's discrete-interval refills.
- Result payload shaped to match grelmicro's `RateLimitResult`
  `(allowed, remaining, retry_after, reset_after)` so that both
  algorithms expose a uniform Python surface.

Copyright (c) 2023 Upstash, Inc.
Licensed under the [MIT License](#mit-license).

## APScheduler

The function-reference validation logic in
`grelmicro/task/_utils.py` (`validate_and_generate_reference`) is
adapted from
[APScheduler](https://github.com/agronholm/apscheduler): specifically
its `_marshalling` module. The checks for `partial`, bound methods,
missing `__module__` / `__qualname__`, lambdas, and nested functions
(via `<lambda>` and `<locals>` qualname markers) before building a
`module:qualname` reference follow APScheduler's approach.

Copyright (c) Alex Grönholm
Licensed under the [MIT License](#mit-license).

## Design Inspirations (No Code Copied)

The following are acknowledged as design inspirations only. No source
code is adapted from them, so no license notice is required, but they
are listed here for transparency:

- **Rust `tracing` crate**: the ergonomics of the
  `@instrument` decorator in `grelmicro/tracing/_instrument.py`
  are inspired by Rust's `#[instrument]` attribute. The
  implementation is native Python over OpenTelemetry.

---

## MIT License

The following license text applies to every copyright holder listed
above.

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
