"""Server-side media handling: validation, storage layout, frame intake and retention."""
from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from PIL import Image

from .config import BASE_DIR, settings
from .errors import InfraScanError

UPLOAD_DIR = BASE_DIR / "uploads"
PREPARED_DIR = BASE_DIR / "prepared"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov"}
# GLB/GLTF/OBJ/PLY are named in the product brief but no 3D parser or viewer is installed here,
# so they are refused with an explanation rather than accepted into a broken viewer.
UNSUPPORTED_EXPLAIN = {
    ".glb": "glTF binary", ".gltf": "glTF", ".obj": "Wavefront OBJ", ".ply": "PLY point cloud",
}

_MAGIC = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
    b"II*\x00": ".tif",
    b"MM\x00*": ".tif",
    b"BM": ".bmp",
    b"RIFF": ".webp",
}
MAX_MAGIC_BYTES = 12


def inspection_dir(inspection_id: str) -> Path:
    path = UPLOAD_DIR / inspection_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepared_dir(inspection_id: str) -> Path:
    path = PREPARED_DIR / inspection_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in UNSUPPORTED_EXPLAIN:
        raise InfraScanError(
            "unsupported_input",
            f"{UNSUPPORTED_EXPLAIN[suffix]} assets are not supported by this build: no 3D parser or "
            "viewer is installed, so an uploaded model would render as a broken preview. "
            "Inspect a photograph of the asset instead.",
            status=415,
            detail={"suffix": suffix, "supported": sorted(IMAGE_SUFFIXES | VIDEO_SUFFIXES)},
        )
    if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
        raise InfraScanError(
            "unsupported_input",
            f"unsupported file type {suffix or '(none)'}; accepted: "
            f"{', '.join(sorted(IMAGE_SUFFIXES | VIDEO_SUFFIXES))}",
            status=415,
            detail={"suffix": suffix},
        )
    return suffix


def _looks_like_media(path: Path) -> str:
    """Trust the bytes, not the filename: an .jpg that is really HTML must not reach the model."""
    head = path.read_bytes()[:MAX_MAGIC_BYTES]
    for magic, kind in _MAGIC.items():
        if head.startswith(magic):
            return kind
    if head[4:8] in (b"ftyp",):
        return ".mp4"
    return ""


@dataclass(slots=True)
class StoredMedia:
    path: Path
    kind: str
    filename: str
    width: int = 0
    height: int = 0


def _write_part(part: UploadFile, target: Path, budget: int) -> int:
    """Stream one multipart part to disk, refusing to exceed the shared byte budget."""
    written = 0
    with target.open("wb") as handle:
        while chunk := part.file.read(1 << 20):
            written += len(chunk)
            if written > budget:
                target.unlink(missing_ok=True)
                raise InfraScanError(
                    "too_large",
                    f"upload exceeds the {settings.max_upload_mb} MB limit",
                    status=413,
                    detail={"bytes": written, "limit_bytes": settings.max_upload_mb * 1024 * 1024},
                )
            handle.write(chunk)
    return written


def save_upload(part: UploadFile, dest_dir: Path) -> StoredMedia:
    """Validate and persist a single uploaded image or clip."""
    suffix = _safe_suffix(part.filename or "")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"raw_{uuid.uuid4().hex}{suffix}"

    written = _write_part(part, target, settings.max_upload_mb * 1024 * 1024)
    if written == 0:
        target.unlink(missing_ok=True)
        raise InfraScanError("empty_file", "uploaded file is empty", status=422)

    sniffed = _looks_like_media(target)
    if suffix in IMAGE_SUFFIXES and sniffed not in IMAGE_SUFFIXES:
        target.unlink(missing_ok=True)
        raise InfraScanError("invalid_media", "the file is not a decodable image", status=422)
    if suffix in VIDEO_SUFFIXES and sniffed != ".mp4":
        target.unlink(missing_ok=True)
        raise InfraScanError("invalid_media", "video must be an MP4/MOV container (H.264/H.265)", status=422)

    if sniffed in IMAGE_SUFFIXES:
        with Image.open(target) as image:
            return StoredMedia(path=target, kind="image", filename=part.filename or target.name,
                               width=image.width, height=image.height)
    return StoredMedia(path=target, kind="video", filename=part.filename or target.name)


@dataclass(slots=True)
class StoredFrame:
    frame_index: int
    source_time_ms: int
    path: Path
    width: int
    height: int


def save_frames(dest_dir: Path, parts: list[UploadFile], times: list[int]) -> list[StoredFrame]:
    """Persist browser-extracted video frames; each is validated as a real image."""
    if len(parts) > settings.max_frames:
        raise InfraScanError(
            "too_many_frames",
            f"at most {settings.max_frames} frames per inspection (video is billed per frame)",
            status=422,
            detail={"max_frames": settings.max_frames, "received": len(parts)},
        )

    stored: list[StoredFrame] = []
    for position, part in enumerate(parts):
        target = dest_dir / f"frame_{position:03d}.jpg"
        if _write_part(part, target, settings.max_upload_mb * 1024 * 1024) == 0:
            raise InfraScanError("empty_file", f"frame {position} is empty", status=422)
        if _looks_like_media(target) not in IMAGE_SUFFIXES:
            target.unlink(missing_ok=True)
            raise InfraScanError("invalid_media", f"frame {position} is not a decodable image", status=422)
        with Image.open(target) as image:
            stored.append(StoredFrame(
                frame_index=position,
                source_time_ms=times[position] if position < len(times) else 0,
                path=target,
                width=image.width,
                height=image.height,
            ))
    return stored


def sidecar_for(path: Path) -> Path | None:
    """Ground-truth fixture shipped next to a bundled sample, if one exists."""
    candidate = path.parent / f"{path.stem}.defects.json"
    return candidate if candidate.exists() else None


def resolve_sample(stem: str) -> Path:
    from .config import ROOT

    samples = ROOT / "samples"
    wanted = Path(stem).stem.lower()
    for candidate in sorted(samples.glob("*")):
        if candidate.suffix.lower() in IMAGE_SUFFIXES and wanted in candidate.stem.lower():
            return candidate
    available = sorted(p.stem for p in samples.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    raise InfraScanError("not_found", f"no sample matching {stem!r}; available: {available}", status=404,
                         detail={"available": available})


def purge_inspection(inspection_id: str) -> None:
    shutil.rmtree(UPLOAD_DIR / inspection_id, ignore_errors=True)
    shutil.rmtree(PREPARED_DIR / inspection_id, ignore_errors=True)


def purge_older_than(days: float) -> int:
    """Retention control for temporary media. Returns the number of directories removed."""
    cutoff = time.time() - days * 86_400
    removed = 0
    for root in (UPLOAD_DIR, PREPARED_DIR):
        if not root.exists():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    return removed
