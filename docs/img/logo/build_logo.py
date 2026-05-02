# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fonttools>=4.55",
#     "resvg-py>=0.3",
#     "pillow>=10.0",
# ]
# ///
"""Build the grelmicro logo asset set from Funnel Display glyph outlines.

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
no system C libs). The Funnel Display ``.ttf`` is cached in the OS temp
directory and downloaded on first run.
"""

from __future__ import annotations

import io
import math
import sys
import tempfile
import urllib.request
from pathlib import Path

import resvg_py
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from PIL import Image

# The script sits inside the asset directory it writes to.
OUT_DIR = Path(__file__).resolve().parent
# Funnel Display Bold (static). Upstream source (SIL OFL 1.1).
FONT_CACHE = Path(tempfile.gettempdir()) / "grelmicro-funnel-display-bold.ttf"
FONT_URL = (
    "https://raw.githubusercontent.com/Dicotype/Funnel/main/"
    "fonts/Funnel_Display/ttf/FunnelDisplay-Bold.ttf"
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
    # Download to a sibling temp file then atomically rename, so a concurrent
    # invocation (or a crashed one) never leaves ``FONT_CACHE`` half-written.
    with tempfile.NamedTemporaryFile(
        dir=FONT_CACHE.parent,
        prefix=FONT_CACHE.name,
        suffix=".part",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        urllib.request.urlretrieve(FONT_URL, tmp_path)
        tmp_path.replace(FONT_CACHE)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
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


def _units_per_em(font: TTFont) -> int:
    """Read the font's units-per-em with a runtime isinstance check.

    ``font["head"]`` is typed as the abstract ``DefaultTable`` by
    fonttools' stubs, so the concrete ``unitsPerEm`` attribute is
    invisible to a type checker. Using ``getattr`` + ``isinstance``
    narrows the value without resorting to ``typing.cast``.
    """
    value = getattr(font["head"], "unitsPerEm", None)
    if not isinstance(value, int):
        msg = f"Font has no integer unitsPerEm (got {type(value).__name__})"
        raise TypeError(msg)
    return value


def measure(font: TTFont) -> dict:
    tit = _dot_bbox(font)
    g_bb = _bbox(font, "g")
    stem = _g_stem_geom(font)
    return {
        "upem": _units_per_em(font),
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


def build_wordmark(
    font: TTFont,
    m: dict,
    *,
    dark: bool,
    with_plate: bool,
) -> str:
    """Tight-padded wordmark.

    ``with_plate=True`` emits a paper- or ink-coloured rounded rectangle
    behind the letters so the mark stays readable on any surface
    (universal fallback for GitHub / PyPI / generic renderers).

    ``with_plate=False`` emits a transparent-background mark. Intended
    for contexts where the hosting page controls the surface colour
    (typically swapped via CSS in Material-theme dark mode).
    """
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
    if with_plate:
        plate_fill = INK if dark else PAPER
        lines.append(
            f'  <rect width="{vw}" height="{vh}" rx="12" fill="{plate_fill}"/>'
        )
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
    # viewBox 1536 (3x the visual size of 512) is the LCM of all common
    # favicon render sizes: 16, 32, 48, 192, 256, 512. Coordinates that
    # are multiples of 96 vbu therefore land on integer pixel boundaries
    # at every one of those render sizes (LCM of 1536/N for N in that
    # set is 96). Compared to viewBox 512, this lets us pixel-snap at
    # 48 and 192 — the two non-power-of-two sizes that drift fractional
    # in the smaller viewBox.
    view = 1536
    snap = 96
    # Pick the dot/stem size first so the dot square matches the g's right
    # stem width exactly. Choosing 2*snap gives a stem of 192 vbu = 64 px
    # @ 512 render, very close to the original ~62 px natural width.
    target_stem_w = 2 * snap  # 192 vbu
    scale = target_stem_w / m["g_stem_w"]
    asc, desc = m["ascender"] * scale, m["descender"] * scale
    top_margin = (view - (asc + desc)) / 2
    baseline = top_margin + asc

    g_advance = _glyph_advance(font, "g") * scale
    g_x_natural = view / 2 - g_advance / 2

    # Snap the g's right-stem outer edge to a multiple of `snap` so the
    # stem and the red dot (which shares its inner edge) land on integer
    # pixels at every supported render size, not just 48.
    stem_outer_fu = m["g_stem_cx"] + m["g_stem_w"] / 2
    right_stem_vb = g_x_natural + stem_outer_fu * scale
    snapped_outer = round(right_stem_vb / snap) * snap
    g_x = g_x_natural + (snapped_outer - right_stem_vb)

    # Red dot anchored to the g's right stem. Width, x and y all snap
    # to `snap` so the rect's corners hit integer pixels at every
    # supported render size.
    stem_inner_fu = m["g_stem_cx"] - m["g_stem_w"] / 2
    tit_x_natural = g_x + stem_inner_fu * scale
    tit_x = round(tit_x_natural / snap) * snap
    tit_side_natural = m["g_stem_w"] * scale
    tit_side = round(tit_side_natural / snap) * snap or snap
    tit_y_natural = baseline - m["ascender"] * scale
    tit_y = round(tit_y_natural / snap) * snap
    plate_rx = round(view * 0.22 / snap) * snap

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
    tag = "Async-first toolkit. Microservice patterns inside."
    lines.append(
        f'  <text x="{wm_start:.3f}" y="{wm_baseline + wm_fs * 0.45:.3f}" '
        f'font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" '
        f'font-size="26" fill="{INK}" opacity="0.6">{tag}</text>'
    )
    lines.append("</svg>\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# PNG rasterisation via resvg-py
# --------------------------------------------------------------------------- #


# Render at SUPERSAMPLE times the target size, then downsample with Lanczos.
# resvg's direct rasterisation at 16/32/48 px makes the g's curves look
# aliased because there are too few pixels for the renderer's AA to work
# with. Supersampling renders the geometry into a much larger grid where
# AA has room to breathe, and Lanczos resampling preserves the smoothness
# when shrinking back to the target size.
SUPERSAMPLE = 4


def rasterise(svg: Path, png: Path, width: int, height: int) -> None:
    """Rasterise an SVG to PNG with supersample anti-aliasing."""
    data = resvg_py.svg_to_bytes(
        svg_string=svg.read_text(),
        width=width * SUPERSAMPLE,
        height=height * SUPERSAMPLE,
    )
    with Image.open(io.BytesIO(bytes(data))) as img:
        img.resize((width, height), Image.Resampling.LANCZOS).save(
            png, "PNG", optimize=True
        )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    font = TTFont(str(_ensure_font()))
    m = measure(font)

    print(f"Funnel Display {WEIGHT} · upem={m['upem']}")
    print(f"  ascender top        : {m['ascender']} fu")
    print(f"  g stem centre       : {m['g_stem_cx']:.2f} fu")
    print(f"  square side         : {m['square']:.2f} fu")

    written: list[Path] = []

    def _write_svg(name: str, svg: str) -> Path:
        path = OUT_DIR / name
        path.write_text(svg)
        written.append(path)
        return path

    def _raster(svg: Path, name: str, width: int, height: int) -> None:
        path = OUT_DIR / name
        rasterise(svg, path, width, height)
        written.append(path)

    # Default wordmark: light (paper plate + ink letters). Universal
    # fallback that reads on any surface, safe for every renderer.
    _write_svg(
        "wordmark.svg",
        build_wordmark(font, m, dark=False, with_plate=True),
    )
    _write_svg(
        "wordmark-dark.svg",
        build_wordmark(font, m, dark=True, with_plate=True),
    )
    # Transparent variants for contexts that control their own surface
    # colour (e.g. Material-theme dark mode swapping via CSS).
    _write_svg(
        "wordmark-transparent.svg",
        build_wordmark(font, m, dark=False, with_plate=False),
    )
    _write_svg(
        "wordmark-transparent-dark.svg",
        build_wordmark(font, m, dark=True, with_plate=False),
    )
    fav = _write_svg("favicon.svg", build_favicon(font, m, dark=False))
    _write_svg("favicon-dark.svg", build_favicon(font, m, dark=True))
    social = _write_svg("social-preview.svg", build_social_preview(font, m))

    for size in (16, 32, 48, 192, 512):
        _raster(fav, f"favicon-{size}.png", size, size)
    _raster(fav, "apple-touch-icon.png", 180, 180)
    _raster(social, "social-preview.png", 1200, 630)

    print(f"\nWrote {len(written)} files to {OUT_DIR}/")


if __name__ == "__main__":
    sys.exit(main())
