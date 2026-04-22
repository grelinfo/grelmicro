# grelmicro brand assets

Every SVG contains outlined glyph paths of **Funnel Sans 700** (OFL),
so there is no web-font or font-file dependency at runtime. Every
position is measured from the font's actual glyph data (ascender top,
dot contour, g-stem midpoint), not tuned by eye.

Regenerate with:

```bash
uv run docs/img/logo/build_logo.py
```

## Wordmark

Transparent background: the consumer provides the surface. GitHub
serves `README.md` on both light and dark, so the same mark needs two
colour variants.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="wordmark-dark.svg">
  <img alt="grelmicro" src="wordmark.svg" width="520">
</picture>

| Light | Dark |
|---|---|
| <img src="wordmark.svg" width="320" alt="wordmark light"> | <img src="wordmark-dark.svg" width="320" alt="wordmark dark" style="background:#0F0F10;padding:12px;border-radius:6px"> |

## Favicon

Rounded square plate, used as favicon, GitHub avatar, and social avatar.

| Light | Dark |
|---|---|
| <img src="favicon.svg" width="96" alt="favicon light"> | <img src="favicon-dark.svg" width="96" alt="favicon dark"> |

PNG sizes shipped for non-SVG contexts:

`favicon-16.png` · `favicon-32.png` · `favicon-48.png` ·
`favicon-192.png` · `favicon-512.png` · `apple-touch-icon.png` (180×180)

## Social preview

Open Graph / Twitter / Slack / Discord card (1200×630). Shown when the
URL is shared on social platforms.

<img src="social-preview.svg" alt="grelmicro social preview" width="640">

## HTML wiring

```html
<link rel="icon" type="image/svg+xml" href="/img/logo/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/img/logo/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/img/logo/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/img/logo/apple-touch-icon.png">
<meta property="og:image"
      content="https://grelinfo.github.io/grelmicro/img/logo/social-preview.png">
```

## Brand

| Property | Value |
|---|---|
| Hero red | `#E30613` |
| Ink | `#0F0F10` |
| Paper | `#FAFAF7` |
| Typeface | Funnel Sans 700 (outlined inside every SVG) |

The red square above **g** and **i** is the signature: aligned to the
ascender top, sized to the font's natural dot. Do not move or
recolour it.

## License

Source code MIT. Glyph outlines embedded under SIL OFL 1.1 (see the
repo-root [`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md)).
