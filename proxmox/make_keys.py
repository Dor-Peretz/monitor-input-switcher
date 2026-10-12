#!/usr/bin/env python3
"""Draw the standby key images. No third-party packages.

Stream Deck MK.2 shows a 72x72 JPEG mirrored on both axes, so these PNGs
are already flipped. Convert them with:

    sips -s format jpeg -s formatOptions 70 proxmox/keys/*.png --out ...
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 72
OUT = Path(__file__).resolve().parent / "keys"

# 5x7 glyphs, top row first. Bits are left to right in the low 5 bits.
FONT = {
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b11110, 0b10001, 0b10001, 0b10001, 0b11110),
    "C": (0b01111, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b01111),
    "D": (0b11110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11110),
    "E": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000, 0b10000),
    "G": (0b01111, 0b10000, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111),
    "H": (0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001, 0b10001),
    "I": (0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "K": (0b10001, 0b10010, 0b11100, 0b10010, 0b10001, 0b10001, 0b10001),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b10101, 0b01010),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    " ": (0, 0, 0, 0, 0, 0, 0),
}


def png(size: int, rgb: bytearray) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = bytearray()
    row = size * 3
    for y in range(size):
        raw.append(0)
        raw.extend(rgb[y * row : (y + 1) * row])
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def draw(lines: list[str], background: tuple[int, int, int], ink: tuple[int, int, int]) -> bytes:
    """Upright label, stored mirrored so the MK.2 panel shows it upright."""
    pixels = bytearray(SIZE * SIZE * 3)
    for i in range(0, len(pixels), 3):
        pixels[i : i + 3] = bytes(background)
    scale = 3
    line_h = 7 * scale
    gap = 4
    block_h = len(lines) * line_h + (len(lines) - 1) * gap
    top = (SIZE - block_h) // 2
    for index, line in enumerate(lines):
        text = line.upper()
        width = len(text) * (5 * scale + scale) - scale
        left = (SIZE - width) // 2
        origin_y = top + index * (line_h + gap)
        for char_index, char in enumerate(text):
            glyph = FONT.get(char, FONT[" "])
            origin_x = left + char_index * (5 * scale + scale)
            for row, bits in enumerate(glyph):
                for col in range(5):
                    if not (bits & (1 << (4 - col))):
                        continue
                    for dy in range(scale):
                        for dx in range(scale):
                            x = origin_x + col * scale + dx
                            y = origin_y + row * scale + dy
                            # MK.2 key glass is rotated 180 degrees.
                            fx = SIZE - 1 - x
                            fy = SIZE - 1 - y
                            if 0 <= fx < SIZE and 0 <= fy < SIZE:
                                offset = (fy * SIZE + fx) * 3
                                pixels[offset : offset + 3] = bytes(ink)
    return png(SIZE, pixels)


KEYS = {
    "main-mac": (["ON", "MAC"], (20, 72, 150), (255, 255, 255)),
    "main-pc": (["ON", "PC"], (12, 110, 78), (255, 255, 255)),
    "main-ha": (["HA"], (150, 90, 20), (255, 255, 255)),
    "ha-back": (["BACK"], (50, 54, 64), (255, 255, 255)),
    "ha-both-off": (["BOTH"], (40, 44, 54), (220, 220, 220)),
    "ha-both-on": (["BOTH"], (20, 110, 70), (255, 255, 255)),
    "ha-lamp-off": (["LAMP"], (40, 44, 54), (220, 220, 220)),
    "ha-lamp-on": (["LAMP"], (160, 120, 20), (255, 255, 255)),
    "ha-light-off": (["LIGHT"], (40, 44, 54), (220, 220, 220)),
    "ha-light-on": (["LIGHT"], (40, 90, 160), (255, 255, 255)),
    "ha-ac-off": (["AC"], (40, 44, 54), (220, 220, 220)),
    "ha-ac-on": (["AC"], (20, 120, 140), (255, 255, 255)),
    "ha-red-off": (["RED"], (40, 44, 54), (220, 220, 220)),
    "ha-red-on": (["RED"], (160, 30, 30), (255, 255, 255)),
    "ha-on": (["ON"], (16, 100, 64), (255, 255, 255)),
    "ha-off": (["OFF"], (120, 36, 36), (255, 255, 255)),
    "blank": ([" "], (8, 8, 10), (8, 8, 10)),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (lines, background, ink) in KEYS.items():
        path = OUT / f"{name}.png"
        path.write_bytes(draw(lines, background, ink))
        print(path)


if __name__ == "__main__":
    main()
