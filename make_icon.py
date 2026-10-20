"""Generate a neutral 'document' app icon as a multi-size .ico (pure Python).

Renders a rounded blue tile with a white sheet (folded corner + text lines) at
high resolution, downsamples to several sizes for anti-aliasing, and packs them
into icon.ico. Also writes icon_preview.png (256px) to eyeball the result.
"""
import struct

W = 768  # high-res render canvas (divisible by all target sizes)

# palette (a calm steel-blue tile with a white document — looks generic)
TILE = (71, 97, 127, 255)     # #47617F
PAPER = (255, 255, 255, 255)  # white
FOLD = (215, 222, 232, 255)   # #D7DEE8 fold underside
LINE = (138, 152, 172, 255)   # #8A98AC text lines


def blank(w):
    return bytearray(w * w * 4)


def fill_run(buf, y, x0, x1, rgba):
    if y < 0 or y >= W:
        return
    x0 = max(0, x0); x1 = min(W - 1, x1)
    if x1 < x0:
        return
    i = (y * W + x0) * 4
    seg = bytes(rgba) * (x1 - x0 + 1)
    buf[i:i + len(seg)] = seg


def fill_rounded(buf, x0, y0, x1, y1, r, rgba):
    for y in range(y0, y1 + 1):
        dx = 0
        if y < y0 + r:
            dy = (y0 + r) - y
            dx = int(round(r - (r * r - dy * dy) ** 0.5)) if dy <= r else r
        elif y > y1 - r:
            dy = y - (y1 - r)
            dx = int(round(r - (r * r - dy * dy) ** 0.5)) if dy <= r else r
        fill_run(buf, y, x0 + dx, x1 - dx, rgba)


def fill_tri(buf, p0, p1, p2, rgba):
    pts = [p0, p1, p2]
    y0 = max(0, min(p[1] for p in pts))
    y1 = min(W - 1, max(p[1] for p in pts))
    edges = [(p0, p1), (p1, p2), (p2, p0)]
    for y in range(y0, y1 + 1):
        xs = []
        for (ax, ay), (bx, by) in edges:
            if (ay <= y < by) or (by <= y < ay):
                t = (y - ay) / (by - ay)
                xs.append(ax + t * (bx - ax))
        if len(xs) >= 2:
            fill_run(buf, y, int(min(xs)), int(max(xs)), rgba)


def render():
    buf = blank(W)
    # tile
    fill_rounded(buf, 64, 64, 704, 704, 150, TILE)
    # paper with folded top-right corner
    px0, py0, px1, py1, fold = 214, 176, 554, 600, 96
    fill_rounded(buf, px0, py0, px1, py1, 18, PAPER)
    # cut the corner (replace with tile) then draw the fold flap
    fill_tri(buf, (px1 - fold, py0), (px1, py0), (px1, py0 + fold), TILE)
    fill_tri(buf, (px1 - fold, py0), (px1, py0 + fold), (px1 - fold, py0 + fold), FOLD)
    # text lines
    lx0, lx1 = px0 + 40, px1 - 40
    for i, ly in enumerate((300, 360, 420, 480)):
        right = lx1 if i else px1 - fold - 12  # first line stops before the fold
        fill_rounded(buf, lx0, ly, right, ly + 22, 11, LINE)
    return bytes(buf)


def downsample(src, n):
    f = W // n
    out = bytearray(n * n * 4)
    inv = 1.0 / (f * f)
    for oy in range(n):
        for ox in range(n):
            r = g = b = a = 0
            bx = ox * f
            for yy in range(oy * f, (oy + 1) * f):
                base = (yy * W + bx) * 4
                for _ in range(f):
                    r += src[base]; g += src[base + 1]; b += src[base + 2]; a += src[base + 3]
                    base += 4
            oi = (oy * n + ox) * 4
            out[oi] = int(r * inv); out[oi + 1] = int(g * inv)
            out[oi + 2] = int(b * inv); out[oi + 3] = int(a * inv)
    return bytes(out)


def png(w, h, rgba):
    import zlib
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def main():
    base = render()
    sizes = [256, 128, 64, 48, 32, 16]
    images = []
    for n in sizes:
        rgba = downsample(base, n)
        images.append((n, png(n, n, rgba)))
        if n == 256:
            with open("icon_preview.png", "wb") as f:
                f.write(images[-1][1])
    # assemble .ico
    n = len(images)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    data = b""
    offset = 6 + 16 * n
    for size, data_png in images:
        wh = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(data_png), offset)
        data += data_png
        offset += len(data_png)
    with open("icon.ico", "wb") as f:
        f.write(header + entries + data)
    print("wrote icon.ico (%d sizes) and icon_preview.png" % n)


if __name__ == "__main__":
    main()
