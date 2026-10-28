"""Tests for the SAM 3.1 wire grammar, mask codec and the deterministic simulator."""
from __future__ import annotations

import json

import pytest

from infrascan.config import ROOT
from infrascan.mask_codec import encode_one_bit_mask
from infrascan.phases.detect import build_consensus, merge_tracks, normalise_prompt
from infrascan.sam_grammar import parse_segmentation_text
from infrascan.simulation import simulate_lane

DOC_EXAMPLE = (
    "<0f>0<|box;x1=353;y1=382;x2=453;y2=427;w=537;h=561|>"
    "<|mask;x=0;y=0;data=46,101,~...|>,1<|box;x1=260;y1=382;x2=340;y2=427;w=537;h=561|>"
    "<|mask;x=0;y=0;data=46,81,~...|>,2<|box;x1=516;y1=117;x2=536;y2=150;w=537;h=561|>"
    "<|mask;x=0;y=0;data=34,21,~...|>"
)


def test_parses_documented_image_line() -> None:
    parsed = parse_segmentation_text(DOC_EXAMPLE, concept="button")
    assert len(parsed.detections) == 3
    first = parsed.detections[0]
    assert (first.left, first.top, first.right, first.bottom) == (353, 382, 453, 427)
    assert (first.source_width, first.source_height) == (537, 561)
    assert (first.mask_height, first.mask_width) == (46, 101)


def test_parses_confidence_and_sparse_ids() -> None:
    line = (
        "<0f>1<|box;x1=444;y1=625;x2=679;y2=850;w=1800;h=1200;c=0.9609375|>"
        "<|mask;x=0;y=0;c=0.9609375;data=226,236,!xx|>"
    )
    parsed = parse_segmentation_text(line)
    assert parsed.detections[0].object_id == "1"
    assert parsed.detections[0].confidence == pytest.approx(0.9609375)


def test_zero_matches_is_a_valid_empty_response() -> None:
    assert parse_segmentation_text("").detections == []


def test_mask_roundtrip() -> None:
    width, height = 24, 12
    raster = [1 if (x // 3 + y // 3) % 2 == 0 else 0 for y in range(height) for x in range(width)]
    line = (
        f"<0f>0<|box;x1=0;y1=0;x2={width};y2={height};w=100;h=100|>"
        f"<|mask;x=0;y=0;data={height},{width},{encode_one_bit_mask(raster, width, height)}|>"
    )
    detection = parse_segmentation_text(line).detections[0]
    assert list(detection.pixels()) == raster
    assert 0 < detection.mask_fill <= 1
    assert detection.svg_path().startswith("M")


def test_simulated_lane_uses_the_documented_grammar() -> None:
    lane = simulate_lane("asphalt crack", width=640, height=480, seed_text="I-test")
    detections = parse_segmentation_text(lane, concept="asphalt crack").detections
    assert detections, "the simulator should emit at least one region for a crack prompt"
    assert detections[0].svg_path()


def test_consensus_rewards_corroboration() -> None:
    lane = simulate_lane("asphalt crack", width=640, height=480, seed_text="I-corroborate")
    one = parse_segmentation_text(lane, concept="asphalt crack").detections
    two = parse_segmentation_text(lane, concept="surface crack").detections
    groups = build_consensus(one + two)
    assert groups
    assert groups[0]["agreement"] == 1.0
    assert set(groups[0]["corroborated_by"]) == {"asphalt crack", "surface crack"}


def test_a_single_signal_is_not_reported_as_agreement() -> None:
    lane = simulate_lane("asphalt crack", width=640, height=480, seed_text="I-single")
    groups = build_consensus(parse_segmentation_text(lane, concept="asphalt crack").detections)
    assert groups
    assert all(group["agreement"] == 1.0 and len(group["corroborated_by"]) == 1 for group in groups)


def test_commands_are_rewritten_as_noun_phrases() -> None:
    assert normalise_prompt("Please detect any visible asphalt cracks")[0] == "asphalt cracks"
    assert normalise_prompt("road pothole")[1] is None
    concept, warning = normalise_prompt("count the cracks in this wall and report them")
    assert concept and warning and len(concept) <= 64
    assert normalise_prompt("   ")[0] == ""


def test_repeated_regions_across_frames_merge_into_one_defect() -> None:
    defects = json.loads((ROOT / "samples" / "road_surface.defects.json").read_text(encoding="utf-8"))["defects"]
    frames = [
        {"frame_index": index, "raw_detections": 3, "groups": build_consensus(
            parse_segmentation_text(
                simulate_lane("road pothole", width=1024, height=768, seed_text=f"I-{index}",
                              frame_index=index, ground_truth=defects),
                concept="road pothole", frame_index=index).detections)}
        for index in range(3)
    ]
    tracks = merge_tracks(frames)
    merged = [track for track in tracks if len(track["frames_seen"]) == 3]
    assert merged, "the same region seen in three frames must become one defect"
    assert merged[0]["detection_count"] >= 3
    assert merged[0]["frames_seen"] == [0, 1, 2]


def test_the_wire_frame_prefix_carries_the_frame_identity() -> None:
    defects = json.loads((ROOT / "samples" / "road_surface.defects.json").read_text(encoding="utf-8"))["defects"]
    lane = simulate_lane("road pothole", width=640, height=480, seed_text="I-prefixed",
                         frame_index=7, ground_truth=defects)
    assert lane.startswith("<7f>")
    assert {detection.frame_index for detection in parse_segmentation_text(lane).detections} == {7}
