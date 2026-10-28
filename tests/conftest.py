"""Test harness isolation: a throwaway data directory, mock mode, and no ambient API key."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

_DATA = Path(tempfile.mkdtemp(prefix="infrascan-tests-"))
os.environ["INFRASCAN_DATA_DIR"] = str(_DATA)
os.environ["INFRASCAN_MODE"] = "mock"
os.environ.pop("MODEL_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from infrascan.db import init_db
from infrascan.seed import ensure_seed_data
from infrascan.server import app

TERMINAL = ("completed", "failed", "cancelled")


@pytest.fixture(scope="session", autouse=True)
def _database():
    init_db()
    ensure_seed_data()
    yield
    shutil.rmtree(_DATA, ignore_errors=True)


@pytest.fixture(scope="session")
def client():
    """One app for the session: the lifespan runs once, so jobs are never reconciled mid-flight."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def sample_asset(client: TestClient) -> dict:
    """The seeded road-network sample asset, whose image ships with ground-truth fixtures."""
    items = client.get("/api/assets", params={"search": "Raiwind"}).json()["items"]
    assert items, "the sample assets must be seeded before the tests run"
    return items[0]


def wait_for(client: TestClient, inspection_id: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = client.get(f"/api/inspections/{inspection_id}").json()
        if row["status"] in TERMINAL:
            return row
        time.sleep(0.05)
    raise AssertionError(f"inspection {inspection_id} never reached a terminal state")


@pytest.fixture(scope="session")
def waiter():
    return wait_for


@pytest.fixture(scope="session")
def run_inspection(client: TestClient, sample_asset: dict):
    """Factory: launch a mock inspection and return it once the job has settled."""

    def _run(prompts: list[str] | None = None, sample: str = "road_surface", **payload) -> dict:
        body = {"asset_id": sample_asset["id"], "title": payload.pop("title", "Fixture run"),
                "prompts": prompts if prompts is not None else ["detect asphalt cracks", "road pothole"]}
        body.update(payload)
        response = client.post("/api/inspections", data={"payload": json.dumps(body), "sample": sample})
        assert response.status_code == 202, response.text
        return wait_for(client, response.json()["inspection"]["id"])

    return _run


@pytest.fixture(scope="session")
def completed_inspection(run_inspection) -> dict:
    """One real mock run shared by the read-side tests, so the engine is exercised once."""
    row = run_inspection(pixels_per_meter=120)
    assert row["status"] == "completed", row["error"]
    return row
