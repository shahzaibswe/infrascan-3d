"""Generate synthetic inspection samples with ground-truth defect sidecars.

Offline demos need overlays that land on something visible, so the bundled samples
are drawn here and their true defect regions are written next to the image.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

from infrascan.mask_codec import encode_one_bit_mask

SAMPLES = {
    "road_surface": {
        "base": (96, 92, 88),
        "defects": [
            ("asphalt crack", "crack"),
            ("asphalt crack", "crack"),
            ("road pothole", "pothole"),
            ("road spalling", "patch"),
        ],
    },
    "pipeline_segment": {
        "base": (88, 96, 104),
        "defects": [
            ("pipe corrosion", "rust"),
            ("pipe corrosion", "rust"),
            ("surface crack", "crack"),
            ("paint peeling", "patch"),
        ],
    },
    "tower_lattice": {
        "base": (120, 124, 128),
        "defects": [
            ("rusted tower lattice", "rust"),
            ("corroded metal joint", "rust"),
            ("cracked concrete foundation", "crack"),
        ],
    },
}


def _texture(draw: ImageDraw.ImageDraw, size: tuple[int, int], base: tuple[int, int], rng: random.Random) -> None:
    width, height = size
    for _ in range(2400):
        shade = base[0] + rng.randint(-14, 14), base[1] + rng.randint(-14, 14), base[2] + rng.randint(-14, 14)
        x, y = rng.randrange(width), rng.randrange(height)
        draw.point((x, y), fill=shade)
    for step in range(0, width, 160):
        draw.line([(step, 0), (step, height)], fill=tuple(max(0, c - 10) for c in base), width=1)


def _crack(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], rng: random.Random) -> None:
    x1, y1, x2, y2 = box
    points = [(x1, y1)]
    steps = max(4, (x2 - x1) // 14)
    for index in range(1, steps + 1):
        t = index / steps
        points.append((x1 + (x2 - x1) * t + rng.randint(-4, 4), y1 + (y2 - y1) * t + rng.randint(-9, 9)))
    draw.line(points, fill=(28, 26, 24), width=max(2, (y2 - y1) // 9))
    for point in points[::2]:
        branch = (point[0] + rng.randint(-18, 18), point[1] + rng.randint(-18, 18))
        draw.line([point, branch], fill=(38, 35, 32), width=1)


def _pothole(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], rng: random.Random) -> None:
    x1, y1, x2, y2 = box
    lobes = [(x1 + (x2 - x1) * 0.5 + (x2 - x1) * 0.34 * math.cos(a * 2 * math.pi / 12),
              y1 + (y2 - y1) * 0.5 + (y2 - y1) * 0.34 * math.sin(a * 2 * math.pi / 12)) for a in range(12)]
    draw.polygon(lobes, fill=(44, 40, 36), outline=(20, 18, 16))
    for _ in range(90):
        point = (rng.uniform(x1, x2), rng.uniform(y1, y2))
        draw.point(point, fill=(62, 56, 50))


def _rust(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], rng: random.Random) -> None:
    x1, y1, x2, y2 = box
    for _ in range(340):
        cx, cy = rng.uniform(x1, x2), rng.uniform(y1, y2)
        radius = rng.randint(2, 8)
        tone = (128 + rng.randint(0, 60), 62 + rng.randint(0, 34), 22 + rng.randint(0, 22))
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=tone)
    draw.line([(x1 + 4, y1), (x1 + 10, y2 + 18)], fill=(118, 58, 22), width=3)


def _patch(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], rng: random.Random) -> None:
    x1, y1, x2, y2 = box
    draw.rectangle([x1, y1, x2, y2], fill=(152, 148, 140), outline=(96, 92, 86))
    for _ in range(120):
        draw.point((rng.uniform(x1, x2), rng.uniform(y1, y2)), fill=(132, 128, 122))


SHAPES = {"crack": _crack, "pothole": _pothole, "rust": _rust, "patch": _patch}


def _mask_for(shape: str, box: tuple[int, int, int, int], rng: random.Random) -> list[int]:
    x1, y1, x2, y2 = box
    width, height = x2 - x1, y2 - y1
    raster = []
    for y in range(height):
        for x in range(width):
            nx, ny = (x + 0.5) / width - 0.5, (y + 0.5) / height - 0.5
            if shape == "crack":
                inside = abs(ny - nx * 0.6) < 0.16
            elif shape == "pothole":
                inside = nx * nx + ny * ny < 0.2
            elif shape == "rust":
                inside = nx * nx * 1.4 + ny * ny * 2.2 < 0.22
            else:
                inside = abs(nx) < 0.42 and abs(ny) < 0.4
            raster.append(1 if inside else 0)
    return raster


def build(destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    size = (1024, 768)
    for name, spec in SAMPLES.items():
        rng = random.Random(abs(hash(name)) % 9973)
        image = Image.new("RGB", size, spec["base"])
        draw = ImageDraw.Draw(image)
        _texture(draw, size, spec["base"], rng)

        truth = []
        columns = 2
        for index, (concept, shape) in enumerate(spec["defects"]):
            cell_w, cell_h = size[0] // columns, size[1] // ((len(spec["defects"]) + 1) // columns)
            width = rng.randint(cell_w // 3, cell_w // 2)
            height = rng.randint(cell_h // 4, cell_h // 2)
            col, row = index % columns, index // columns
            left = col * cell_w + rng.randint(20, max(21, cell_w - width - 20))
            top = row * cell_h + rng.randint(20, max(21, cell_h - height - 20))
            box = (left, top, min(left + width, size[0] - 8), min(top + height, size[1] - 8))
            SHAPES[shape](draw, box, rng)
            mask = _mask_for(shape, box, rng)
            truth.append(
                {
                    "concept": concept,
                    "object_id": index,
                    "box": list(box),
                    "mask_height": box[3] - box[2],
                    "mask_width": box[2] - box[0],
                    "mask": encode_one_bit_mask(mask, box[2] - box[0], box[3] - box[1]),
                    "confidence": round(rng.uniform(0.72, 0.96), 6),
                }
            )

        image_path = destination / f"{name}.jpg"
        image.save(image_path, "JPEG", quality=88)
        (destination / f"{name}.defects.json").write_text(json.dumps({"size": list(size), "defects": truth}, indent=1), encoding="utf-8")
        written.append(image_path.name)
    return written


if __name__ == "__main__":
    target = Path(__file__).resolve().parent
    for name in build(target):
        print("generated", name)
