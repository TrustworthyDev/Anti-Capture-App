"""
win_icons.py

Extract an application's icon (from its .exe path) and return it as a
base64-encoded PNG string that Tkinter's PhotoImage(data=...) can load.

Pure ctypes + stdlib (zlib/base64) — no Pillow or pywin32, so the frozen exe
stays small. All failures are swallowed and return None so the caller can just
render a row without an icon.
"""

import base64
import ctypes
import struct
import zlib
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --- SHGetFileInfoW ------------------------------------------------------
SHGFI_ICON = 0x000000100
SHGFI_SMALLICON = 0x000000001
SHGFI_LARGEICON = 0x000000000
SHGFI_USEFILEATTRIBUTES = 0x000000010
FILE_ATTRIBUTE_NORMAL = 0x00000080
DIB_RGB_COLORS = 0
BI_RGB = 0


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


shell32.SHGetFileInfoW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFOW),
    wintypes.UINT, wintypes.UINT
]
shell32.SHGetFileInfoW.restype = ctypes.c_void_p

user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int

gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL


def _mask_alpha(hbm_mask, w, h):
    """Build a per-pixel alpha (0/255) from a 1bpp icon AND-mask (fallback for
    legacy icons that carry no real alpha channel)."""
    hdc = user32.GetDC(None)
    if not hdc:
        return None
    try:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 1
        bmi.bmiHeader.biCompression = BI_RGB
        stride = ((w + 31) // 32) * 4
        buf = (ctypes.c_ubyte * (stride * h))()
        if gdi32.GetDIBits(hdc, hbm_mask, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS) == 0:
            return None
        data = bytes(buf)
        alpha = bytearray(w * h)
        for y in range(h):
            row = y * stride
            for x in range(w):
                bit = (data[row + (x >> 3)] >> (7 - (x & 7))) & 1
                alpha[y * w + x] = 0 if bit else 255  # mask bit set => transparent
        return alpha
    finally:
        user32.ReleaseDC(None, hdc)


def _hicon_to_rgba(hicon):
    """Return (width, height, rgba_bytes) for an HICON, or None."""
    ii = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
        return None
    try:
        bm = BITMAP()
        if gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)) == 0:
            return None
        w, h = bm.bmWidth, bm.bmHeight
        if w <= 0 or h <= 0:
            return None

        hdc = user32.GetDC(None)
        if not hdc:
            return None
        try:
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h  # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB
            buf = (ctypes.c_ubyte * (w * h * 4))()
            if gdi32.GetDIBits(hdc, ii.hbmColor, 0, h, buf, ctypes.byref(bmi),
                               DIB_RGB_COLORS) == 0:
                return None
        finally:
            user32.ReleaseDC(None, hdc)

        data = bytes(buf)  # BGRA, top-down
        n = w * h * 4
        has_alpha = any(data[i] for i in range(3, n, 4))
        mask_alpha = None if has_alpha else _mask_alpha(ii.hbmMask, w, h)

        rgba = bytearray(n)
        for p in range(w * h):
            i = p * 4
            rgba[i] = data[i + 2]      # R <- from B position (BGRA -> RGBA)
            rgba[i + 1] = data[i + 1]  # G
            rgba[i + 2] = data[i]      # B
            if has_alpha:
                rgba[i + 3] = data[i + 3]
            elif mask_alpha is not None:
                rgba[i + 3] = mask_alpha[p]
            else:
                rgba[i + 3] = 255
        return w, h, bytes(rgba)
    finally:
        if ii.hbmColor:
            gdi32.DeleteObject(ii.hbmColor)
        if ii.hbmMask:
            gdi32.DeleteObject(ii.hbmMask)


def _png_from_rgba(w, h, rgba):
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0 (None) per scanline
        raw += rgba[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(typ, payload):
        return (struct.pack(">I", len(payload)) + typ + payload
                + struct.pack(">I", zlib.crc32(typ + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def _icon_rgba(path, small=True):
    """Return (w, h, rgba_bytes) for the file's icon, or None."""
    if not path:
        return None
    try:
        info = SHFILEINFOW()
        flags = SHGFI_ICON | (SHGFI_SMALLICON if small else SHGFI_LARGEICON)
        if not shell32.SHGetFileInfoW(path, 0, ctypes.byref(info),
                                      ctypes.sizeof(info), flags):
            return None
        hicon = info.hIcon
        if not hicon:
            return None
        try:
            return _hicon_to_rgba(hicon)
        finally:
            user32.DestroyIcon(hicon)
    except Exception:
        return None


def icon_png_base64(path, small=True):
    """Return a base64 PNG string for the icon of the file at `path`, or None."""
    res = _icon_rgba(path, small)
    if not res:
        return None
    try:
        w, h, rgba = res
        return base64.b64encode(_png_from_rgba(w, h, rgba)).decode("ascii")
    except Exception:
        return None


# --- checkbox drawing + compositing (pure Python) ------------------------
def _set_px(buf, w, x, y, color):
    i = (y * w + x) * 4
    buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = color


def _thick_line(buf, w, h, a, b, color, thick):
    x0, y0 = a
    x1, y1 = b
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        for ox in range(-thick, thick + 1):
            for oy in range(-thick, thick + 1):
                xx, yy = x0 + ox, y0 + oy
                if 0 <= xx < w and 0 <= yy < h:
                    _set_px(buf, w, xx, yy, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _checkbox_rgba(size, checked):
    """Draw a checkbox: white square + gray border, plus a green check if set."""
    s = size
    buf = bytearray(s * s * 4)
    border = (95, 95, 95, 255)
    fill = (255, 255, 255, 255)
    t = max(2, s // 9)  # border thickness
    for y in range(s):
        for x in range(s):
            on_border = x < t or x >= s - t or y < t or y >= s - t
            _set_px(buf, s, x, y, border if on_border else fill)
    if checked:
        green = (34, 150, 66, 255)
        line_t = max(1, s // 11)
        p1 = (int(s * 0.22), int(s * 0.52))
        p2 = (int(s * 0.42), int(s * 0.72))
        p3 = (int(s * 0.78), int(s * 0.26))
        _thick_line(buf, s, s, p1, p2, green, line_t)
        _thick_line(buf, s, s, p2, p3, green, line_t)
    return s, s, bytes(buf)


def _paste(dst, dw, dh, src, sw, sh, ox, oy):
    for y in range(sh):
        dy = oy + y
        if dy < 0 or dy >= dh:
            continue
        for x in range(sw):
            dx = ox + x
            if dx < 0 or dx >= dw:
                continue
            si = (y * sw + x) * 4
            if src[si + 3] == 0:  # keep destination where source is transparent
                continue
            di = (dy * dw + dx) * 4
            dst[di:di + 4] = src[si:si + 4]


def row_image_b64(path, checked, box_size=20, gap=6, small=True):
    """Composite [checkbox][gap][app icon] into one PNG; return base64 or None."""
    try:
        cb_w, cb_h, cb = _checkbox_rgba(box_size, checked)
        icon = _icon_rgba(path, small) if path else None
        iw, ih, irgba = icon if icon else (0, 0, b"")
        dw = cb_w + (gap + iw if iw else 0)
        dh = max(cb_h, ih) if ih else cb_h
        canvas = bytearray(dw * dh * 4)
        _paste(canvas, dw, dh, cb, cb_w, cb_h, 0, (dh - cb_h) // 2)
        if iw:
            _paste(canvas, dw, dh, irgba, iw, ih, cb_w + gap, (dh - ih) // 2)
        return base64.b64encode(_png_from_rgba(dw, dh, bytes(canvas))).decode("ascii")
    except Exception:
        return None
