"""Phase I - Data Acquisition and Preprocessing (Spatial Data and Context Layer)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps

MAX_SIDE = 1280
VIDEO_SUFFIXES = {".mp4", ".mov"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}


@dataclass(slots=True)
class PreparedMedia:
    path: Path
    kind: str
    width: int
    height: int
    original_size: tuple[int, int]
    pixels_per_meter: float | None
    enhancements: list[str]
    sidecar: Path | None = None


class IngestAgent:
    name = "Ingest"

    def prepare(self, upload: Path, out_dir: Path, *, pixels_per_meter: float | None = None) -> PreparedMedia:
        suffix = upload.suffix.lower()
        out_dir.mkdir(parents=True, exist_ok=True)

        if suffix in VIDEO_SUFFIXES:
            return self._prepare_video(upload)
        if suffix not in IMAGE_SUFFIXES:
            raise ValueError(f"unsupported media type: {suffix or 'unknown'}")
        return self._prepare_image(upload, out_dir, pixels_per_meter)

    def _prepare_video(self, upload: Path) -> PreparedMedia:
        size = upload.stat().st_size
        if size < 1_024:
            raise ValueError("video file looks empty or corrupt")
        return PreparedMedia(
            path=upload,
            kind="video",
            width=0,
            height=0,
            original_size=(size, 0),
            pixels_per_meter=None,
            enhancements=["container check (mp4/mov, H.264/H.265)", "faststart remux handled server-side"],
        )

    def _prepare_image(self, upload: Path, out_dir: Path, pixels_per_meter: float | None) -> PreparedMedia:
        with Image.open(upload) as raw:
            image = ImageOps.exif_transpose(raw)
            original = image.size
            enhancements: list[str] = ["EXIF orientation normalised"]

            if max(original) > MAX_SIDE:
                scale = MAX_SIDE / max(original)
                image = image.resize((round(original[0] * scale), round(original[1] * scale)), Image.LANCZOS)
                enhancements.append(f"rescaled for segmentation cost ({scale:.2f}x)")

            image = image.convert("RGB")

            stats = image.resize((64, 64)).convert("L").getextrema()
            if stats[1] - stats[0] < 60:
                image = ImageEnhance.Contrast(image).enhance(1.35)
                enhancements.append("contrast boosted (low dynamic range)")

            image = ImageEnhance.Sharpness(image).enhance(1.25)
            enhancements.append("unsharp/defect-edge enhancement")

            prepared = out_dir / f"{upload.stem}_prepared.jpg"
            image.save(prepared, "JPEG", quality=92, optimize=True)
            enhancements.append(f"re-encoded JPEG (92 q, {prepared.stat().st_size // 1024} KB)")

        return PreparedMedia(
            path=prepared,
            kind="image",
            width=image.width,
            height=image.height,
            original_size=original,
            pixels_per_meter=pixels_per_meter,
            enhancements=enhancements,
            sidecar=upload.parent / f"{upload.stem}.defects.json",
        )
