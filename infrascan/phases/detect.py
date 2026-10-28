"""Phase II - Defect Identification and Segmentation (Vision & Detection Agent).

One SAM 3.1 request per concept prompt, exactly as documented (a short noun phrase in
``input_text``, the media in ``input_image``/``input_video``; never a multi-concept list).
Results are parsed by the wire grammar in :mod:`infrascan.sam_grammar`, cross-validated by
IoU inside a frame, then merged across frames so one persistent defect stays one defect.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ..config import settings
from ..meta_client import client, file_to_data_url
from ..sam_grammar import Detection, iou, parse_segmentation_text
from ..simulation import simulate_lane

CONCEPTS_BY_ASSET: dict[str, list[str]] = {
    "power_grid": ["rusted tower lattice", "cracked concrete foundation", "corroded metal joint"],
    "pipeline": ["pipe corrosion", "surface crack", "paint peeling"],
    "road_network": ["asphalt crack", "road pothole", "road spalling"],
    "bridge": ["concrete crack", "steel corrosion", "spalled concrete"],
    "facade": ["facade crack", "stained surface", "missing panel"],
}
DEFAULT_CONCEPTS = ["surface crack", "corrosion patch", "material spalling"]
MIN_CONFIDENCE = 0.35
MERGE_IOU = 0.30
TRACK_IOU = 0.20
MAX_PROMPT_CHARS = 64

# SAM 3.1 matches noun phrases; the docs state that commands and questions are unreliable.
_COMMAND_RE = re.compile(
    r"^\s*(?:please\s+)?(?:detect|identify|find|locate|inspect|segment|look for|count|show)\s+"
    r"(?:the\s+|any\s+|visible\s+|all\s+)*",
    re.IGNORECASE,
)
_SUFFIX_RE = re.compile(r"\s*(?:in|on|of)\s+(?:the\s+)?[\w\s/,-]+[?.!]?\s*$")


def concepts_for(asset_type: str) -> list[str]:
    return CONCEPTS_BY_ASSET.get(asset_type, DEFAULT_CONCEPTS)


def normalise_prompt(text: str) -> tuple[str, str | None]:
    """Turn a user instruction into a documented noun phrase; return (concept, warning)."""
    raw = re.sub(r"\s+", " ", (text or "").strip()).strip(" .!?,")
    if not raw:
        return "", "empty prompt ignored"
    warning = None
    command = _COMMAND_RE.match(raw)
    if command:
        stripped = raw[command.end():].strip()
        if stripped:
            warning = f"prompt {raw!r} was phrased as a command; segmented as noun phrase {stripped!r}"
            raw = stripped
    if len(raw) > MAX_PROMPT_CHARS:
        warning = warning or f"prompt shortened to {MAX_PROMPT_CHARS} characters for reliable matching"
        raw = raw[:MAX_PROMPT_CHARS].rsplit(" ", 1)[0]
    return raw, warning


class VisionAgent:
    """Runs open-vocabulary segmentation and returns consensus groups per frame."""

    name = "Vision & Detection Agent"

    def __init__(self, audit: Any | None = None) -> None:
        self.audit = audit

    # ------------------------------------------------------------------ lanes

    async def _image_lane(self, concept: str, media_path: Path) -> str:
        return await client.segment_image(concept, file_to_data_url(media_path))

    def _simulated_lane(self, concept: str, *, width: int, height: int, inspection_id: str,
                        ground_truth: list[dict[str, Any]], frame_index: int) -> str:
        return simulate_lane(
            concept,
            width=width,
            height=height,
            seed_text=inspection_id,
            frame_index=frame_index, ground_truth=ground_truth,
        )

    async def run_frame(
        self,
        *,
        media_path: Path,
        width: int,
        height: int,
        concepts: list[str],
        frame_index: int,
        inspection_id: str,
        ground_truth: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Segment every concept in one still; returns consensus groups for that frame."""
        ground_truth = ground_truth or []
        lanes: dict[str, str] = {}
        if settings.live:
            lane_results = await asyncio.gather(
                *(self._image_lane(concept, media_path) for concept in concepts),
                return_exceptions=True,
            )
            for concept, result in zip(concepts, lane_results):
                if isinstance(result, BaseException):
                    self._note(f"concept {concept!r} failed on frame {frame_index}: {result}")
                    lanes[concept] = ""
                else:
                    lanes[concept] = result
        else:
            lanes = {
                concept: self._simulated_lane(
                    concept, width=width, height=height, inspection_id=inspection_id,
                    ground_truth=ground_truth, frame_index=frame_index,
                )
                for concept in concepts
            }

        for concept, lane in lanes.items():
            self._record(
                action="sam.segment",
                concept=concept,
                frame_index=frame_index,
                engine="live" if settings.live else "simulated",
                model=settings.sam_model if settings.live else "simulated",
                raw_chars=len(lane),
                raw_lane=lane[:4000],
            )

        detections: list[Detection] = []
        for concept, lane in lanes.items():
            parsed = parse_segmentation_text(lane, concept=concept, scan_id=inspection_id,
                                             frame_index=frame_index)
            detections.extend(parsed.detections)

        return {
            "frame_index": frame_index,
            "concepts": concepts,
            "raw_detections": len(detections),
            "groups": build_consensus(detections, on_error=self._note),
        }

    async def run_clip(self, *, media_path: Path, concepts: list[str], inspection_id: str) -> dict[str, Any]:
        """Native SAM video segmentation (live keys only): upload once, segment by file_id."""
        if not settings.live:
            raise RuntimeError("native clip segmentation needs a live MODEL_API_KEY; extract frames instead")
        file_id = await client.upload_file(media_path)
        self._record(action="files.upload", file_id=file_id, purpose="user_data")
        detections: list[Detection] = []
        for concept in concepts:
            lane = "".join([chunk async for chunk in client.segment_video(concept, file_id)])
            self._record(action="sam.segment_video", concept=concept, model=settings.sam_model,
                         raw_chars=len(lane), raw_lane=lane[:4000])
            detections.extend(parse_segmentation_text(lane, concept=concept, scan_id=inspection_id).detections)

        by_frame: dict[int, list[Detection]] = {}
        for detection in detections:
            by_frame.setdefault(detection.frame_index, []).append(detection)
        frames = [
            {"frame_index": index, "groups": build_consensus(members, on_error=self._note)}
            for index, members in sorted(by_frame.items())
        ]
        return {"concepts": concepts, "raw_detections": len(detections), "frames": frames}

    # ------------------------------------------------------------------ output

    def _record(self, **fields: Any) -> None:
        if self.audit:
            self.audit.record(agent=self.name, phase="II", **fields)

    def _note(self, message: str) -> None:
        if self.audit:
            self.audit.record(agent=self.name, phase="II", action="warning", message=message)


