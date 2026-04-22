# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fonttools>=4.55",
#     "resvg-py>=0.3",
# ]
# ///
"""Build the grelmicro logo asset set from Funnel Sans glyph outlines.

Outputs ``docs/img/logo/*`` (SVG + PNG). SVGs embed outlined glyph paths
so they render identically without any web-font or font-file dependency.
Every dimension comes from the font file (ascender top, dot contour,
g-stem midpoint). No hand-tuned values.

Usage
-----
    uv run docs/img/logo/build_logo.py

uv reads the PEP 723 header above and provisions an ephemeral
environment with the required deps. No manual ``pip install`` needed,
and nothing installs into the project venv.

PNG rasterisation uses ``resvg-py`` (Rust ``resvg`` shipped as a wheel,
no system C libs). The Funnel Sans ``.ttf`` is cached in the OS temp
directory and downloaded on first run.
"""

from __future__ import annotations

import math
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import cast

import resvg_py
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

# The script sits inside the asset directory it writes to.
OUT_DIR = Path(__file__).resolve().parent
# Funnel Sans Bold (static). Softer terminal on `g` than Funnel Display and
# anti-aliases more gracefully at small sizes. Upstream source (SIL OFL 1.1).
FONT_CACHE = Path(tempfile.gettempdir()) / "grelmicro-funnel-sans-bold.ttf"
FONT_URL = (
    "https://raw.githubusercontent.com/Dicotype/Funnel/main/"
    "fonts/Funnel_Sans/ttf/FunnelSans-Bold.ttf"
)

# Static Bold TTF — no variable axis to select.
WEIGHT = 700
RED = "#E30613"
INK = "#0F0F10"
PAPER = "#FAFAF7"


# --------------------------------------------------------------------------- #
# Font measurement
# --------------------------------------------------------------------------- #


def _ensure_font() -> Path:
    if FONT_CACHE.exists():
        return FONT_CACHE
    FONT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(FONT_URL, FONT_CACHE)
    return FONT_CACHE


def _glyph_set(font: TTFont):
    # Static Bold TTF: no variable axis, so no location override needed.
    return font.getGlyphSet()


def _cmap(font: TTFont) -> dict[int, str]:
    """Non-None codepoint→glyph-name mapping (ty-friendly narrowing)."""
    cmap = font.getBestCmap()
    if cmap is None:
        msg = "Font has no usable cmap table"
        raise RuntimeError(msg)
    return cmap


def _bbox(font: TTFont, ch: str):
    gs = _glyph_set(font)
    bp = BoundsPen(gs)
    gs[_cmap(font)[ord(ch)]].draw(bp)
    return bp.bounds


def _contour_points(font: TTFont, ch: str) -> list[tuple[float, float]]:
    gs = _glyph_set(font)
    rp = DecomposingRecordingPen(gs)
    gs[_cmap(font)[ord(ch)]].draw(rp)
    pts: list[tuple[float, float]] = []
    for _, args in rp.value:
        for pt in args:
            if isinstance(pt, tuple) and len(pt) == 2:
                pts.append(pt)
    return pts


