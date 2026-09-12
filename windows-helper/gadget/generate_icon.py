"""Generate the original Sessions terminal/checkmark icon using only the stdlib."""

from pathlib import Path
import math
import struct
import zlib


def rounded(x, y, left, top, right, bottom, radius):
    dx = max(left + radius - x, 0, x - (right - radius))
    dy = max(top + radius - y, 0, y - (bottom - radius))
    return math.hypot(dx, dy) <= radius


def line(x, y, ax, ay, bx, by, width):
    t = max(0, min(1, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) /
                   ((bx - ax) ** 2 + (by - ay) ** 2)))
    return math.hypot(x - ax - t * (bx - ax), y - ay - t * (by - ay)) <= width / 2


def pixel(x, y):
    if not rounded(x, y, 3, 3, 61, 61, 14):
        return (0, 0, 0, 0)
    color = (20, 54 + int(y / 5), 66 + int(y / 6), 255)
    if rounded(x, y, 12, 15, 52, 47, 5):
        color = (115, 226, 204, 255)
        if rounded(x, y, 15, 18, 49, 44, 2):
            color = (18, 38, 51, 255)
    if any(line(x, y, *segment, 3) for segment in
           ((21, 25, 27, 31), (27, 31, 21, 37), (32, 37, 38, 37))):
        color = (230, 250, 246, 255)
    if math.hypot(x - 48, y - 47) <= 12:
        color = (14, 35, 44, 255)
    if math.hypot(x - 48, y - 47) <= 9:
        color = (111, 226, 197, 255)
    if line(x, y, 43.5, 47, 47, 50.5, 2.5) or line(x, y, 47, 50.5, 53, 44, 2.5):
        color = (15, 53, 57, 255)
    return color


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(size):
    data = bytearray()
    samples = 3
    for y in range(size):
        data.append(0)
        for x in range(size):
            colors = [pixel((x + (dx + .5) / samples) * 64 / size,
                            (y + (dy + .5) / samples) * 64 / size)
                      for dy in range(samples) for dx in range(samples)]
            alpha = sum(color[3] for color in colors)
            data.extend([round(sum(color[channel] * color[3] for color in colors) / alpha)
                         if alpha else 0 for channel in range(3)] + [round(alpha / samples ** 2)])
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(data, 9)) + chunk(b"IEND", b""))


def generate(directory):
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [png(size) for size in sizes]
    offset = 6 + 16 * len(images)
    entries = []
    for size, image in zip(sizes, images):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    (directory / "sessions.ico").write_bytes(struct.pack("<HHH", 0, 1, len(images)) +
                                            b"".join(entries) + b"".join(images))
    (directory / "sessions.png").write_bytes(images[-1])


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent)
