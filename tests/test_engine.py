"""Job engine behaviour under the API: live stages, cancellation, partial success and retry."""
from __future__ import annotations

import asyncio
import io
import json
import time

from fastapi.testclient import TestClient
from PIL import Image

from infrascan.errors import ProviderError
from infrascan.jobs import job_manager
from infrascan.phases.detect import VisionAgent, merge_tracks
from infrascan.phases.reason import ReasoningAgent


def _jpeg(seed: int) -> tuple[str, io.BytesIO, str]:
    buffer = io.BytesIO()
    Image.new("RGB", (480, 320), (90 + seed % 40, 96, 102)).save(buffer, "JPEG")
    buffer.seek(0)
    return (f"frame_{seed:03d}.jpg", buffer, "image/jpeg")


def test_a_clip_is_refused_honestly_without_a_live_key(client: TestClient, sample_asset: dict,
                                                      waiter) -> None:
    clip = b"\x00\x00\x00\x20ftypmp42" + b"mp4 clip padding for the container check" * 40
    response = client.post("/api/inspections", data={
        "payload": json.dumps({"asset_id": sample_asset["id"], "prompts": ["road pothole"]}),
    }, files={"file": ("site_walk.mp4", clip, "video/mp4")})
    assert response.status_code == 202, response.text
    row = waiter(client, response.json()["inspection"]["id"])
    assert row["status"] == "failed"
    error = json.loads(row["error"])
    assert error["code"] == "video_requires_key"
    assert "MODEL_API_KEY" in error["message"], "the message must name the missing dependency"


def test_extracted_frames_are_stored_as_a_real_multi_frame_inspection(client: TestClient,
                                                                     sample_asset: dict,
                                                                     waiter) -> None:
    payload = json.dumps({"asset_id": sample_asset["id"], "title": "Clip frames",
                          "prompts": ["road pothole", "asphalt crack"], "pixels_per_meter": 90})
    response = client.post(
        "/api/inspections",
        data={"payload": payload, "frame_times": json.dumps([0, 400, 800])},
        files=[("frames", _jpeg(0)), ("frames", _jpeg(1)), ("frames", _jpeg(2))],
    )
    assert response.status_code == 202, response.text
    inspection_id = response.json()["inspection"]["id"]
    row = waiter(client, inspection_id)
    assert row["status"] == "completed", row["error"]
    assert row["media_kind"] == "frames" and row["frames_total"] == 3
    assert row["frames_done"] == 3
    assert [frame["source_time_ms"] for frame in row["frames"]] == [0, 400, 800]

    events = client.get(f"/api/inspections/{inspection_id}/events").text
    assert '"stage": "detecting"' in events
    stages = [json.loads(line[6:]) for line in events.splitlines() if line.startswith("data: ")]
    frames_seen = [event for event in stages if event.get("type") == "frame"]
    assert len({event["index"] for event in frames_seen}) == 3, "every frame must report its own progress"

    image = client.get(f"/api/inspections/{inspection_id}/media", params={"frame": 1})
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/")


def test_one_defect_across_frames_stays_one_defect() -> None:
    """Regression: a clip must not report the same region once per frame.

    An earlier build seeded the per-frame simulator with the frame index, so a 6-frame clip
    became 19 defects and 19 draft work orders with `frames_seen` of one frame each, and
    anchoring tracks on the clearest frame let a 16-frame clip fragment again. The ground-truth
    path in test_sam_grammar could not catch either — fixture boxes are identical in every
    frame — so this covers the real no-sidecar path through VisionAgent.run_frame.
    """
    inspection_id = "I-A1"
    concepts = ["rusted tower lattice", "cracked concrete foundation", "corroded metal joint"]
    frames_total = 16

    async def analyse() -> list[dict]:
        agent = VisionAgent()
        return [
            await agent.run_frame(media_path=None, width=480, height=320, concepts=concepts,
                                  frame_index=index, inspection_id=inspection_id)
            for index in range(frames_total)
        ]

    frames = asyncio.run(analyse())
    regions = [len(frame["groups"]) for frame in frames]
    assert regions and regions[0] > 0, "the fixture concept set must produce regions"

    tracks = merge_tracks(frames)
    assert len(tracks) == regions[0], "one region per frame must fold into one defect per region"
    assert all(track["frames_seen"] == list(range(frames_total)) for track in tracks), \
        "a region that persists must be one defect seen in every frame"
    assert sum(len(track["frames_seen"]) for track in tracks) == sum(regions), \
        "tracks must partition the per-frame regions — none lost, none double counted"


