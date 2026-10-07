"""Rasterize assets/icon.svg into png, ico, and icns files."""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "icon.svg"
ASSETS = ROOT / "assets"

# PNG-encoded icns types. Sizes are the pixel dimensions of each PNG.
ICNS_TYPES = {
    "ic12": 32,
    "ic11": 64,
    "ic07": 128,
    "ic08": 256,
    "ic09": 512,
    "ic13": 512,
    "ic10": 1024,
    "ic14": 1024,
}


def render(size: int, dest: Path) -> None:
    subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(dest), str(SVG)],
        check=True,
    )


def pack_icns(pngs: list[tuple[str, bytes]]) -> bytes:
    chunks = []
    for ostype, blob in pngs:
        chunks.append(ostype.encode("ascii") + struct.pack(">I", 8 + len(blob)) + blob)
    body = b"".join(chunks)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    render(1024, ASSETS / "icon.png")
    render(1024, ASSETS / "icon-1024.png")
    render(512, ASSETS / "icon-512.png")
    image = Image.open(ASSETS / "icon.png").convert("RGBA")
    image.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    encoded: list[tuple[str, bytes]] = []
    for ostype, size in ICNS_TYPES.items():
        dest = ASSETS / f".icon-{size}.png"
        render(size, dest)
        encoded.append((ostype, dest.read_bytes()))
        dest.unlink()
    (ASSETS / "icon.icns").write_bytes(pack_icns(encoded))
    print("Wrote icon png, ico, and icns.")


if __name__ == "__main__":
    main()
