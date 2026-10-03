"""Build app icons from official MiSTer logo_small.png (MiSTer-kun crop)."""
from __future__ import annotations

import struct
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
MASTER = ASSETS / "mister_kun_master.png"
LOGO_URL = (
    "https://raw.githubusercontent.com/MiSTer-devel/MkDocs_MiSTer/"
    "main/docs/assets/logo_small.png"
)
# Cat ends before the transparent gap before "MiSTer" text (~x=208 on 779-wide asset).
CAT_RIGHT = 208
PAD_RATIO = 0.16
BADGE_COLOR = (36, 36, 40, 255)
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def _save_ico_png(path: Path, images: list[Image.Image]) -> None:
    entries: list[tuple[int, int, int, int]] = []
    blobs: list[bytes] = []
    offset = 6 + 16 * len(images)
    for im in images:
        bio = BytesIO()
        im.save(bio, format="PNG")
        data = bio.getvalue()
        w, h = im.size
        entries.append((0 if w >= 256 else w, 0 if h >= 256 else h, len(data), offset))
        blobs.append(data)
        offset += len(data)
    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(images))
    for w, h, size, off in entries:
        out += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, size, off)
    for blob in blobs:
        out += blob
    path.write_bytes(out)


def _kill_light_fringe(im: Image.Image, light: int = 150, alpha_cut: int = 40) -> Image.Image:
    w, h = im.size
    px = im.load()
    alpha = [[px[x, y][3] for x in range(w)] for y in range(h)]
    out = im.copy()
    op = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if a < alpha_cut:
                op[x, y] = (0, 0, 0, 0)
                continue
            if min(r, g, b) >= light:
                near_t = False
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        xx, yy = x + dx, y + dy
                        if not (0 <= xx < w and 0 <= yy < h) or alpha[yy][xx] < alpha_cut:
                            near_t = True
                if near_t:
                    op[x, y] = (0, 0, 0, 0)
    return out


def _harden_outline(im: Image.Image) -> Image.Image:
    """Make near-black outline pixels solid; snap weak alpha away."""
    out = im.copy()
    px = out.load()
    w, h = out.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if a < 96:
                px[x, y] = (0, 0, 0, 0)
                continue
            if max(r, g, b) < 50:
                px[x, y] = (0, 0, 0, 255)
            elif a < 255:
                px[x, y] = (r, g, b, 255)
    return out


def _drop_light_on_dark(im: Image.Image, light: int = 175) -> Image.Image:
    """On a dark badge, white cutout-halos become holes (badge shows through)."""
    out = im.copy()
    px = out.load()
    w, h = out.size
    alpha = [[px[x, y][3] for x in range(w)] for y in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if min(r, g, b) < light:
                continue
            # Keep solid interior whites (body); drop only edge/halo whites.
            near_clear = False
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    xx, yy = x + dx, y + dy
                    if not (0 <= xx < w and 0 <= yy < h) or alpha[yy][xx] < 40:
                        near_clear = True
            if near_clear:
                px[x, y] = (0, 0, 0, 0)
    return out


def _extract_cat(logo: Image.Image) -> Image.Image:
    cat = logo.crop((0, 0, min(CAT_RIGHT, logo.width), logo.height)).convert("RGBA")
    cat = _kill_light_fringe(cat, light=160, alpha_cut=50)
    cat = _kill_light_fringe(cat, light=130, alpha_cut=40)
    cat = _harden_outline(cat)
    bbox = cat.split()[-1].point(lambda v: 255 if v > 10 else 0).getbbox()
    if bbox:
        cat = cat.crop(bbox)
    return cat


def _ensure_master() -> Image.Image:
    ASSETS.mkdir(exist_ok=True)
    if MASTER.is_file() and MASTER.stat().st_size > 1000:
        return Image.open(MASTER).convert("RGBA")
    print(f"downloading {LOGO_URL}")
    with urllib.request.urlopen(LOGO_URL) as resp:
        logo = Image.open(BytesIO(resp.read())).convert("RGBA")
    cat = _extract_cat(logo)
    cat.save(MASTER)
    return cat


def make_icon(mascot: Image.Image, size: int, *, badge: bool = True) -> Image.Image:
    # Draw at 4x then downscale so the circle/edges are anti-aliased.
    ss = 4
    big = size * ss
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    if badge:
        m = max(1, int(round(big * 0.03)))
        draw.ellipse([m, m, big - 1 - m, big - 1 - m], fill=BADGE_COLOR)

    pad = PAD_RATIO + (0.04 if badge else 0.0)
    inner = max(1, int(round(big * (1 - 2 * pad))))
    mw, mh = mascot.size
    scale = min(inner / mw, inner / mh)
    nw = max(1, int(round(mw * scale)))
    nh = max(1, int(round(mh * scale)))
    resized = mascot.resize((nw, nh), Image.Resampling.LANCZOS)
    resized = _kill_light_fringe(resized, light=145, alpha_cut=30)
    resized = _harden_outline(resized)
    if badge:
        resized = _drop_light_on_dark(resized, light=170)
    canvas.paste(resized, ((big - nw) // 2, (big - nh) // 2), resized)
    if size == big:
        return canvas
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    # Always refresh master from official logo when regenerating
    print(f"downloading {LOGO_URL}")
    with urllib.request.urlopen(LOGO_URL) as resp:
        logo = Image.open(BytesIO(resp.read())).convert("RGBA")
    cat = _extract_cat(logo)
    cat.save(MASTER)
    print(f"wrote {MASTER} {cat.size}")

    for name, size in (
        ("mister_favicon.png", 32),
        ("mister_32.png", 32),
        ("mister_48.png", 48),
    ):
        out = ASSETS / name
        make_icon(cat, size, badge=True).save(out)
        print(f"wrote {out}")

    frames = [make_icon(cat, s, badge=True) for s in ICO_SIZES]
    ico = ASSETS / "mister.ico"
    _save_ico_png(ico, frames)
    print(f"wrote {ico} ({ico.stat().st_size} bytes)")

    (ASSETS / "SOURCE.txt").write_text(
        "MiSTer-kun cropped from official logo_small.png (MkDocs_MiSTer).\n"
        f"{LOGO_URL}\n"
        "Art: hewhoisred (MiSTer-kun) / Conrad Fenech (logo).\n"
        "App icons: fringe cleanup + ~16% padding on a dark circular badge.\n"
        "Regen: py -3 _gen_official_icons.py\n",
        encoding="utf-8",
    )
    print("wrote assets/SOURCE.txt")


if __name__ == "__main__":
    main()
