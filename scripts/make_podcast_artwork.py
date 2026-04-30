"""Compose a 3000x3000 Apple-Podcasts-compliant cover image.

Apple requires:
- 3000x3000 px (or 1400x1400 minimum)
- JPG or PNG, RGB color space (no alpha for PNG)
- HTTP HEAD-able URL

Output: docs/artwork.png — overwrites whatever was there.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
LOGO_PATH = ROOT.parent / "harro-life-site" / "public" / "images" / "brand" / "harro-life-on-dark.png"
OUTPUT = ROOT / "docs" / "artwork.png"

# HARRO brand navy (matches site / email / wordmark background)
NAVY = (9, 32, 46)         # #09202E
CREAM = (234, 230, 195)    # #EAE6C3 — subtle accent for tagline

SIZE = 3000


def main() -> int:
    if not LOGO_PATH.exists():
        print(f"Logo not found: {LOGO_PATH}", file=sys.stderr)
        return 1

    # Solid navy canvas, RGB (no alpha — Apple requires RGB color space)
    canvas = Image.new("RGB", (SIZE, SIZE), NAVY)

    # Load wordmark (2048x768, RGBA on dark — designed for navy bg).
    # The source has internal padding around the glyphs, so we crop to the
    # actual content bounding box first for tighter composition.
    logo = Image.open(LOGO_PATH).convert("RGBA")
    bbox = logo.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    logo_w, logo_h = logo.size

    # Scale logo so its width = 70% of canvas (~2100px wide), tight composition.
    target_w = int(SIZE * 0.70)
    scale = target_w / logo_w
    target_h = int(logo_h * scale)
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    # Pre-compute tagline height so we can center the (logo + gap + tagline)
    # group as one composition rather than centering the logo alone.
    tagline = "オランダのニュースを、日本語で"
    font = None
    for candidate in (
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        if Path(candidate).exists():
            try:
                from PIL import ImageFont
                font = ImageFont.truetype(candidate, size=130)
                break
            except Exception:
                font = None

    tagline_h = 0
    text_w = 0
    if font:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(canvas)
        bbox_t = draw.textbbox((0, 0), tagline, font=font)
        text_w = bbox_t[2] - bbox_t[0]
        tagline_h = bbox_t[3] - bbox_t[1]

    gap = int(SIZE * 0.06) if tagline_h else 0
    group_h = target_h + gap + tagline_h
    group_y = (SIZE - group_h) // 2

    x = (SIZE - target_w) // 2
    y = group_y
    canvas.paste(logo, (x, y), logo)

    # Render the tagline below the logo (already font-resolved above).
    if font and tagline_h:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(canvas)
        tx = (SIZE - text_w) // 2
        ty = y + target_h + gap
        draw.text((tx, ty), tagline, fill=CREAM, font=font)
        print(f"Tagline rendered with font: {font.path}")
    else:
        print("No Japanese font found — skipped tagline.")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUTPUT, format="PNG", optimize=True)
    print(f"Wrote {OUTPUT} ({SIZE}x{SIZE} RGB PNG, {OUTPUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
