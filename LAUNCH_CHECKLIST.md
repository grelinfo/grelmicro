# grelmicro 1.0 launch checklist

Maintainer planning doc (not published to the docs site). Tracks the launch tasks that need a human: posting, badges, and the demo recording. Issues [#172](https://github.com/grelinfo/grelmicro/issues/172), [#177](https://github.com/grelinfo/grelmicro/issues/177), [#178](https://github.com/grelinfo/grelmicro/issues/178).

## Pre-launch (do before posting)

- [ ] Tag and publish the 1.0 release on PyPI; confirm `pip install grelmicro` resolves it.
- [ ] `docs/benchmarks.md` numbers re-run on a clean machine and dated.
- [ ] The [FastAPI demo](examples/fastapi-demo) starts from a fresh clone in three commands.
- [ ] Record the demo asset (see below) and embed it in the README.
- [ ] Enable the badges (see below).
- [ ] README "Why grelmicro" leads with one sentence a stranger understands.
- [ ] CHANGELOG `Unreleased` section moved under the `1.0` heading.

## Launch channels (#172)

Post in this order, spacing them out over a day so you can answer comments:

| Channel | Format | Notes |
|---|---|---|
| Hacker News (Show HN) | "Show HN: grelmicro — async microservice toolkit for FastAPI" | Post in the morning ET. Link the repo, not a blog. Be present for the first 2 hours. |
| r/Python | Text post | Lead with the problem (distributed primitives for FastAPI), then the demo. Flair: "Show and Tell". |
| r/FastAPI | Text post | Focus on the FastAPI integration and the demo. |
| dev.to | Article | "Distributed primitives for FastAPI without the boilerplate". Embed the demo asset. |
| X / Bluesky | Thread | One Pattern per post with a code snippet; end with the repo link. |

Draft copy lives in this file's history; refine per channel. Do not cross-post the same text verbatim.

## Badges (#177)

Add to the top of `README.md` once enabled:

```markdown
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/grelinfo/grelmicro/badge)](https://securityscorecards.dev/viewer/?uri=github.com/grelinfo/grelmicro)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://slsa.dev)
```

To make them real:

1. **OpenSSF Scorecard**: add the official `ossf/scorecard-action` workflow (`.github/workflows/scorecard.yml`) running on a weekly `schedule` and on push to `main`, with `publish_results: true`. It needs `id-token: write` and `security-events: write` permissions. The badge endpoint goes live after the first successful run. Keep it off pull-request triggers so it never gates PR CI.
2. **SLSA provenance**: have the release workflow generate provenance with `slsa-framework/slsa-github-generator` and attach it to the GitHub Release + PyPI (Trusted Publishing with attestations). The SLSA badge is static once provenance ships.

Both need the actions pinned by commit SHA (the repo's `zizmor` workflow-lint enforces this).

## Demo asset (#178)

Record a ~30s [asciinema](https://asciinema.org) cast of the demo, then embed it:

```bash
cd examples/fastapi-demo
asciinema rec demo.cast
# in the recording: docker compose up --wait, curl a couple endpoints, ctrl-d
```

Upload the cast (asciinema.org or an SVG via `svg-term`) and add it to the top of `examples/fastapi-demo/README.md` and the main README "Run the demo" section. A GIF works too; keep it under ~2 MB so GitHub renders it inline.
