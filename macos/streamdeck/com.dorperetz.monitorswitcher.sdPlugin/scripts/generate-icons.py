#!/usr/bin/env python3
"""Generate the Stream Deck key icons.

Writes PNGs with zlib/struct so the plugin needs no imaging dependency.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

IMGS = Path(__file__).resolve().parent.parent / "imgs"
SIZE = 144

WHITE = (255, 255, 255)
SLATE = (45, 52, 64)
BLUE = (30, 91, 168)
GREEN = (10, 112, 86)

# name -> (background, glyph, big)
# "big" centres the glyph; otherwise it sits above the title band.
ICONS = {
    "plugin": (SLATE, "swap", True),
    "action-pc": (SLATE, "desk", True),
    "action-input": (SLATE, "swap", True),
    "pc-a": (BLUE, "desk", False),
    "pc-b": (GREEN, "desk", False),
    "input-a": (BLUE, "swap", False),
    "input-b": (GREEN, "swap", False),
}


class Canvas:
    def __init__(self, size: int, background: tuple[int, int, int]) -> None:
        self.size = size
        self.data = bytearray(size * size * 3)
        for i in range(0, len(self.data), 3):
            self.data[i], self.data[i + 1], self.data[i + 2] = background

    def put(self, x: int, y: int, rgb: tuple[int, int, int], alpha: float = 1.0) -> None:
        if alpha <= 0 or not (0 <= x < self.size and 0 <= y < self.size):
            return
        i = (y * self.size + x) * 3
        if alpha >= 1:
            self.data[i], self.data[i + 1], self.data[i + 2] = rgb
            return
        inverse = 1 - alpha
        for offset in range(3):
            self.data[i + offset] = int(
                self.data[i + offset] * inverse + rgb[offset] * alpha
            )

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        width: float,
        rgb: tuple[int, int, int],
    ) -> None:
        half = width / 2
        pad = half + 1
        dx, dy = x1 - x0, y1 - y0
        length2 = dx * dx + dy * dy
        for y in range(int(min(y0, y1) - pad), int(max(y0, y1) + pad) + 1):
            for x in range(int(min(x0, x1) - pad), int(max(x0, x1) + pad) + 1):
                px, py = x + 0.5, y + 0.5
                if length2 == 0:
                    distance = math.hypot(px - x0, py - y0)
                else:
                    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length2))
                    distance = math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
                if distance <= half:
                    self.put(x, y, rgb)
                elif distance <= half + 0.7:
                    self.put(x, y, rgb, 0.4)

    def triangle(
        self,
        a: tuple[float, float],
        b: tuple[float, float],
        c: tuple[float, float],
        rgb: tuple[int, int, int],
    ) -> None:
        xs, ys = (a[0], b[0], c[0]), (a[1], b[1], c[1])
        for y in range(int(min(ys) - 1), int(max(ys) + 2)):
            for x in range(int(min(xs) - 1), int(max(xs) + 2)):
                if _inside(x + 0.5, y + 0.5, a, b, c):
                    self.put(x, y, rgb)

    def round_rect(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        radius: float,
        rgb: tuple[int, int, int],
        width: float = 0.0,
    ) -> None:
        """Filled when width is 0, otherwise an outline of that thickness."""
        radius = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
        for y in range(int(y0 - 1), int(y1 + 2)):
            for x in range(int(x0 - 1), int(x1 + 2)):
                px, py = x + 0.5, y + 0.5
                cx = min(max(px, x0 + radius), x1 - radius)
                cy = min(max(py, y0 + radius), y1 - radius)
                distance = math.hypot(px - cx, py - cy)
                if width <= 0:
                    if distance <= radius:
                        self.put(x, y, rgb)
                    elif distance <= radius + 0.7:
                        self.put(x, y, rgb, 0.4)
                    continue
                inner = radius - width
                if inner <= distance <= radius:
                    self.put(x, y, rgb)
                elif inner - 0.7 <= distance <= radius + 0.7:
                    self.put(x, y, rgb, 0.4)


def _inside(
    px: float,
    py: float,
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    def sign(p1, p2, p3) -> float:
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    return not (((d1 < 0) or (d2 < 0) or (d3 < 0)) and ((d1 > 0) or (d2 > 0) or (d3 > 0)))


def arrow(
    canvas: Canvas,
    x0: float,
    x1: float,
    y: float,
    width: float,
    rgb: tuple[int, int, int],
) -> None:
    """Horizontal arrow from x0 to x1, head at the x1 end."""
    head = width * 2.1
    direction = 1 if x1 > x0 else -1
    shaft_end = x1 - direction * head * 0.9
    canvas.line(x0, y, shaft_end, y, width, rgb)
    canvas.triangle(
        (x1, y),
        (shaft_end, y - head * 0.75),
        (shaft_end, y + head * 0.75),
        rgb,
    )


def draw_swap(canvas: Canvas, cx: float, cy: float, span: float) -> None:
    """Two opposing arrows — the swap mark."""
    width = max(3.0, span * 0.1)
    gap = span * 0.34
    arrow(canvas, cx - span / 2, cx + span / 2, cy - gap, width, WHITE)
    arrow(canvas, cx + span / 2, cx - span / 2, cy + gap, width, WHITE)


def draw_desk(canvas: Canvas, cx: float, cy: float, span: float) -> None:
    """A monitor with the swap mark on its screen."""
    stroke = max(2.5, span * 0.055)
    screen_w = span * 1.02
    screen_h = screen_w * 0.68
    stand_h = span * 0.15
    top = cy - (screen_h + stand_h) / 2

    canvas.round_rect(
        cx - screen_w / 2,
        top,
        cx + screen_w / 2,
        top + screen_h,
        span * 0.1,
        WHITE,
        stroke,
    )

    base = top + screen_h
    canvas.line(cx, base, cx, base + stand_h, stroke, WHITE)
    canvas.line(
        cx - screen_w * 0.22, base + stand_h, cx + screen_w * 0.22, base + stand_h, stroke, WHITE
    )

    arrow_w = max(2.2, span * 0.048)
    inner = screen_w * 0.54
    gap = screen_h * 0.17
    screen_cy = top + screen_h / 2
    arrow(canvas, cx - inner / 2, cx + inner / 2, screen_cy - gap, arrow_w, WHITE)
    arrow(canvas, cx + inner / 2, cx - inner / 2, screen_cy + gap, arrow_w, WHITE)


def png(size: int, pixels: bytearray) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = bytearray()
    row = size * 3
    for y in range(size):
        raw.append(0)
        raw.extend(pixels[y * row : (y + 1) * row])
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    IMGS.mkdir(parents=True, exist_ok=True)
    for name, (background, glyph, big) in ICONS.items():
        canvas = Canvas(SIZE, background)
        if big:
            cx = cy = SIZE / 2
            span = SIZE * 0.52
        else:
            # Leave the lower third clear for the key title.
            cx = SIZE / 2
            cy = SIZE * 0.36
            span = SIZE * 0.42
        if glyph == "swap":
            draw_swap(canvas, cx, cy, span)
        else:
            draw_desk(canvas, cx, cy, span)
        path = IMGS / f"{name}.png"
        path.write_bytes(png(SIZE, canvas.data))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
