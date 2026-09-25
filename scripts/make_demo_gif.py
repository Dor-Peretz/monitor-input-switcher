#!/usr/bin/env python3
"""Render docs/demo.gif — the animated explainer of a whole-desk PC switch.

Drawn from scratch with the standard library only: a 5x7 bitmap font, simple
anti-aliased shapes and a GIF89a/LZW writer, so the asset can be regenerated
anywhere without an imaging dependency.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "demo.gif"

WIDTH, HEIGHT = 800, 450
FRAMES = 48
HALF = FRAMES // 2
FRAME_DELAY = 6  # hundredths of a second
HOLD_DELAY = 55  # longer pause on the last steady frame before each press

BG = (14, 17, 23)
PANEL = (28, 33, 43)
EDGE = (52, 60, 75)
RAIL = (44, 51, 65)
TEXT = (226, 232, 240)
MUTED = (122, 134, 156)
DARK_SCREEN = (10, 12, 17)
WHITE = (255, 255, 255)

BLUE = (56, 132, 255)
GREEN = (16, 185, 129)

# Geometry
MON_TOP, MON_BOTTOM = 58, 238
MON_1 = (78, 366)
MON_2 = (434, 722)
STAND_1 = (MON_1[0] + MON_1[1]) // 2
STAND_2 = (MON_2[0] + MON_2[1]) // 2
RAIL_Y = 282
KEY = (340, 295, 460, 415)
KEY_CX = (KEY[0] + KEY[2]) // 2

PATH_LEFT = [(KEY_CX, KEY[1]), (KEY_CX, RAIL_Y), (STAND_1, RAIL_Y), (STAND_1, 268)]
PATH_RIGHT = [(KEY_CX, KEY[1]), (KEY_CX, RAIL_Y), (STAND_2, RAIL_Y), (STAND_2, 268)]

STATES = (
    {"accent": BLUE, "key": "MAC", "input": "USB-C"},
    {"accent": GREEN, "key": "PC", "input": "DISPLAYPORT"},
)

MONITOR_NAMES = ("DELL P2721Q", "DELL U2720Q")


FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    "*": ["00000", "00000", "01100", "01100", "00000", "00000", "00000"],
    " ": ["00000"] * 7,
}


def mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class Frame:
    def __init__(self, width: int, height: int, background: tuple) -> None:
        self.w = width
        self.h = height
        self.px = bytearray(bytes(background) * (width * height))

    def put(self, x: int, y: int, rgb: tuple, alpha: float = 1.0) -> None:
        if alpha <= 0 or not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) * 3
        if alpha >= 1:
            self.px[i], self.px[i + 1], self.px[i + 2] = rgb
            return
        inverse = 1 - alpha
        for offset in range(3):
            self.px[i + offset] = int(self.px[i + offset] * inverse + rgb[offset] * alpha)

    def rect(self, x0: float, y0: float, x1: float, y1: float, rgb: tuple) -> None:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(self.w, int(x1)), min(self.h, int(y1))
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(rgb) * (x1 - x0)
        for y in range(y0, y1):
            start = (y * self.w + x0) * 3
            self.px[start : start + len(row)] = row

    def round_rect(
        self, x0: float, y0: float, x1: float, y1: float, radius: float, rgb: tuple
    ) -> None:
        radius = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
        # Flat middle and sides fill fast; only the corners need per-pixel work.
        self.rect(x0 + radius, y0, x1 - radius, y1, rgb)
        self.rect(x0, y0 + radius, x0 + radius, y1 - radius, rgb)
        self.rect(x1 - radius, y0 + radius, x1, y1 - radius, rgb)
        for cx, cy, sx, sy in (
            (x0 + radius, y0 + radius, -1, -1),
            (x1 - radius, y0 + radius, 1, -1),
            (x0 + radius, y1 - radius, -1, 1),
            (x1 - radius, y1 - radius, 1, 1),
        ):
            for step_y in range(int(radius) + 2):
                for step_x in range(int(radius) + 2):
                    px, py = cx + sx * step_x, cy + sy * step_y
                    distance = ((px + 0.5 - cx) ** 2 + (py + 0.5 - cy) ** 2) ** 0.5
                    if distance <= radius - 0.5:
                        self.put(int(px), int(py), rgb)
                    elif distance <= radius + 0.5:
                        self.put(int(px), int(py), rgb, radius + 0.5 - distance)

    def circle(self, cx: float, cy: float, radius: float, rgb: tuple) -> None:
        for y in range(int(cy - radius - 1), int(cy + radius + 2)):
            for x in range(int(cx - radius - 1), int(cx + radius + 2)):
                distance = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
                if distance <= radius - 0.5:
                    self.put(x, y, rgb)
                elif distance <= radius + 0.5:
                    self.put(x, y, rgb, radius + 0.5 - distance)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        scale: int,
        rgb: tuple,
        center: bool = False,
    ) -> None:
        value = value.upper()
        width = len(value) * 6 * scale - scale
        if center:
            x -= width // 2
        for char in value:
            glyph = FONT.get(char)
            if glyph is not None:
                for row, bits in enumerate(glyph):
                    for col, bit in enumerate(bits):
                        if bit == "1":
                            self.rect(
                                x + col * scale,
                                y + row * scale,
                                x + col * scale + scale,
                                y + row * scale + scale,
                                rgb,
                            )
            x += 6 * scale


def point_on_path(points: list, t: float) -> tuple:
    spans = []
    total = 0.0
    for start, end in zip(points, points[1:]):
        length = ((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5
        spans.append((start, end, length))
        total += length
    target = max(0.0, min(1.0, t)) * total
    for start, end, length in spans:
        if target <= length or length == 0:
            ratio = 0 if length == 0 else target / length
            return (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
        target -= length
    return points[-1]


def draw_monitor(
    frame: Frame,
    bounds: tuple,
    stand_x: int,
    name: str,
    label: str | None,
    accent: tuple,
    brightness: float,
) -> None:
    left, right = bounds
    frame.round_rect(left, MON_TOP, right, MON_BOTTOM, 12, EDGE)
    frame.round_rect(left + 4, MON_TOP + 4, right - 4, MON_BOTTOM - 4, 9, DARK_SCREEN)

    centre_x = (left + right) // 2
    if label is None:
        frame.text(centre_x, 138, "NO SIGNAL", 2, (72, 80, 96), center=True)
    else:
        glow = mix(DARK_SCREEN, accent, 0.18 * brightness)
        frame.round_rect(left + 26, MON_TOP + 30, right - 26, MON_BOTTOM - 30, 8, glow)

        chip = mix(DARK_SCREEN, accent, brightness)
        chip_width = len(label) * 6 * 3 + 30
        frame.round_rect(
            centre_x - chip_width // 2, 118, centre_x + chip_width // 2, 158, 10, chip
        )
        frame.text(centre_x, 129, label, 3, mix(DARK_SCREEN, WHITE, brightness), center=True)
        frame.text(centre_x, 178, name, 1, mix(DARK_SCREEN, MUTED, brightness), center=True)

    frame.rect(stand_x - 5, MON_BOTTOM, stand_x + 5, MON_BOTTOM + 26, EDGE)
    frame.round_rect(stand_x - 34, MON_BOTTOM + 26, stand_x + 34, MON_BOTTOM + 33, 3, EDGE)


def draw_key(frame: Frame, accent: tuple, label: str, pressed: bool, glow: float) -> None:
    x0, y0, x1, y1 = KEY
    if glow > 0:
        for spread, strength in ((18, 0.16), (11, 0.26), (5, 0.4)):
            frame.round_rect(
                x0 - spread,
                y0 - spread,
                x1 + spread,
                y1 + spread,
                20 + spread,
                mix(BG, accent, strength * glow),
            )

    inset = 4 if pressed else 0
    face = mix(accent, WHITE, 0.18) if pressed else accent
    frame.round_rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset, 18, face)

    # Monitor-with-swap-arrows glyph, matching the Stream Deck key icon.
    cx, cy = KEY_CX, y0 + 44
    frame.round_rect(cx - 31, cy - 21, cx + 31, cy + 21, 6, WHITE)
    frame.round_rect(cx - 27, cy - 17, cx + 27, cy + 17, 4, face)
    frame.rect(cx - 2, cy + 21, cx + 2, cy + 29, WHITE)
    frame.rect(cx - 14, cy + 29, cx + 14, cy + 33, WHITE)
    for direction, offset in ((1, -7), (-1, 7)):
        y = cy + offset
        frame.rect(cx - 15, y - 1, cx + 15, y + 2, WHITE)
        tip = cx + direction * 19
        for step in range(6):
            frame.rect(tip - direction * step, y - step, tip - direction * step + 1, y + step + 1, WHITE)

    frame.text(KEY_CX, y1 - 30, label, 2, WHITE, center=True)


def render(index: int) -> Frame:
    local = index % HALF
    source = STATES[index // HALF]
    target = STATES[1 - index // HALF]

    frame = Frame(WIDTH, HEIGHT, BG)
    frame.text(40, 24, "MONITOR INPUT SWITCHER", 2, TEXT)
    frame.text(40, 44, "DDC/CI  *  ONE KEY MOVES THE WHOLE DESK", 1, MUTED)

    if local < 14:
        shown, accent, brightness = source["input"], source["accent"], 1.0
    elif local < 18:
        shown, accent, brightness = None, source["accent"], 0.0
    else:
        shown, accent = target["input"], target["accent"]
        brightness = min(1.0, (local - 17) / 4)

    # Key flips to the new PC at the moment the signal drops.
    key_state = source if local < 14 else target

    frame.rect(STAND_1 - 2, MON_BOTTOM + 33, STAND_1 + 2, RAIL_Y, RAIL)
    frame.rect(STAND_2 - 2, MON_BOTTOM + 33, STAND_2 + 2, RAIL_Y, RAIL)
    frame.rect(STAND_1, RAIL_Y - 2, STAND_2, RAIL_Y + 2, RAIL)
    frame.rect(KEY_CX - 2, RAIL_Y, KEY_CX + 2, KEY[1], RAIL)

    draw_monitor(frame, MON_1, STAND_1, MONITOR_NAMES[0], shown, accent, brightness)
    draw_monitor(frame, MON_2, STAND_2, MONITOR_NAMES[1], shown, accent, brightness)

    pressed = 8 <= local <= 11
    draw_key(
        frame,
        key_state["accent"],
        key_state["key"],
        pressed,
        1.0 if 8 <= local <= 20 else 0.55,
    )

    if 8 <= local <= 14:
        progress = (local - 8) / 6
        pulse = key_state["accent"]
        for path in (PATH_LEFT, PATH_RIGHT):
            x, y = point_on_path(path, progress)
            frame.circle(x, y, 9, mix(BG, pulse, 0.30))
            frame.circle(x, y, 5, mix(pulse, WHITE, 0.55))

    frame.text(WIDTH // 2, 424, "ONE KEY  *  WHOLE DESK  *  NO KVM", 2, MUTED, center=True)
    return frame


# --------------------------------------------------------------------------- #
# GIF writing
# --------------------------------------------------------------------------- #


def unique_colors(pixels: bytearray, shift: int) -> set:
    if shift:
        table = bytes((value >> shift) << shift for value in range(256))
        pixels = pixels.translate(table)
    return set(zip(pixels[0::3], pixels[1::3], pixels[2::3]))


def build_palette(frames: list) -> tuple:
    for shift in range(0, 7):
        colors: set = set()
        for frame in frames:
            colors |= unique_colors(frame.px, shift)
            if len(colors) > 256:
                break
        if len(colors) <= 256:
            return sorted(colors), shift
    raise RuntimeError("Could not reduce the image to 256 colours.")


def to_indices(pixels: bytearray, lookup: dict, shift: int) -> bytes:
    if shift:
        table = bytes((value >> shift) << shift for value in range(256))
        pixels = pixels.translate(table)
    return bytes(map(lookup.__getitem__, zip(pixels[0::3], pixels[1::3], pixels[2::3])))


def lzw_encode(indices: bytes, min_code_size: int) -> bytes:
    clear_code = 1 << min_code_size
    end_code = clear_code + 1

    out = bytearray()
    bit_buffer = 0
    bit_count = 0
    code_size = min_code_size + 1

    def emit(code: int) -> None:
        nonlocal bit_buffer, bit_count
        bit_buffer |= code << bit_count
        bit_count += code_size
        while bit_count >= 8:
            out.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8

    table: dict = {}
    next_code = end_code + 1
    emit(clear_code)

    prefix = indices[0]
    for byte in indices[1:]:
        key = (prefix, byte)
        found = table.get(key)
        if found is not None:
            prefix = found
            continue
        emit(prefix)
        table[key] = next_code
        next_code += 1
        if next_code > 4095:
            emit(clear_code)
            table = {}
            next_code = end_code + 1
            code_size = min_code_size + 1
        elif next_code > (1 << code_size):
            code_size += 1
        prefix = byte

    emit(prefix)
    emit(end_code)
    if bit_count:
        out.append(bit_buffer & 0xFF)
    return bytes(out)


def sub_blocks(data: bytes) -> bytes:
    out = bytearray()
    for start in range(0, len(data), 255):
        chunk = data[start : start + 255]
        out.append(len(chunk))
        out.extend(chunk)
    out.append(0)
    return bytes(out)


def dirty_rows(current: bytes, previous: bytes, width: int, height: int) -> tuple:
    stride = width * 3
    top = 0
    while top < height and current[top * stride : (top + 1) * stride] == previous[
        top * stride : (top + 1) * stride
    ]:
        top += 1
    if top == height:
        return 0, 1
    bottom = height
    while bottom > top and current[(bottom - 1) * stride : bottom * stride] == previous[
        (bottom - 1) * stride : bottom * stride
    ]:
        bottom -= 1
    return top, bottom


def write_gif(path: Path, frames: list, delays: list) -> None:
    palette, shift = build_palette(frames)
    lookup = {color: i for i, color in enumerate(palette)}

    bits = max(1, (len(palette) - 1).bit_length())
    table_size = 1 << bits
    min_code_size = max(2, bits)

    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", WIDTH, HEIGHT)
    out += bytes([0xF0 | (bits - 1), 0, 0])
    for color in palette:
        out += bytes(color)
    out += bytes(3 * (table_size - len(palette)))
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"

    previous = None
    for frame, delay in zip(frames, delays):
        if previous is None:
            top, bottom = 0, HEIGHT
        else:
            top, bottom = dirty_rows(frame.px, previous, WIDTH, HEIGHT)

        stride = WIDTH * 3
        region = frame.px[top * stride : bottom * stride]
        indices = to_indices(region, lookup, shift)

        out += b"\x21\xF9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00"
        out += b"\x2C" + struct.pack("<HHHH", 0, top, WIDTH, bottom - top) + b"\x00"
        out += bytes([min_code_size]) + sub_blocks(lzw_encode(indices, min_code_size))
        previous = frame.px

    out += b"\x3B"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def main() -> int:
    frames = []
    for index in range(FRAMES):
        frames.append(render(index))
        sys.stdout.write(f"\rrendering {index + 1}/{FRAMES}")
        sys.stdout.flush()
    print()

    delays = [HOLD_DELAY if i % HALF == 7 else FRAME_DELAY for i in range(FRAMES)]
    write_gif(OUT, frames, delays)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
