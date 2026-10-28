"""Simulated SAM 3.1 output for runs without a ``MODEL_API_KEY``.

Produces text in the exact wire grammar and encodes masks with the real one_bit
codec, so downstream parsing, decoding and overlay rendering are identical to a
live response. Nothing here pretends to be model output: every scan records
``engine=simulated`` in its audit trail.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from .mask_codec import encode_one_bit_mask

SYNONYMS = {
    "asphalt crack": {"surface crack", "asphalt crack", "concrete crack"},
    "surface crack": {"asphalt crack", "concrete crack", "facade crack", "cracked concrete foundation"},
    "pipe corrosion": {"corroded metal joint", "rusted tower lattice"},
    "rusted tower lattice": {"corroded metal joint", "pipe corrosion"},
    "corroded metal joint": {"pipe corrosion", "steel corrosion", "rusted tower lattice"},
    "road pothole": {"road spalling", "spalled concrete"},
}


def _rng(seed_text: str) -> random.Random:
    return random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16))


def _raster(shape: str, width: int, height: int) -> list[int]:
    pixels: list[int] = []
    for y in range(height):
        for x in range(width):
            nx, ny = (x + 0.5) / width - 0.5, (y + 0.5) / height - 0.5
            if shape == "crack":
                inside = abs(ny - nx * 0.7) < 0.15 and abs(nx) < 0.48
            elif shape == "hole":
                inside = nx * nx + ny * ny < 0.19
            elif shape == "patch":
                inside = abs(nx) < 0.4 and abs(ny) < 0.36
            else:
                inside = nx * nx * 1.3 + ny * ny * 2.0 < 0.2
            pixels.append(1 if inside else 0)
    return pixels


def _shape_for(concept: str) -> str:
    text = concept.lower()
    if "crack" in text:
        return "crack"
    if "pothole" in text:
        return "hole"
    if "peel" in text or "stain" in text or "panel" in text:
        return "patch"
    return "blob"


def _record(object_id: int, box: tuple[int, int, int, int], width: int, height: int, mask: str, confidence: float) -> str:
    left, top, right, bottom = box
    return (
        f"{object_id}<|box;x1={left};y1={top};x2={right};y2={bottom};w={width};h={height};c={confidence:.6f}|>"
        f"<|mask;x=0;y=0;c={confidence:.6f};data={bottom - top},{right - left},{mask}|>"
    )


def load_ground_truth(sidecar: Path | None) -> list[dict[str, Any]]:
    if sidecar is None or not sidecar.exists():
        return []
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return list(data.get("defects", []))


def _matches(prompt: str, truth_concept: str) -> bool:
    """Tolerate plural forms and the documented synonym families when replaying fixtures."""
    left, right = prompt.lower().strip(), truth_concept.lower().strip()
    if left == right or left.rstrip("s") == right.rstrip("s"):
        return True
    family = {item.lower() for item in SYNONYMS.get(truth_concept, set())}
    return left in family or left.rstrip("s") in {item.rstrip("s") for item in family}


def simulate_lane(concept: str, *, width: int, height: int, seed_text: str,
                frame_index: int = 0, ground_truth: list[dict[str, Any]] | None = None) -> str:
    """Return one output-text line for a single concept prompt."""
    rng = _rng(seed_text + concept)
    records: list[str] = []
    matched = 0

    for truth in ground_truth or []:
        if not _matches(concept, str(truth["concept"])):
            continue
        left, top, right, bottom = (int(value) for value in truth["box"])
        mask = str(truth["mask"])
        confidence = float(truth.get("confidence", 0.8)) * rng.uniform(0.93, 1.0)
        records.append(_record(matched, (left, top, right, bottom), width, height, mask,
                               round(min(confidence, 0.99), 6)))
        matched += 1

    if records:
        return f"<{frame_index}f>" + ",".join(records)

    count = rng.choice([0, 1, 1, 2])
    shape = _shape_for(concept)
    for index in range(count):
        box_w = rng.randint(max(40, width // 12), max(70, width // 5))
        box_h = rng.randint(max(28, height // 14), max(48, height // 6))
        left = rng.randint(0, max(1, width - box_w))
        top = rng.randint(0, max(1, height - box_h))
        # One region per concept, drifting slightly each frame: the same defect stays the same
        # defect across a clip so cross-frame merging has something real to merge. The step is
        # kept small because tracks are anchored on their clearest frame, not the previous one.
        left = max(0, min(width - box_w, left + frame_index * max(1, width // 200)))
        top = max(0, min(height - box_h, top + frame_index * max(1, height // 200)))
        raster = _raster(shape, box_w, box_h)
        mask = encode_one_bit_mask(raster, box_w, box_h)
        confidence = round(rng.uniform(0.41, 0.9), 6)
        records.append(_record(index, (left, top, left + box_w, top + box_h), width, height, mask, confidence))
    return f"<{frame_index}f>" + ",".join(records) if records else ""
