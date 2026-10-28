"""Parser for the SAM 3.1 special-token segmentation grammar.

Wire format (one line per frame, in the Responses ``output_text`` lane)::

    <0f>0<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..;c=..|><|mask;x=0;y=0;data=<H>,<W>,<enc>payload|>,1<|box;...|><|mask;...|>
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, cast

from meta_sam_parser import (
    SegmentationMask,
    decode_mask_to_raster,
    decode_mask_to_svg_path,
)

MaskEncoding = Literal["lossless", "one_bit"]

_FRAME_RE = re.compile(r"^<(?P<index>\d+)f>")
_RECORD_RE = re.compile(
    r"(?P<object_id>\d+)"
    r"<\|box;(?P<box>[^|]*)\|>"
    r"<\|mask;(?P<mask>[^|]*)\|>"
)


def _fields(blob: str) -> dict[str, str]:
    """Split a ``key=value;key=value`` token body; values may contain commas."""
    fields: dict[str, str] = {}
    for part in blob.split(";"):
        key, separator, value = part.partition("=")
        if separator:
            fields[key] = value
    return fields


@dataclass(slots=True)
class Detection:
    """One box/mask pair for one object on one frame."""

    object_id: str
    frame_index: int
    left: int
    top: int
    right: int
    bottom: int
    source_width: int
    source_height: int
    confidence: float | None
    mask_width: int
    mask_height: int
    mask_payload: str
    concept: str = ""
    scan_id: str = ""
    _raster: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def mask_encoding(self) -> MaskEncoding:
        return cast("MaskEncoding", "lossless" if self.mask_payload.startswith("~") else "one_bit")

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    @property
    def area_px(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)

    @property
    def mask_fill(self) -> float:
        """Fraction of the bounding box covered by the mask (0-1)."""
        raster = self.pixels()
        if not raster or self.mask_width <= 0 or self.mask_height <= 0:
            return 0.0
        return sum(raster) / (self.mask_width * self.mask_height)

    def pixels(self) -> bytes:
        if self._raster is None:
            self._raster = decode_mask_to_raster(self._mask())
        return self._raster

    def svg_path(self) -> str:
        """Polygon path in mask-local pixel coordinates (origin at box top-left)."""
        return decode_mask_to_svg_path(self._mask())

    def _mask(self) -> SegmentationMask:
        return SegmentationMask(
            encoding=self.mask_encoding,
            payload=self.mask_payload,
            width=self.mask_width,
            height=self.mask_height,
        )


@dataclass(slots=True)
class ConceptResult:
    """Segmentation output for a single concept prompt."""

    concept: str
    detections: list[Detection] = field(default_factory=list)
    raw_output: str = ""
    error: str | None = None

    @property
    def matched(self) -> bool:
        return bool(self.detections)


def parse_segmentation_text(text: str, *, concept: str = "", scan_id: str = "",
                            frame_index: int | None = None) -> ConceptResult:
    """Parse a complete ``output_text`` lane into detections.

    Zero matches is a valid outcome and yields an empty detection list. ``frame_index``
    overrides the ``<Nf>`` prefix when the caller knows which frame the lane belongs to.
    """
    result = ConceptResult(concept=concept, raw_output=text or "")
    if not text or not text.strip():
        return result

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        frame_match = _FRAME_RE.match(line)
        if not frame_match:
            continue
        frame_index = frame_index if frame_index is not None else int(frame_match.group("index"))
        for record in _RECORD_RE.finditer(line):
            box = _fields(record.group("box"))
            mask = _fields(record.group("mask"))
            if "x1" not in box or "data" not in mask:
                continue
            payload = mask["data"]
            height_str, _, rest = payload.partition(",")
            width_str, _, encoded = rest.partition(",")
            if not height_str.isdigit() or not width_str.isdigit() or not encoded:
                continue
            result.detections.append(
                Detection(
                    object_id=record.group("object_id"),
                    frame_index=frame_index,
                    left=int(float(box["x1"])),
                    top=int(float(box["y1"])),
                    right=int(float(box["x2"])),
                    bottom=int(float(box["y2"])),
                    source_width=int(float(box.get("w", 0))),
                    source_height=int(float(box.get("h", 0))),
                    confidence=float(box["c"]) if "c" in box else None,
                    mask_width=int(width_str),
                    mask_height=int(height_str),
                    mask_payload=encoded,
                    concept=concept,
                    scan_id=scan_id,
                )
            )
    return result


def iou(a: Detection, b: Detection) -> float:
    ax1, ay1, ax2, ay2 = a.box
    bx1, by1, bx2, by2 = b.box
    inter = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
    union = a.area_px + b.area_px - inter
    return inter / union if union > 0 else 0.0
