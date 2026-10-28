"""Mask payload helpers.

Reuses the same range coder the SAM 3.1 wire format requires so simulated masks
decode through the identical path as live ones.
"""
from __future__ import annotations

from collections.abc import Sequence

from meta_sam_parser._mask_codec import _encode_raster


def encode_one_bit_mask(raster: Sequence[int], width: int, height: int) -> str:
    """Encode a row-major 0/1 raster as a canonical ``!`` (one_bit) wire payload."""
    if width <= 0 or height <= 0 or len(raster) != width * height:
        raise ValueError("raster length must equal width * height")
    return _encode_raster([int(value) & 1 for value in raster], width, height)
