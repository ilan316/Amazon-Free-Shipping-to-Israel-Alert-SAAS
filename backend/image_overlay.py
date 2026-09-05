"""Brand bars drawn onto the Instagram product image.

Instagram captions are not clickable and get folded after ~2 lines, so a bare
Amazon catalog shot carries no brand, no price and no reason to stop scrolling.
These bars put all three on the pixels themselves.

Colors are the site's own tokens from amzfreeil-www/styles.css.
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_FONT_PATH = _ROOT / "backend" / "assets" / "fonts" / "Heebo[wght].ttf"
_LOGO_PATH = _ROOT / "frontend" / "static" / "logo-new.png"

# Site palette (:root in amzfreeil-www/styles.css)
_CREAM = (255, 250, 241)   # --bg
_NAVY = (23, 32, 51)       # --ink
_BRAND = (255, 153, 0)     # --brand
_WHITE = (255, 255, 255)

# Bar geometry, as a fraction of the (square) canvas edge
_TOP_H = 0.13
_BOTTOM_H = 0.19
_RULE = 0.0021             # ~3px on a 1440 canvas
_PAD = 0.045
FREE_BAND = 1 - _TOP_H - _BOTTOM_H   # what's left for the product itself

# Pillow's binary wheels stopped bundling libraqm, so the default layout engine
# draws in logical order — Hebrew comes out reversed. When raqm is missing we
# reorder the string ourselves. A naive reverse() would mangle the Latin words
# that name_he routinely contains ("...Speedo Biofuse 2.0 Junior"), so this
# needs the real bidi algorithm.
_HAS_RAQM = features.check("raqm")
if not _HAS_RAQM:
    from bidi.algorithm import get_display


def _shape(text: str) -> str:
    return text if _HAS_RAQM else get_display(text)


@lru_cache(maxsize=32)
def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(_FONT_PATH), size)
    f.set_variation_by_axes([weight])
    return f


def _width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _wrap(draw, text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    """Greedy word wrap; the last line is ellipsized if the text doesn't fit."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and _width(draw, trial, font) > max_w:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
        else:
            current = trial
    if len(lines) < max_lines and current:
        lines.append(current)

    if len(lines) == max_lines and current and lines[-1] != current:
        # Ran out of lines with words still pending — trim the tail to fit "…"
        tail = lines[-1]
        while tail and _width(draw, tail + "…", font) > max_w:
            tail = tail.rsplit(" ", 1)[0] if " " in tail else tail[:-1]
        lines[-1] = (tail + "…") if tail else lines[-1]
    return lines


def _clean_price(raw: str | None) -> str:
    """'ILS 82.50' -> '82.50 ₪'. Same normalization as scheduler._format_price,
    but the shekel sign rather than 'ש"ח' — this is a numeric badge, not prose."""
    p = (raw or "").replace("ILS", "").replace("₪", "").strip()
    return f"{p} ₪" if re.search(r"\d", p) else ""


def draw_bars(canvas: Image.Image, name: str | None, price: str | None) -> Image.Image:
    """Paint the top (cream + logo) and bottom (navy + name/price) bars in place."""
    W, H = canvas.size
    draw = ImageDraw.Draw(canvas)
    pad = int(W * _PAD)
    rule = max(2, int(W * _RULE))

    top_h = int(H * _TOP_H)
    bottom_h = int(H * _BOTTOM_H)

    # ---- top bar -----------------------------------------------------------
    draw.rectangle([0, 0, W, top_h], fill=_CREAM)
    draw.rectangle([0, top_h - rule, W, top_h], fill=_BRAND)

    if _LOGO_PATH.exists():
        logo = Image.open(_LOGO_PATH).convert("RGBA")
        # The logo ships on its own near-cream plate, which reads as a visible box
        # against the bar. Knock that plate out so only the mark lands on the bar.
        base = logo.getpixel((0, 0))[:3]
        px = logo.load()
        for y in range(logo.height):
            for x in range(logo.width):
                r, g, b, a = px[x, y]
                if a and abs(r - base[0]) < 24 and abs(g - base[1]) < 24 and abs(b - base[2]) < 24:
                    px[x, y] = (r, g, b, 0)
        target_h = int(top_h * 0.74)
        logo.thumbnail((W, target_h), Image.LANCZOS)
        canvas.paste(logo, (pad, (top_h - logo.height) // 2), logo)

    ship_font = _font(int(W * 0.041), 700)
    ship = _shape("משלוח חינם לישראל")
    ship_w = _width(draw, ship, ship_font)
    ship_box = draw.textbbox((0, 0), ship, font=ship_font)
    draw.text((W - pad - ship_w, (top_h - rule - (ship_box[3] - ship_box[1])) // 2 - ship_box[1]),
              ship, font=ship_font, fill=_NAVY)

    # ---- bottom bar --------------------------------------------------------
    b_top = H - bottom_h
    draw.rectangle([0, b_top, W, H], fill=_NAVY)
    draw.rectangle([0, b_top, W, b_top + rule], fill=_BRAND)

    # Price first: it is fixed-width and sits on the left, so it decides where the
    # name's left edge has to stop.
    name_left = pad
    name_right = W - pad
    price_text = _clean_price(price)
    if price_text:
        price_font = _font(int(W * 0.085), 900)
        pbox = draw.textbbox((0, 0), price_text, font=price_font)
        py = b_top + rule + (bottom_h - rule - (pbox[3] - pbox[1])) // 2 - pbox[1]
        draw.text((pad, py), price_text, font=price_font, fill=_BRAND)
        name_left = pad + (pbox[2] - pbox[0]) + int(W * 0.04)

    if name:
        name_font = _font(int(W * 0.043), 500)
        max_w = name_right - name_left
        lines = _wrap(draw, name.strip(), name_font, max_w, max_lines=2)
        line_h = int(name_font.size * 1.25)
        block_h = line_h * len(lines)
        y = b_top + rule + (bottom_h - rule - block_h) // 2
        for line in lines:
            shaped = _shape(line)
            draw.text((name_right - _width(draw, shaped, name_font), y),
                      shaped, font=name_font, fill=_WHITE)
            y += line_h

    return canvas