def _dot_bbox(font: TTFont) -> tuple[float, float, float, float]:
    """Isolate the `i` glyph's dot contour (points above ı's stem top)."""
    stem_top = _bbox(font, "ı")[3]
    gs = _glyph_set(font)
    rp = DecomposingRecordingPen(gs)
    gs[_cmap(font)[ord("i")]].draw(rp)
    contours: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for cmd, args in rp.value:
        if cmd == "moveTo" and cur:
            contours.append(cur)
            cur = []
        for pt in args:
            if isinstance(pt, tuple) and len(pt) == 2:
                cur.append(pt)
    if cur:
        contours.append(cur)
    pts = [
        p for c in contours if min(q[1] for q in c) >= stem_top - 1 for p in c
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _g_stem_geom(font: TTFont) -> dict:
    """Measure the g's right stem: center, width, and bowl-junction y.

    Looks at points near the x-height-top of the glyph to find the two
    rightmost x values (stem inner-left and outer-right). Then finds the
    lowest y at the outer-right x to get the bowl junction.
    """
    pts = _contour_points(font, "g")
    max_y = max(p[1] for p in pts)
    top = [p for p in pts if p[1] > max_y - 50]
    xs = sorted({round(p[0], 2) for p in top})
    inner, outer = xs[-2], xs[-1]
    # Bowl junction: lowest y at x = outer edge
    on_outer = [p[1] for p in pts if abs(p[0] - outer) < 0.5]
    junction_y = min(on_outer) if on_outer else max_y - 200
    return {
        "cx": (inner + outer) / 2,
        "width": outer - inner,
        "junction_y": junction_y,
    }


def _g_stem_cx(font: TTFont) -> float:
    return _g_stem_geom(font)["cx"]


def _glyph_path(font: TTFont, ch: str) -> str:
    gs = _glyph_set(font)
    pen = SVGPathPen(gs)
    gs[_cmap(font)[ord(ch)]].draw(pen)
    return pen.getCommands()


def _glyph_advance(font: TTFont, ch: str) -> float:
    gs = _glyph_set(font)
    return gs[_cmap(font)[ord(ch)]].width


def measure(font: TTFont) -> dict:
    tit = _dot_bbox(font)
    g_bb = _bbox(font, "g")
    stem = _g_stem_geom(font)
    return {
        "upem": cast("int", font["head"].unitsPerEm),  # ty: ignore[unresolved-attribute]
        "ascender": _bbox(font, "l")[3],
        # g's descender depth is |yMin| of the g glyph (negative in font y-up).
        "descender": abs(g_bb[1]),
        "g_stem_cx": stem["cx"],
        "g_stem_w": stem["width"],
        "g_bowl_junction_y": stem["junction_y"],
        "square": max(tit[2] - tit[0], tit[3] - tit[1])
        * math.sqrt(math.pi)
        / 2,
    }


# --------------------------------------------------------------------------- #
# SVG builders
# --------------------------------------------------------------------------- #


def _svg_header(vw: int | float, vh: int | float, title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {vw:g} {vh:g}" width="{vw:g}" height="{vh:g}" '
        f'shape-rendering="geometricPrecision" role="img" '
        f'aria-label="{title}">\n  <title>{title}</title>'
    )


def build_wordmark(font: TTFont, m: dict, *, dark: bool) -> str:
    """Tight-padded wordmark with transparent background."""
    font_size = 110
    scale = font_size / m["upem"]
    letter_spacing_em = 0.005
    text = "grelmıcro"
    pad_x, pad_top, pad_bottom = 12, 6, 6

    xs: list[float] = []
    x = pad_x
    for ch in text:
        xs.append(x)
        x += _glyph_advance(font, ch) * scale + letter_spacing_em * font_size
    vw = int(round(xs[-1] + _glyph_advance(font, text[-1]) * scale + pad_x))
    asc, desc = m["ascender"] * scale, m["descender"] * scale
    vh = int(round(asc + desc + pad_top + pad_bottom))
    baseline = pad_top + asc

    iota = _bbox(font, "ı")
    iota_cx = (iota[0] + iota[2]) / 2 * scale
    i_cx = xs[text.index("ı")] + iota_cx
    g_cx = xs[text.index("g")] + m["g_stem_cx"] * scale
    tit_side = m["square"] * scale
    tit_y = baseline - m["ascender"] * scale
    fill_text = PAPER if dark else INK

    lines = [_svg_header(vw, vh, "grelmicro")]
    for idx, ch in enumerate(text):
        fill = fill_text if idx < 4 else RED
        lines.append(
            f'  <path d="{_glyph_path(font, ch)}" fill="{fill}" '
            f'transform="translate({xs[idx]:.3f} {baseline:.3f}) '
            f'scale({scale:.6f} -{scale:.6f})"/>'
        )
    for cx in (i_cx, g_cx):
        lines.append(
            f'  <rect x="{cx - tit_side / 2:.3f}" y="{tit_y:.3f}" '
            f'width="{tit_side:.3f}" height="{tit_side:.3f}" '
            f'fill="{RED}" shape-rendering="crispEdges"/>'
        )
    lines.append("</svg>\n")
    return "\n".join(lines)


def build_favicon(font: TTFont, m: dict, *, dark: bool) -> str:
    """Square plate + g + red dot.

    The g fills 72% of the tile so the natural dot is ~2 px at 16×16 and
    the stem stays > 1 px. Dot size and y-position come straight from the
    font (ascender top, natural dot contour). Rects carry
    ``shape-rendering="crispEdges"`` so the browser snaps their edges to the
    pixel grid at render time, giving sharp corners without shifting the
    dot off its ascender-aligned position.
    """
    view = 512
    target_h = view * 0.78  # larger g so small-size stems cover more pixels
    font_size = target_h / (m["ascender"] + m["descender"]) * m["upem"]
    scale = font_size / m["upem"]
    asc, desc = m["ascender"] * scale, m["descender"] * scale
    top_margin = (view - (asc + desc)) / 2
    baseline = top_margin + asc

    g_advance = _glyph_advance(font, "g") * scale
    g_x_natural = view / 2 - g_advance / 2

    # Nudge g horizontally so its right stem (the most prominent vertical
    # edge) lands on an integer pixel at the PNG sizes that render it.
    # Target 48 px (header icon): at that render 1 px = 512/48 view-units.
    # Shift g_x by <5 vb-units (sub-pixel at 512) to snap.
    stem_outer_fu = m["g_stem_cx"] + m["g_stem_w"] / 2
    px_48 = 512 / 48
    right_stem_vb = g_x_natural + stem_outer_fu * scale
    snapped = round(right_stem_vb / px_48) * px_48
    g_x = g_x_natural + (snapped - right_stem_vb)

    # Red + black column stacked on top of the g's right stem. The left
    # and right edges are placed at the exact font-unit positions of the
    # stem's edges (not rounded), so both the rectangles and the g's
    # outline anti-alias to the same sub-pixel — no visible step between
    # the rectangle column and the stem.
    stem_inner_fu = m["g_stem_cx"] - m["g_stem_w"] / 2
    tit_x = g_x + stem_inner_fu * scale
    tit_side = m["g_stem_w"] * scale
    tit_y = baseline - m["ascender"] * scale
    plate_rx = round(view * 0.22)

    bg, fg = (INK, PAPER) if dark else (PAPER, INK)

    lines = [_svg_header(view, view, "grelmicro favicon")]
    lines.append(
        f'  <rect width="{view}" height="{view}" rx="{plate_rx}" '
        f'fill="{bg}" shape-rendering="crispEdges"/>'
    )
    lines.append(
        f'  <path d="{_glyph_path(font, "g")}" fill="{fg}" '
        f'transform="translate({g_x:.3f} {baseline:.3f}) '
        f'scale({scale:.6f} -{scale:.6f})"/>'
    )
    lines.append(
        f'  <rect x="{tit_x:.3f}" y="{tit_y:.3f}" '
        f'width="{tit_side:.3f}" height="{tit_side:.3f}" fill="{RED}"/>'
    )
    lines.append("</svg>\n")
    return "\n".join(lines)


def build_social_preview(font: TTFont, m: dict) -> str:
    """1200×630 social-media card (Open Graph / Twitter / Slack preview).

    Tile on the left, wordmark on the right, tagline below. Minimal padding
    so the mark feels confident rather than floating in empty space.
    """
    vw, vh = 1200, 630
    # Bigger tile, centred vertically; 60 px outer margin (tight)
    tile = 360
    tile_x, tile_y = 60, (vh - tile) / 2

    # Tile glyph: scale g so it occupies ~56 % of tile height (same ratio as favicon)
    target_h = tile * 0.56
    tile_fs = target_h / (m["ascender"] + m["descender"]) * m["upem"]
    t_scale = tile_fs / m["upem"]
    tg_adv = _glyph_advance(font, "g") * t_scale
    tg_x = tile_x + tile / 2 - tg_adv / 2
    t_asc = m["ascender"] * t_scale
    t_desc = m["descender"] * t_scale
    tile_margin = (tile - (t_asc + t_desc)) / 2
    tg_baseline = tile_y + tile_margin + t_asc
    tg_cx = tg_x + m["g_stem_cx"] * t_scale
    t_tit = m["square"] * t_scale
    t_tit_y = tg_baseline - m["ascender"] * t_scale

    # Wordmark
    wm_fs = 150
    wm_scale = wm_fs / m["upem"]
    wm_start = tile_x + tile + 50
    letter_spacing_em = 0.005
    text = "grelmıcro"
    wm_xs: list[float] = []
    x = wm_start
    for ch in text:
        wm_xs.append(x)
        x += _glyph_advance(font, ch) * wm_scale + letter_spacing_em * wm_fs
    wm_end = x
    available = vw - wm_start - 60
    if wm_end - wm_start > available:
        wm_fs = wm_fs * available / (wm_end - wm_start)
        wm_scale = wm_fs / m["upem"]
        wm_xs = []
        x = wm_start
        for ch in text:
            wm_xs.append(x)
            x += _glyph_advance(font, ch) * wm_scale + letter_spacing_em * wm_fs
    wm_baseline = vh / 2 + wm_fs * 0.22
    iota = _bbox(font, "ı")
    iota_cx = (iota[0] + iota[2]) / 2 * wm_scale
    wm_i_cx = wm_xs[text.index("ı")] + iota_cx
    wm_g_cx = wm_xs[text.index("g")] + m["g_stem_cx"] * wm_scale
    wm_tit = m["square"] * wm_scale
    wm_tit_y = wm_baseline - m["ascender"] * wm_scale

    lines = [_svg_header(vw, vh, "grelmicro: Python primitives")]
    lines.append(f'  <rect width="{vw}" height="{vh}" fill="{PAPER}"/>')
    lines.append(
        f'  <rect x="{tile_x}" y="{tile_y}" width="{tile}" height="{tile}" '
        f'rx="78" fill="{INK}"/>'
    )
    lines.append(
        f'  <path d="{_glyph_path(font, "g")}" fill="{PAPER}" '
        f'transform="translate({tg_x:.3f} {tg_baseline:.3f}) '
        f'scale({t_scale:.6f} -{t_scale:.6f})"/>'
    )
    lines.append(
        f'  <rect x="{tg_cx - t_tit / 2:.3f}" y="{t_tit_y:.3f}" '
        f'width="{t_tit:.3f}" height="{t_tit:.3f}" fill="{RED}" '
        f'shape-rendering="crispEdges"/>'
    )
    for idx, ch in enumerate(text):
        fill = INK if idx < 4 else RED
        lines.append(
            f'  <path d="{_glyph_path(font, ch)}" fill="{fill}" '
            f'transform="translate({wm_xs[idx]:.3f} {wm_baseline:.3f}) '
            f'scale({wm_scale:.6f} -{wm_scale:.6f})"/>'
        )
    for cx in (wm_i_cx, wm_g_cx):
        lines.append(
            f'  <rect x="{cx - wm_tit / 2:.3f}" y="{wm_tit_y:.3f}" '
            f'width="{wm_tit:.3f}" height="{wm_tit:.3f}" fill="{RED}" '
            f'shape-rendering="crispEdges"/>'
        )
    tag = "Python primitives. Micro by design. Fast by default."
    # Note: the main tagline ("Import only what you need.") stays in the
    # README body rather than on the card, to keep the card visually calm.
    lines.append(
        f'  <text x="{wm_start:.3f}" y="{wm_baseline + wm_fs * 0.45:.3f}" '
        f'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" '
        f'font-size="26" fill="{INK}" opacity="0.6">{tag}</text>'
    )
    lines.append("</svg>\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# PNG rasterisation via Chrome headless
# --------------------------------------------------------------------------- #


def rasterise(svg: Path, png: Path, width: int, height: int) -> None:
    """Rasterise an SVG to PNG using resvg (bundled Rust binary)."""
    data = resvg_py.svg_to_bytes(
        svg_string=svg.read_text(),
        width=width,
        height=height,
    )
    png.write_bytes(bytes(data))


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    font = TTFont(str(_ensure_font()))
    m = measure(font)

    print(f"Funnel Sans {WEIGHT} · upem={m['upem']}")
    print(f"  ascender top        : {m['ascender']} fu")
    print(f"  g stem centre       : {m['g_stem_cx']:.2f} fu")
    print(f"  square side         : {m['square']:.2f} fu")

    (OUT_DIR / "wordmark.svg").write_text(build_wordmark(font, m, dark=False))
    (OUT_DIR / "wordmark-dark.svg").write_text(
        build_wordmark(font, m, dark=True)
    )
    (OUT_DIR / "favicon.svg").write_text(build_favicon(font, m, dark=False))
    (OUT_DIR / "favicon-dark.svg").write_text(build_favicon(font, m, dark=True))
    (OUT_DIR / "social-preview.svg").write_text(build_social_preview(font, m))

    fav = OUT_DIR / "favicon.svg"
    for size in (16, 32, 48, 192, 512):
        rasterise(fav, OUT_DIR / f"favicon-{size}.png", size, size)
    rasterise(fav, OUT_DIR / "apple-touch-icon.png", 180, 180)
    rasterise(
        OUT_DIR / "social-preview.svg",
        OUT_DIR / "social-preview.png",
        1200,
        630,
    )

    print(f"\nWrote {len(list(OUT_DIR.glob('*')))} files to {OUT_DIR}/")


if __name__ == "__main__":
    sys.exit(main())
