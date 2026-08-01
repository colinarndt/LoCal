"""Draw the app icon and compile it to AppIcon.icns.

    python make_icon.py        # -> AppIcon.icns

Vector-drawn with CoreGraphics rather than shipped as a PNG: pyobjc is already a
dependency of the app, every size is rendered from the same geometry at its
native pixel count, and the source of the artwork is readable instead of being a
binary nobody can edit.

The mark is a calendar page -- binder tabs, header band, four ruled lines --
over the warm-to-violet gradient that reads as "Instagram" at a glance.
Deliberately *evocative* rather than a copy: Instagram's glyph is a trademark,
and the gradient carries the association on its own.

Everything is a hole rather than a stroke. Even-odd filling on one white path
means the gradient shows through the rules and the header split, so the shapes
can never misregister against the background, and there are no hairlines to
vanish at 16pt.

This is the *app* icon only. The menu bar item is a separate thing -- the
`calendar` SF Symbol, set as a template image in `app.py` so macOS tints it for
light and dark bars. A colour icon cannot go there.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys

import Quartz
from CoreFoundation import CFURLCreateWithFileSystemPath, kCFURLPOSIXPathStyle

CANVAS = 1024.0          # the size every coordinate below is expressed in

# macOS Big Sur grid: the art sits in an 824pt rounded square inside a 1024pt
# canvas. The padding is not optional -- it is the gap the Dock expects.
BODY = (100.0, 100.0, 824.0, 824.0)
BODY_RADIUS = 185.0

# Instagram's warm-to-violet ramp, drawn corner to corner.
STOPS = [
    (0.00, (0.996, 0.855, 0.459)),   # #FEDA75
    (0.25, (0.980, 0.494, 0.118)),   # #FA7E1E
    (0.50, (0.839, 0.161, 0.463)),   # #D62976
    (0.75, (0.588, 0.184, 0.749)),   # #962FBF
    (1.00, (0.310, 0.357, 0.835)),   # #4F5BD5
]

PAGE = (242.0, 215.0, 540.0, 530.0)  # the calendar page
PAGE_RADIUS = 62.0
HEADER_Y, HEADER_H = 635.0, 22.0     # gap between header band and date area
TAB_W, TAB_H, TAB_R = 62.0, 100.0, 31.0
TAB_Y, TAB_DX = 705.0, 132.0         # tabs straddle the page's top edge
CENTER_X = 512.0
ROW_COUNT = 4
ROW_W, ROW_H, ROW_GAP = 412.0, 23.0, 53.0    # the agenda rules
ROWS_CY = 424.0                      # centre of the stack, within the date area
FLASH = (694.0, 691.0, 27.0)         # x, y, r -- the viewfinder dot, in the header

# ICNS stores PNG representations as typed chunks. Writing the small container
# directly is more reliable than iconutil on current macOS, where iconutil can
# reject an iconset it just exported as "Invalid Iconset". These are Apple's
# standard PNG-backed size slots, from 16 through 1024 pixels.
ICNS_SLOTS = [
    (b"icp4", "icon_16x16.png"),
    (b"icp5", "icon_32x32.png"),
    (b"icp6", "icon_32x32@2x.png"),
    (b"ic07", "icon_128x128.png"),
    (b"ic08", "icon_256x256.png"),
    (b"ic09", "icon_512x512.png"),
    (b"ic10", "icon_512x512@2x.png"),
]


def _rounded(x, y, w, h, r):
    return Quartz.CGPathCreateWithRoundedRect(
        Quartz.CGRectMake(x, y, w, h), r, r, None)


def _draw(ctx, scale):
    Quartz.CGContextScaleCTM(ctx, scale, scale)

    # --- gradient body ---
    Quartz.CGContextSaveGState(ctx)
    Quartz.CGContextAddPath(ctx, _rounded(*BODY, BODY_RADIUS))
    Quartz.CGContextClip(ctx)

    space = Quartz.CGColorSpaceCreateDeviceRGB()
    comps, locs = [], []
    for loc, (r, g, b) in STOPS:
        comps += [r, g, b, 1.0]
        locs.append(loc)
    gradient = Quartz.CGGradientCreateWithColorComponents(space, comps, locs, len(locs))
    bx, by, bw, bh = BODY
    Quartz.CGContextDrawLinearGradient(
        ctx, gradient,
        Quartz.CGPointMake(bx, by),            # bottom-left, warm
        Quartz.CGPointMake(bx + bw, by + bh),  # top-right, violet
        0)
    Quartz.CGContextRestoreGState(ctx)

    # --- the white mark, as a single even-odd path ---
    # Subpaths alternate fill and hole, so the lens ring and every grid dot are
    # the gradient itself showing through rather than shapes drawn on top.
    path = Quartz.CGPathCreateMutable()

    for i in (-1, 1):
        Quartz.CGPathAddPath(path, None, _rounded(
            CENTER_X + i * TAB_DX - TAB_W / 2, TAB_Y, TAB_W, TAB_H, TAB_R))

    px, py, pw, ph = PAGE
    Quartz.CGPathAddPath(path, None, _rounded(px, py, pw, ph, PAGE_RADIUS))
    # Splits the header band off from the date area.
    Quartz.CGPathAddRect(path, None, Quartz.CGRectMake(px, HEADER_Y, pw, HEADER_H))

    fx, fy, fr = FLASH
    Quartz.CGPathAddEllipseInRect(path, None,
                                  Quartz.CGRectMake(fx - fr, fy - fr, fr * 2, fr * 2))

    # Ruled lines through the date area, evenly spaced and equal width: varying
    # the lengths reads as a paragraph of text, which is a note-taking app, not a
    # calendar. The stack stays centred on ROWS_CY whether the count is odd or
    # even.
    for i in range(ROW_COUNT):
        cy = ROWS_CY + (i - (ROW_COUNT - 1) / 2) * (ROW_H + ROW_GAP)
        Quartz.CGPathAddPath(path, None, _rounded(
            CENTER_X - ROW_W / 2, cy - ROW_H / 2, ROW_W, ROW_H, ROW_H / 2))

    Quartz.CGContextAddPath(ctx, path)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextEOFillPath(ctx)


def render(size: int, out: str) -> None:
    ctx = Quartz.CGBitmapContextCreate(
        None, size, size, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetAllowsAntialiasing(ctx, True)
    Quartz.CGContextSetShouldAntialias(ctx, True)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)

    _draw(ctx, size / CANVAS)

    image = Quartz.CGBitmapContextCreateImage(ctx)
    url = CFURLCreateWithFileSystemPath(None, out, kCFURLPOSIXPathStyle, False)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, image, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise SystemExit(f"could not write {out}")


def compile_icns(iconset: str, output: str) -> None:
    chunks = []
    for kind, name in ICNS_SLOTS:
        with open(os.path.join(iconset, name), "rb") as fh:
            data = fh.read()
        chunks.append(kind + struct.pack(">I", len(data) + 8) + data)
    payload = b"".join(chunks)
    temp = output + ".tmp"
    with open(temp, "wb") as fh:
        fh.write(b"icns" + struct.pack(">I", len(payload) + 8) + payload)
    os.replace(temp, output)


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    iconset = os.path.join(root, "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)

    # Every size drawn from the geometry above at its true pixel count. Scaling
    # one 1024 master down instead is what makes the 16pt icon look furry.
    for pt in (16, 32, 128, 256, 512):
        render(pt, os.path.join(iconset, f"icon_{pt}x{pt}.png"))
        render(pt * 2, os.path.join(iconset, f"icon_{pt}x{pt}@2x.png"))

    icns = os.path.join(root, "AppIcon.icns")
    compile_icns(iconset, icns)
    shutil.rmtree(iconset, ignore_errors=True)
    print(f"==> {icns} ({os.path.getsize(icns) // 1024}KB)")


if __name__ == "__main__":
    sys.exit(main())