def build_consensus(detections: list[Detection], on_error=None) -> list[dict[str, Any]]:
    """Cluster detections describing the same region inside one frame (Precision over Aggression).

    Regions corroborated by more than one concept prompt earn a higher agreement score;
    single-signal, low-confidence regions are flagged for human review downstream.
    """
    clusters: list[list[Detection]] = []
    for detection in sorted(detections, key=lambda d: -(d.confidence or 0.0)):
        target = None
        for cluster in clusters:
            if any(iou(detection, member) >= MERGE_IOU for member in cluster):
                target = cluster
                break
        if target is None:
            clusters.append([detection])
        else:
            target.append(detection)

    groups: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters):
        best = max(cluster, key=lambda d: d.confidence or 0.0)
        concepts = sorted({d.concept for d in cluster})
        signals = sorted({d.concept for d in cluster if (d.confidence or 0.0) >= MIN_CONFIDENCE})
        svg = ""
        mask_fill = 0.0
        try:
            svg = best.svg_path()
            mask_fill = best.mask_fill
        except Exception as error:  # noqa: BLE001 - a malformed mask must not kill the whole frame
            if on_error:
                on_error(f"mask decode failed for {best.concept}: {error}")
        groups.append(
            {
                "group_id": f"D{index + 1:03d}",
                "primary": best,
                "frame_index": best.frame_index,
                "concept": concepts[0] if len(concepts) == 1 else " / ".join(concepts),
                "all_concepts": concepts,
                "corroborated_by": signals,
                "agreement": round(len(signals) / max(1, len(concepts)), 3),
                "confidence": round(best.confidence or 0.0, 4),
                "mask_svg": svg,
                "mask_fill": round(mask_fill, 4),
                "detection_count": len(cluster),
            }
        )
    return sorted(groups, key=lambda group: (-group["confidence"], -group["agreement"]))


def merge_tracks(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold per-frame groups into persistent defects so one defect is not counted per frame.

    A group joins an existing track when its concept family overlaps and its box overlaps the
    box that track was last seen at by at least TRACK_IOU. Tracking is anchored on the previous
    sighting (a panning camera moves a region a little per frame), while the stored evidence —
    box, mask, confidence — stays that of the clearest frame.
    """
    tracks: list[dict[str, Any]] = []
    for frame in frames:
        for group in frame.get("groups", []):
            match = None
            for track in tracks:
                if track["last_frame"] == group["frame_index"]:
                    continue
                if not set(track["all_concepts"]) & set(group["all_concepts"]):
                    continue
                if iou(track["last_seen"], group["primary"]) >= TRACK_IOU:
                    match = track
                    break
            if match is None:
                joined = dict(group)
                joined["track_id"] = f"T{len(tracks) + 1:03d}"
                joined["frames_seen"] = [group["frame_index"]]
                joined["last_frame"] = group["frame_index"]
                joined["last_seen"] = group["primary"]
                tracks.append(joined)
                continue

            clearer = group["confidence"] > match["confidence"]
            match["frames_seen"].append(group["frame_index"])
            match["last_frame"] = group["frame_index"]
            match["last_seen"] = group["primary"]
            match["detection_count"] += group["detection_count"]
            match["corroborated_by"] = sorted(set(match["corroborated_by"]) | set(group["corroborated_by"]))
            match["all_concepts"] = sorted(set(match["all_concepts"]) | set(group["all_concepts"]))
            match["confidence"] = round(max(match["confidence"], group["confidence"]), 4)
            if clearer:  # keep the sharpest evidence as the record for this defect
                match["primary"] = group["primary"]
                match["mask_svg"] = group["mask_svg"]
                match["mask_fill"] = group["mask_fill"]
                match["frame_index"] = group["frame_index"]
    for track in tracks:
        track["concept"] = (
            track["all_concepts"][0] if len(track["all_concepts"]) == 1 else " / ".join(track["all_concepts"])
        )
        track["agreement"] = round(len(track["corroborated_by"]) / max(1, len(track["all_concepts"])), 3)
    return sorted(tracks, key=lambda t: (-t["confidence"], -len(t["frames_seen"])))
