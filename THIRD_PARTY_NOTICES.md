# Third-Party Notices

grelmicro includes source material adapted from third-party projects.
This file lists their copyright and license information.

Source-code adaptations are MIT-licensed and compatible with grelmicro's
own MIT license. Brand assets (the logo SVGs) are built from glyph
outlines of the Funnel Sans typeface under the SIL Open Font License
1.1. Full license texts appear at the end of this file.

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

## Funnel Sans (Brand Assets)

The grelmicro logo (wordmark and favicon SVGs under
`docs/img/logo/`) is built from outlined glyphs of the **Funnel
Sans** typeface (part of the Funnel type family).

- Text is converted to static SVG `<path>` geometry before
  distribution (no font files are shipped with the project).
- The red square replacing the `i`-dot is sized from the font's
  own dot contour so the logo matches the typeface's natural
  proportions.
- Funnel Sans is distributed under the SIL Open Font License 1.1.
  Embedding glyph outlines in documents is explicitly permitted by
  the OFL and does not impose any licensing restrictions on the
  document.

Source: [Dicotype/Funnel on GitHub](https://github.com/Dicotype/Funnel)
Licensed under the [SIL Open Font License 1.1](#sil-open-font-license-11).

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

---

## SIL Open Font License 1.1

Copyright (c) The Funnel Project Authors
(https://github.com/Dicotype/Funnel).

This Font Software is licensed under the SIL Open Font License,
Version 1.1. This license is copied below, and is also available
with a FAQ at: https://openfontlicense.org

-----------------------------------------------------------

SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007

PREAMBLE

The goals of the Open Font License (OFL) are to stimulate
worldwide development of collaborative font projects, to support
the font creation efforts of academic and linguistic communities,
and to provide a free and open framework in which fonts may be
shared and improved in partnership with others.

The OFL allows the licensed fonts to be used, studied, modified
and redistributed freely as long as they are not sold by
themselves. The fonts, including any derivative works, can be
bundled, embedded, redistributed and/or sold with any software
provided that any reserved names are not used by derivative
works. The fonts and derivatives, however, cannot be released
under any other type of license. The requirement for fonts to
remain under this license does not apply to any document created
using the fonts or their derivatives.

DEFINITIONS

"Font Software" refers to the set of files released by the
Copyright Holder(s) under this license and clearly marked as
such. This may include source files, build scripts and
documentation.

"Reserved Font Name" refers to any names specified as such after
the copyright statement(s).

"Original Version" refers to the collection of Font Software
components as distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to,
deleting, or substituting -- in part or in whole -- any of the
components of the Original Version, by changing formats or by
porting the Font Software to a new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS

Permission is hereby granted, free of charge, to any person
obtaining a copy of the Font Software, to use, study, copy,
merge, embed, modify, redistribute, and sell modified and
unmodified copies of the Font Software, subject to the following
conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be
bundled, redistributed and/or sold with any software, provided
that each copy contains the above copyright notice and this
license. These can be included either as stand-alone text files,
human-readable headers or in the appropriate machine-readable
metadata fields within text or binary files as long as those
fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved
Font Name(s) unless explicit written permission is granted by the
corresponding Copyright Holder. This restriction only applies to
the primary font name as presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the
Font Software shall not be used to promote, endorse or advertise
any Modified Version, except to acknowledge the contribution(s)
of the Copyright Holder(s) and the Author(s) or with their
explicit written permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION

This license becomes null and void if any of the above conditions
are not met.

DISCLAIMER

THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE
AND NONINFRINGEMENT OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER
RIGHT. IN NO EVENT SHALL THE COPYRIGHT HOLDER BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING ANY GENERAL, SPECIAL,
INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES, WHETHER IN AN
ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF THE
USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER DEALINGS
IN THE FONT SOFTWARE.
