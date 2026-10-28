"""API v2 contract tests: validation, error envelope, persistence, approval rules."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

TERMINAL = ("completed", "failed", "cancelled")


def _error(response) -> dict:
    assert response.status_code >= 400, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert {"code", "message", "detail"} <= set(body["error"])
    return body["error"]


def test_config_never_leaks_the_key(client: TestClient) -> None:
    body = client.get("/api/config").json()
    assert body["mode"] == "mock" and body["live"] is False
    assert body["provider_configured"] is False
    flat = json.dumps(body).lower()
    assert "model_api_key" not in flat and "bearer" not in flat


def test_api_and_static_reads_are_not_cacheable(client: TestClient) -> None:
    """A dashboard keeps polling while the page stays open; a cached read is a wrong read."""
    assert client.get("/api/config").headers["cache-control"] == "no-store"
    assert client.get("/api/work-orders").headers["cache-control"] == "no-store"
    assert client.get("/static/app.js").headers["cache-control"] == "no-store"


def test_assets_crud_search_and_pagination(client: TestClient) -> None:
    created = client.post("/api/assets", json={
        "name": "Test gantry 12", "asset_type": "bridge", "region": "Lahore",
        "latitude": 31.52, "longitude": 74.35, "installed_year": 1998,
    })
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]

    assert client.get(f"/api/assets/{asset_id}").json()["asset_type"] == "bridge"
    patched = client.patch(f"/api/assets/{asset_id}", json={"notes": "repainted", "archived": True,
                                                            "actor": "M. Nawaz"})
    assert patched.json()["archived"] == 1
    edits = client.get("/api/audit", params={"action": "asset.update"}).json()["items"]
    entry = next(row for row in edits if row["asset_id"] == asset_id)
    assert entry["agent"] == "Operator" and entry["detail"]["actor"] == "M. Nawaz"
    assert "actor" not in entry["detail"]["fields"], "the actor is not a column on the asset"

    found = client.get("/api/assets", params={"search": "gantry", "status": "archived"}).json()
    assert any(item["id"] == asset_id for item in found["items"])

    page = client.get("/api/assets", params={"page": 1, "page_size": 2}).json()
    assert page["page_size"] == 2 and page["total"] >= 3 and page["pages"] >= 2


def test_asset_validation_is_rejected_at_the_boundary(client: TestClient) -> None:
    error = _error(client.post("/api/assets", json={"name": "Kanjli gantry", "asset_type": "road_network",
                                                    "latitude": 12.5}))
    assert error["code"] == "invalid_input"
    assert "latitude and longitude" in error["message"]

    assert "asset_type" in _error(client.post("/api/assets", json={"name": "Valid name",
                                                                   "asset_type": "flying_car"}))["message"]
    assert "last_inspection" in _error(client.post("/api/assets", json={"name": "Valid name",
                                                                        "last_inspection": "03/2026"}))["message"]
    assert "surprise" in _error(client.post("/api/assets", json={"name": "Valid name",
                                                                 "surprise": 1}))["message"]


def test_asset_delete_requires_confirmation_and_care(client: TestClient) -> None:
    asset = client.post("/api/assets", json={"name": "Delete me bridge", "asset_type": "bridge"}).json()
    assert _error(client.delete(f"/api/assets/{asset['id']}"))["code"] == "confirm_required"
    assert client.delete(f"/api/assets/{asset['id']}", params={"confirm": asset["id"]}).json()["deleted"]
    assert _error(client.get(f"/api/assets/{asset['id']}"))["code"] == "not_found"


def test_asset_with_history_is_not_deleted_silently(client: TestClient, sample_asset: dict, waiter) -> None:
    asset = client.post("/api/assets", json={"name": "Guarded viaduct", "asset_type": "bridge"}).json()
    response = client.post("/api/inspections", data={
        "payload": json.dumps({"asset_id": asset["id"], "prompts": ["concrete crack"]}),
        "sample": "road_surface"})
    assert response.status_code == 202, response.text
    inspection_id = response.json()["inspection"]["id"]
    assert waiter(client, inspection_id)["status"] == "completed"

    error = _error(client.delete(f"/api/assets/{asset['id']}", params={"confirm": asset["id"]}))
    assert error["code"] == "asset_has_inspections"
    assert client.delete(f"/api/assets/{asset['id']}",
                         params={"confirm": asset["id"], "cascade": "true"}).status_code == 200
    assert client.get("/api/inspections", params={"asset_id": asset["id"]}).json()["total"] == 0


def test_unknown_ids_use_the_error_contract(client: TestClient) -> None:
    assert _error(client.get("/api/inspections/I-0000000000-aaaaaa"))["code"] == "not_found"
    assert _error(client.get("/api/work-orders/WO-nope"))["code"] == "not_found"
    assert _error(client.get("/api/findings/nope"))["code"] == "not_found"


def test_inspection_needs_media_and_a_real_asset(client: TestClient, sample_asset: dict) -> None:
    missing = client.post("/api/inspections", data={"payload": json.dumps({"asset_id": sample_asset["id"]})})
    assert _error(missing)["code"] == "no_media"

    unknown = client.post("/api/inspections", data={"payload": json.dumps({"asset_id": "A-does-not-exist"}),
                                                    "sample": "road_surface"})
    assert _error(unknown)["code"] == "not_found"

    too_many = client.post("/api/inspections", data={"payload": json.dumps({
        "asset_id": sample_asset["id"], "prompts": [f"crack type {index}" for index in range(9)]}),
        "sample": "road_surface"})
    assert _error(too_many)["code"] == "invalid_input"


def test_mock_run_walks_the_stage_ladder_in_order(client: TestClient, completed_inspection: dict) -> None:
    events = _stage_events(client, completed_inspection["id"])
    assert [event["stage"] for event in events if event["status"] == "completed"] == [
        "validating", "preprocessing", "detecting", "aggregating", "reasoning", "generating_report"]
    progress = [event["progress"] for event in events if event["progress"] is not None]
    assert progress == sorted(progress)
    assert completed_inspection["stage"] == "completed"
    assert completed_inspection["findings"], "the vision stage must produce regions"
    assert completed_inspection["frames_total"] == 1
    assert completed_inspection["mode"] == "mock"
    assert completed_inspection["sam_engine"] == "simulated sam-3.1"
    assert completed_inspection["error"] in (None, "")


def _stage_events(client: TestClient, inspection_id: str) -> list[dict]:
    events = client.get(f"/api/inspections/{inspection_id}/events", params={"after_seq": 0}).text
    parsed = []
    for line in events.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload.get("type") == "stage":
                parsed.append(payload)
    return parsed


def test_completed_run_labels_simulated_output_honestly(client: TestClient, completed_inspection: dict) -> None:
    finding = completed_inspection["findings"][0]
    assert finding["detection_engine"] == "simulated sam-3.1"
    assert finding["reasoning_engine"] == "simulated rule engine"
    assert finding["concept"]
    assert isinstance(finding["needs_human_review"], bool)

    assert completed_inspection["artifacts"], "the report stage must write artifacts"
    for artifact in completed_inspection["artifacts"]:
        assert artifact["bytes"] > 0
        download = client.get(f"/api/inspections/{completed_inspection['id']}/artifacts/{artifact['name']}")
        assert download.status_code == 200, artifact


def test_work_orders_are_never_auto_approved(client: TestClient, completed_inspection: dict) -> None:
    orders = client.get("/api/work-orders", params={"inspection_id": completed_inspection["id"]}).json()["items"]
    assert orders, "the report stage must raise draft work orders"
    assert all(order["status"] in ("draft", "pending_approval") for order in orders)
    assert all(order["source_mode"] == "mock" for order in orders)
    assert all(finding["status"] == "open" for finding in completed_inspection["findings"]), \
        "an unapproved order must not mark the defect as handled"


def test_approval_needs_an_actor_and_a_legal_transition(client: TestClient, completed_inspection: dict) -> None:
    orders = client.get("/api/work-orders", params={"inspection_id": completed_inspection["id"]}).json()["items"]
    order_id = orders[0]["id"]

    assert _error(client.post(f"/api/work-orders/{order_id}/approve",
                              json={"actor": "x"}))["code"] == "invalid_input"

    if orders[0]["status"] == "draft":
        moved = client.post(f"/api/work-orders/{order_id}/transition",
                            json={"to_status": "pending_approval", "actor": "ops lead"}).json()
        assert moved["status"] == "pending_approval"

    approved = client.post(f"/api/work-orders/{order_id}/approve",
                           json={"actor": "ops lead", "note": "scoped repair"}).json()
    assert approved["status"] == "approved"
    assert approved["history"][-1]["actor"] == "ops lead"
    for finding_id in approved["finding_ids"]:
        assert client.get(f"/api/findings/{finding_id}").json()["status"] == "converted"

    assert _error(client.post(f"/api/work-orders/{order_id}/transition",
                             json={"to_status": "draft", "actor": "ops lead"}))["code"] == "invalid_transition"

    assert client.post(f"/api/work-orders/{order_id}/transition",
                       json={"to_status": "in_progress", "actor": "crew 4"}).json()["status"] == "in_progress"
    done = client.post(f"/api/work-orders/{order_id}/transition",
                       json={"to_status": "completed", "actor": "crew 4"}).json()
    assert done["status"] == "completed"
    for finding_id in done["finding_ids"]:
        assert client.get(f"/api/findings/{finding_id}").json()["status"] == "closed"

    assert _error(client.patch(f"/api/work-orders/{order_id}",
                              json={"title": "too late", "actor": "ops lead"}))["code"] == "locked"


def test_manual_work_order_only_accepts_findings_from_its_inspection(client: TestClient,
                                                                    completed_inspection: dict) -> None:
    foreign = client.post("/api/work-orders", json={
        "inspection_id": completed_inspection["id"], "finding_ids": ["other-inspection-F001"],
        "actor": "plan engineer"})
    error = _error(foreign)
    assert error["code"] == "finding_not_found"
    assert error["detail"]["missing"] == ["other-inspection-F001"]


def test_work_order_export_declares_no_live_connection(client: TestClient) -> None:
    response = client.get("/api/work-orders/export")
    assert response.status_code == 200
    payload = response.json()
    assert payload["connection"]["cmms"] is False
    assert payload["connection"]["email"] is False
    assert payload["connection"]["sms"] is False
    assert "no external system" in payload["connection"]["note"].lower()
    assert payload["count"] == len(payload["items"])


def test_idempotency_key_replays_the_first_inspection(client: TestClient, sample_asset: dict) -> None:
    body = json.dumps({"asset_id": sample_asset["id"], "prompts": ["road pothole"],
                       "idempotency_key": "smoke-duplicate"})
    first = client.post("/api/inspections", data={"payload": body, "sample": "road_surface"})
    second = client.post("/api/inspections", data={"payload": body, "sample": "road_surface"})
    assert first.status_code == 202 and second.json()["deduplicated"] is True
    assert first.json()["inspection"]["id"] == second.json()["inspection"]["id"]


def test_dashboard_counts_match_stored_records(client: TestClient, completed_inspection: dict) -> None:
    stats = client.get("/api/dashboard").json()
    assert stats["inspections_completed"] >= 1
    assert stats["findings_total"] == client.get("/api/findings", params={"page_size": 100}).json()["total"]
    assert stats["work_orders_awaiting"] >= 0
    assert stats["engines"]["segmentation"] == "simulated"


def test_audit_trail_records_real_backend_events(client: TestClient, completed_inspection: dict) -> None:
    audit = client.get(f"/api/inspections/{completed_inspection['id']}/audit").json()
    actions = {item["action"] for item in audit["items"]}
    assert {"job.start", "media.validate", "preprocess", "sam.segment", "context.correlate",
            "artifacts.generate", "job.complete"} <= actions
    assert all("api_key" not in json.dumps(item).lower() for item in audit["items"])


def test_audit_paging_is_complete_and_newest_first(client: TestClient, completed_inspection: dict) -> None:
    """The pager renders 'Page 1 of N' from these fields; a missing page count renders undefined.

    Scoped to one finished job on purpose: its event set is closed, while an unscoped offset
    window shifts when other tests still have jobs writing audit events in flight.
    """
    scope = {"inspection_id": completed_inspection["id"], "page_size": 3}
    audit = client.get("/api/audit", params=scope).json()
    assert audit["page"] == 1 and audit["pages"] >= 2 and audit["total"] >= 6
    ids = [item["id"] for item in audit["items"]]
    assert ids == sorted(ids, reverse=True), "newest events first"

    second = client.get("/api/audit", params={**scope, "page": 2}).json()
    later = [item["id"] for item in second["items"]]
    assert not set(ids) & set(later), "pages must not overlap"
    assert max(later) < min(ids), "the older page must hold strictly older rows"


def test_rejects_a_non_media_upload_without_storing_it(client: TestClient, sample_asset: dict) -> None:
    before = client.get("/api/inspections", params={"asset_id": sample_asset["id"]}).json()["total"]
    response = client.post(
        "/api/inspections",
        data={"payload": json.dumps({"asset_id": sample_asset["id"]})},
        files={"file": ("notes.txt", b"not an image at all", "text/plain")},
    )
    assert _error(response)["code"] == "unsupported_input"
    after = client.get("/api/inspections", params={"asset_id": sample_asset["id"]}).json()["total"]
    assert after == before, "a rejected upload must not leave a job row behind"


def test_refuses_3d_formats_with_an_explanation(client: TestClient, sample_asset: dict) -> None:
    before = client.get("/api/inspections", params={"asset_id": sample_asset["id"]}).json()["total"]
    response = client.post(
        "/api/inspections",
        data={"payload": json.dumps({"asset_id": sample_asset["id"]})},
        files={"file": ("model.glb", b"glTF-BINARY\x00\x00\x00\x00", "model/gltf-binary")},
    )
    error = _error(response)
    assert error["code"] == "unsupported_input"
    assert "viewer" in error["message"]
    assert client.get("/api/inspections", params={"asset_id": sample_asset["id"]}).json()["total"] == before


def test_provider_health_reports_the_truth_without_a_key(client: TestClient, monkeypatch) -> None:
    from infrascan import meta_client

    async def fake_check(self) -> dict:
        return {"ok": False, "status_code": 401, "latency_ms": 12, "kind": "authentication",
                "message": "invalid api key sk-SECRETVALUE123"}

    monkeypatch.setattr(meta_client.MetaClient, "check_connectivity", fake_check)
    result = client.post("/api/providers/check").json()
    assert result["ok"] is False and result["configured"] is False
    assert "sk-SECRETVALUE123" not in json.dumps(result)

    health = client.get("/api/providers").json()
    assert health["configured"] is False
    assert health["checks"][-1]["message"].find("sk-SECRETVALUE123") == -1


def test_connectivity_check_sends_nothing_without_a_key(client: TestClient) -> None:
    """No key means no request, and the panel has to say that instead of a transport error."""
    result = client.post("/api/providers/check").json()
    assert result["ok"] is False
    assert result["kind"] == "not_configured"
    assert result["status_code"] == 0 and result["latency_ms"] == 0
    assert result["available_models"] == []
    assert "MODEL_API_KEY" in result["message"]


def test_sample_is_a_multipart_field_not_a_payload_key(client: TestClient, sample_asset: dict) -> None:
    """The browser learned this the hard way: the payload model forbids extras, so `sample`
    must travel as its own form field or intake fails before the job is queued."""
    smuggled = client.post("/api/inspections", data={"payload": json.dumps(
        {"asset_id": sample_asset["id"], "prompts": ["asphalt crack"], "sample": "road_surface"})})
    assert smuggled.status_code == 422
    assert "sample" in json.dumps(smuggled.json())

    accepted = client.post("/api/inspections", data={
        "payload": json.dumps({"asset_id": sample_asset["id"], "prompts": ["asphalt crack"]}),
        "sample": "road_surface"})
    assert accepted.status_code == 202, accepted.text
    row = accepted.json()["inspection"]
    assert row["source_name"] == "road_surface.jpg" and row["media_kind"] == "image"
