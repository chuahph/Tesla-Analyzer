#!/usr/bin/env python3
"""Generate the PWA / iOS home-screen app icons.

Draws a dark rounded-square tile with a Tesla-red lightning bolt, at the sizes
iOS and the web app manifest need. Run once; the PNGs are committed.

    python scripts/make_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG = (14, 17, 22, 255)        # --bg  #0e1116
PANEL = (31, 36, 45, 255)     # subtle inner tile
RED = (232, 33, 39, 255)      # Tesla red #e82127

# Lightning bolt outline in a 0..1 unit square.
BOLT = [
    (0.575, 0.07), (0.30, 0.55), (0.47, 0.55),
    (0.40, 0.93), (0.74, 0.42), (0.55, 0.42), (0.60, 0.07),
]


def _draw(size: int, padding: float = 0.0) -> Image.Image:
    # Supersample for smooth edges.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BG)
    inset = int(s * 0.06)
    d.rounded_rectangle(
        [inset, inset, s - inset - 1, s - inset - 1],
        radius=int(radius * 0.8), outline=PANEL, width=max(2, int(s * 0.012)),
    )

    # Bolt, optionally inset for a maskable "safe zone".
    scale = 1.0 - 2 * padding
    pts = [(padding * s + x * scale * s, padding * s + y * scale * s) for x, y in BOLT]
    d.polygon(pts, fill=RED)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (180, 192, 512):
        _draw(size).save(OUT / f"icon-{size}.png")
        print(f"  wrote icons/icon-{size}.png")
    # Maskable variant with safe padding so iOS/Android masks don't clip the bolt.
    _draw(512, padding=0.12).save(OUT / "icon-512-maskable.png")
    print("  wrote icons/icon-512-maskable.png")


if __name__ == "__main__":
    main()