def test_cancellation_stops_the_job_and_is_visible_in_the_stream(client: TestClient, sample_asset: dict,
                                                                 monkeypatch) -> None:
    async def slow_frame(self, **kwargs):  # keep the job inside the detecting stage
        await asyncio.sleep(0.6)
        return await VisionAgent.run_frame(self, **kwargs)

    monkeypatch.setattr(VisionAgent, "run_frame", slow_frame)
    response = client.post("/api/inspections", data={
        "payload": json.dumps({"asset_id": sample_asset["id"], "title": "Cancel me",
                               "prompts": ["road pothole"]}),
        "sample": "road_surface"})
    inspection_id = response.json()["inspection"]["id"]
    deadline = time.time() + 10
    while not job_manager.is_running(inspection_id) and time.time() < deadline:
        time.sleep(0.02)

    assert client.post(f"/api/inspections/{inspection_id}/cancel").json()["status"] == "cancelling"
    row = None
    for _ in range(400):
        row = client.get(f"/api/inspections/{inspection_id}").json()
        if row["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert row["status"] == "cancelled"
    assert row["stage"] == "detecting"
    assert row["error"] in (None, "")

    assert _error(client.post(f"/api/inspections/{inspection_id}/cancel"))["code"] == "not_cancellable"


def test_reasoning_failure_keeps_detections_and_can_be_retried(client: TestClient, sample_asset: dict,
                                                               monkeypatch, waiter) -> None:
    original = ReasoningAgent.run
    state = {"failed": False}

    async def flaky(self, defects, asset, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise ProviderError("rate_limited", "the provider said slow down", status=429)
        return await original(self, defects, asset, **kwargs)

    monkeypatch.setattr(ReasoningAgent, "run", flaky)
    response = client.post("/api/inspections", data={
        "payload": json.dumps({"asset_id": sample_asset["id"], "title": "Partial success",
                               "prompts": ["road pothole"], "pixels_per_meter": 120}),
        "sample": "road_surface"})
    inspection_id = response.json()["inspection"]["id"]
    row = waiter(client, inspection_id)

    assert row["status"] == "completed", row["error"]
    assert row["spark_engine"] == "unavailable"
    assert any("reasoning failed" in warning for warning in row["warnings"])
    assert row["findings"], "vision results must survive a reasoning outage"
    for finding in row["findings"]:
        assert finding["reasoning_done"] is False
        assert finding["severity"] == "Unknown", "never invent a severity the model did not return"
        assert finding["needs_human_review"] is True
    assert not client.get("/api/work-orders", params={"inspection_id": inspection_id}).json()["items"], \
        "unreasoned findings must not silently become work orders"

    retried = client.post(f"/api/inspections/{inspection_id}/retry", json={"mode": "reasoning"})
    assert retried.json()["mode"] == "reasoning"

    done = {}
    for _ in range(400):
        done = client.get(f"/api/inspections/{inspection_id}").json()
        if done["spark_engine"] == "simulated rule engine" and all(
                finding["reasoning_done"] for finding in done["findings"]):
            break
        time.sleep(0.05)
    assert done["status"] == "completed"
    assert done["spark_engine"] == "simulated rule engine"
    assert all(finding["reasoning_done"] is True for finding in done["findings"])
    assert any(finding["severity"] != "Unknown" for finding in done["findings"])
    orders = client.get("/api/work-orders", params={"inspection_id": inspection_id}).json()["items"]
    assert orders, "the retry lifts the finding out of the unassessed pool and raises a draft order"


def _error(response) -> dict:
    body = response.json()
    assert response.status_code >= 400, response.text
    return body["error"]
