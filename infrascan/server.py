"""InfraScan 3D HTTP API v2: validated boundary in front of the SQLite-backed job engine."""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import __version__, repo, uploads
from .config import BASE_DIR, ROOT, STATIC_DIR, settings
from .db import connect, init_db
from .errors import InfraScanError, normalize, redact
from .jobs import STAGES, TERMINAL, job_manager, stream_events
from .meta_client import client
from .repo import WORK_ORDER_TRANSITIONS
from .report_pdf import inspection_pdf
from .schemas import (
    AssetIn,
    AssetPatch,
    DecisionIn,
    FindingPatch,
    InspectionCreate,
    RetryIn,
    TransitionIn,
    WorkOrderIn,
    WorkOrderPatch,
    as_json,
)
from .seed import ensure_seed_data

SAMPLES_DIR = ROOT / "samples"
ASSET_TYPES = ["road_network", "bridge", "pipeline", "power_grid", "facade", "other"]
SEVERITIES = ["Unknown", "Low", "Medium", "High", "Critical"]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Migrations and seeding are idempotent; nothing here overwrites existing rows."""
    init_db()
    ensure_seed_data()
    removed = uploads.purge_older_than(settings.retention_days)
    _reconcile_interrupted()
    repo.audit(agent="Server", phase="boot", action="startup", mode=settings.mode,
               purged_dirs=removed, schema=_schema_version())
    yield
    repo.audit(agent="Server", phase="boot", action="shutdown")


def _schema_version() -> int:
    with connect() as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def _reconcile_interrupted() -> None:
    """A restart kills in-memory tasks; leaving rows as 'running' would be a lie."""
    with connect() as db:
        stale = db.execute("SELECT id FROM inspections WHERE status IN ('queued','running')").fetchall()
        for row in stale:
            db.execute(
                "UPDATE inspections SET status='failed', cancel_requested=0, finished_at=?,"
                " error=?, updated_at=? WHERE id = ?",
                (time.time(), json.dumps({"code": "interrupted", "message":
                                         "the server stopped while this job was running; retry it",
                                         "detail": {}}), time.time(), row["id"]),
            )
            repo.record_event(row["id"], {"type": "done", "status": "failed", "at": time.time(),
                                          "error": {"code": "interrupted",
                                                    "message": "server restarted during this job"}})
            repo.audit(inspection_id=row["id"], agent="Server", phase="boot", action="job.interrupted")


app = FastAPI(title="InfraScan 3D", version=__version__, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _no_api_cache(request: Request, call_next) -> Response:
    """Job state moves while a page stays open, so a cached API read is a wrong read."""
    response = await call_next(request)
    if request.url.path.startswith(("/api/", "/static/")):
        response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------- error contract


@app.exception_handler(InfraScanError)
async def _infra_error(_request: Request, exc: InfraScanError) -> JSONResponse:
    return JSONResponse(exc.to_payload(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [{"field": _field_of(item), "message": str(item["msg"]).removeprefix("Value error, ")}
              for item in exc.errors()]
    return JSONResponse({"error": {"code": "invalid_input",
                                   "message": "; ".join(f"{f['field']}: {f['message']}" for f in fields)[:400]
                                   or "the request failed validation",
                                   "detail": {"fields": fields}}}, status_code=422)


def _field_of(item: dict[str, list]) -> str:
    """Name the offending field, or 'body' when a whole-object rule failed."""
    parts = [str(part) for part in item["loc"][1:]]
    tail = parts[-1] if parts and parts[-1] not in ("value_error", "model_validate") else ""
    return ".".join(parts) or tail or "body"


@app.exception_handler(Exception)
async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
    problem = normalize(exc)
    repo.audit(agent="Server", phase="api", action="api.error",
               code=problem.code, message=redact(str(exc))[:300])
    return JSONResponse(problem.to_payload(), status_code=problem.status)


def _body(data: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(data, default=str)), status_code=status_code)


def _parse(form_payload: str) -> InspectionCreate:
    try:
        return InspectionCreate.model_validate_json(form_payload)
    except Exception as error:
        raise InfraScanError("invalid_input", redact(str(error))[:400], status=422) from error


def _get_inspection(inspection_id: str) -> dict[str, Any]:
    inspection = repo.get_inspection(inspection_id)
    if inspection is None:
        raise InfraScanError("not_found", f"no inspection {inspection_id}", status=404)
    return inspection


def _get_asset(asset_id: str) -> dict[str, Any]:
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise InfraScanError("not_found", f"no asset {asset_id}", status=404)
    return asset


def _page(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page), min(100, max(1, page_size))


# ------------------------------------------------------------------------------ shell


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Client-safe configuration. The API key itself never crosses this boundary."""
    return {
        "version": __version__,
        "mode": settings.mode,
        "live": settings.live,
        "provider_configured": bool(settings.api_key),
        "base_url": settings.base_url,
        "sam_model": settings.sam_model,
        "spark_model": settings.spark_model,
        "engine_labels": {
            "segmentation": settings.sam_model if settings.live else f"simulated ({settings.sam_model} wire format)",
            "reasoning": settings.spark_model if settings.live else "simulated rule engine",
        },
        "limits": {
            "max_upload_mb": settings.max_upload_mb,
            "max_frames": settings.max_frames,
            "max_prompts": 8,
            "retention_days": settings.retention_days,
            "request_timeout": settings.request_timeout,
        },
        "choices": {
            "asset_types": ASSET_TYPES,
            "severities": SEVERITIES,
            "stages": list(STAGES),
            "work_order_transitions": {key: sorted(value) for key, value in WORK_ORDER_TRANSITIONS.items()},
            "accepted_suffixes": sorted(uploads.IMAGE_SUFFIXES | uploads.VIDEO_SUFFIXES),
        },
    }


@app.get("/api/samples")
def samples() -> list[dict[str, Any]]:
    out = []
    for path in sorted(SAMPLES_DIR.glob("*")):
        if path.suffix.lower() not in uploads.IMAGE_SUFFIXES:
            continue
        out.append({
            "stem": path.stem,
            "name": path.name,
            "kind": "image",
            "bytes": path.stat().st_size,
            "has_ground_truth": uploads.sidecar_for(path) is not None,
        })
    return out


@app.get("/api/activity")
def activity(minutes: int = Query(240, ge=1, le=10080)) -> list[dict[str, Any]]:
    return repo.last_activity(minutes)


# -------------------------------------------------------------------------- providers


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    checks = repo.latest_provider_checks()
    return {
        "configured": settings.live,
        "mode": settings.mode,
        "base_url": settings.base_url,
        "secret_note": "The API key is held in server-side configuration only; this response never contains it.",
        "models": {"segmentation": settings.sam_model, "reasoning": settings.spark_model},
        "checks": checks,
    }


@app.post("/api/providers/check")
async def provider_check() -> dict[str, Any]:
    """One cheap real request (GET /models) so connectivity claims are evidence, not guesswork."""
    result = await client.check_connectivity()
    repo.record_provider_check(
        provider="meta_model_api",
        model=f"{settings.sam_model},{settings.spark_model}",
        configured=settings.live,
        verified_ok=bool(result.get("ok")),
        status_code=int(result.get("status_code") or 0),
        latency_ms=int(result.get("latency_ms") or 0),
        message=redact(str(result.get("message", ""))),
        mode=settings.mode,
    )
    repo.audit(agent="Provider", phase="ops", action="provider.check",
               ok=bool(result.get("ok")), status_code=result.get("status_code"),
               kind=result.get("kind", ""))
    return {
        "ok": bool(result.get("ok")),
        "status_code": result.get("status_code"),
        "latency_ms": result.get("latency_ms"),
        "message": redact(str(result.get("message", ""))),
        "kind": result.get("kind", ""),
        "available_models": result.get("models", [])[:20],
        "configured": settings.live,
    }


# ----------------------------------------------------------------------------- assets


@app.get("/api/assets")
def list_assets(search: str = "", asset_type: str = "", status: str = "active",
                sort: str = "updated_desc", page: int = Query(1, ge=1),
                page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    page, page_size = _page(page, page_size)
    return repo.list_assets(search=search, asset_type=asset_type, status=status,
                            sort=sort, page=page, page_size=page_size)


@app.post("/api/assets", status_code=201)
def create_asset(data: AssetIn) -> dict[str, Any]:
    asset = repo.create_asset(**as_json(data))
    repo.audit(asset_id=asset["id"], agent="Server", phase="ops", action="asset.create", name=asset["name"])
    return asset


@app.get("/api/assets/{asset_id}")
def get_asset(asset_id: str) -> dict[str, Any]:
    return _get_asset(asset_id)


@app.patch("/api/assets/{asset_id}")
def patch_asset(asset_id: str, data: AssetPatch) -> dict[str, Any]:
    _get_asset(asset_id)
    changes = as_json(data)
    actor = str(changes.pop("actor", "") or "") or "anonymous"
    asset = repo.update_asset(asset_id, changes) or {}
    repo.audit(asset_id=asset_id, agent="Operator", phase="ops", action="asset.update",
               fields=sorted(changes), actor=actor)
    return asset


@app.delete("/api/assets/{asset_id}")
def delete_asset(asset_id: str, confirm: str = "", cascade: bool = False) -> dict[str, Any]:
    asset = _get_asset(asset_id)
    if confirm != asset_id:
        raise InfraScanError("confirm_required",
                             "repeat the asset id as ?confirm=<id> to delete", status=409,
                             detail={"confirm": asset_id})
    outcome = repo.delete_asset(asset_id, cascade=cascade)
    if outcome == "blocked":
        raise InfraScanError(
            "asset_has_inspections",
            "this asset has inspection history; pass ?cascade=true to delete the history too",
            status=409, detail={"asset_id": asset_id},
        )
    if cascade:
        for row in repo.list_inspections(asset_id=asset_id, page_size=1000)["items"]:
            uploads.purge_inspection(row["id"])
    repo.audit(asset_id=asset_id, agent="Server", phase="ops", action="asset.delete",
               name=asset["name"], cascade=cascade)
    return {"deleted": asset_id, "cascade": cascade}


@app.get("/api/assets/{asset_id}/inspections")
def asset_inspections(asset_id: str, page: int = Query(1, ge=1),
                      page_size: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
    _get_asset(asset_id)
    page, page_size = _page(page, page_size)
    return repo.list_inspections(asset_id=asset_id, page=page, page_size=page_size)


# ------------------------------------------------------------------------ inspections


@app.post("/api/inspections", status_code=202)
async def create_inspection(
    payload: str = Form(...),
    file: UploadFile | None = File(None),  # noqa: B008 - FastAPI reads the default as the field spec
    sample: str = Form(""),
    frames: list[UploadFile] = File(default=[]),  # noqa: B008
    frame_times: str = Form(""),
) -> JSONResponse:
    """Accept media, persist the job row, then hand it to the engine. Returns as soon as it is queued."""
    data = _parse(payload)
    asset = _get_asset(data.asset_id)

    repeat = repo.find_by_idempotency_key(data.idempotency_key or "")
    if repeat is not None:
        return _body({"inspection": repeat, "deduplicated": True}, status_code=200)

    parts = [part for part in frames if part and part.filename]
    if not parts and file is not None and file.filename:
        parts = [file]
    if not parts and not sample:
        raise InfraScanError("no_media", "attach one image, a clip, extracted frames or a sample", status=422)

    inspection = repo.create_inspection(
        asset_id=asset["id"],
        title=data.title or f"{asset['name']} inspection",
        prompts=data.prompts,
        media_kind="image",
        source_name="",
        source_path="",
        pixels_per_meter=data.pixels_per_meter,
        mode=settings.mode,
        idempotency_key=data.idempotency_key,
    )

    try:
        inspection = await _attach_media(inspection, parts, sample, frame_times)
    except Exception as error:
        repo.delete_inspection(inspection["id"])
        uploads.purge_inspection(inspection["id"])
        raise normalize(error) from error

    job_manager.submit(inspection["id"])
    repo.audit(inspection_id=inspection["id"], asset_id=asset["id"], agent="Server",
               phase="api", action="inspection.create", source=inspection["source_name"],
               media_kind=inspection["media_kind"], frames_total=inspection["frames_total"],
               prompts=inspection["prompts"], mode=settings.mode)
    return _body({"inspection": inspection, "deduplicated": False}, status_code=202)


async def _attach_media(inspection: dict[str, Any], parts: list[UploadFile], sample: str,
                        frame_times: str) -> dict[str, Any]:
    """Validate and store the media, then point the inspection row at exactly those files."""
    inspection_id = inspection["id"]
    dest = uploads.inspection_dir(inspection_id)

    if sample and not parts:
        source = uploads.resolve_sample(sample)
        kind = "video" if source.suffix.lower() in uploads.VIDEO_SUFFIXES else "image"
        fields: dict[str, Any] = {"media_kind": kind, "source_name": source.name,
                                  "source_path": str(source), "frames_total": 1}
        if kind == "image":
            with Image.open(source) as image:
                fields["width"], fields["height"] = image.size
    elif len(parts) > 1:
        frames = uploads.save_frames(dest, parts, _times(frame_times, len(parts)))
        for frame in frames:
            repo.add_frame(inspection_id, frame_index=frame.frame_index, path=str(frame.path),
                           width=frame.width, height=frame.height, source_time_ms=frame.source_time_ms)
        fields = {"media_kind": "frames", "source_name": parts[0].filename or "extracted frames",
                  "source_path": str(frames[0].path), "width": frames[0].width,
                  "height": frames[0].height, "frames_total": len(frames)}
    else:
        media = uploads.save_upload(parts[0], dest)
        fields = {"media_kind": media.kind, "source_name": media.filename,
                  "source_path": str(media.path), "width": media.width, "height": media.height,
                  "frames_total": 1}

    repo.set_inspection(inspection_id, **fields)
    return repo.get_inspection(inspection_id) or inspection


def _times(raw: str, count: int) -> list[int]:
    try:
        values = [int(item) for item in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        values = []
    return [values[index] if index < len(values) else 0 for index in range(count)]


@app.get("/api/inspections")
def list_inspections(asset_id: str = "", status: str = "", search: str = "",
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    page, page_size = _page(page, page_size)
    return repo.list_inspections(asset_id=asset_id, status=status, search=search,
                                 page=page, page_size=page_size)


@app.get("/api/inspections/{inspection_id}")
def get_inspection(inspection_id: str) -> dict[str, Any]:
    return _get_inspection(inspection_id)


@app.delete("/api/inspections/{inspection_id}")
def delete_inspection(inspection_id: str, confirm: str = "") -> dict[str, Any]:
    inspection = _get_inspection(inspection_id)
    if job_manager.is_running(inspection_id):
        raise InfraScanError("running", "cancel the job before deleting its inspection", status=409)
    if confirm != inspection_id:
        raise InfraScanError("confirm_required", "repeat the inspection id as ?confirm=<id>",
                             status=409, detail={"confirm": inspection_id})
    repo.delete_inspection(inspection_id)
    uploads.purge_inspection(inspection_id)
    repo.audit(inspection_id=inspection_id, agent="Server", phase="ops", action="inspection.delete",
               source=inspection["source_name"])
    return {"deleted": inspection_id}


@app.post("/api/inspections/{inspection_id}/cancel")
def cancel_inspection(inspection_id: str) -> dict[str, Any]:
    inspection = _get_inspection(inspection_id)
    outcome = job_manager.request_cancel(inspection_id)
    if outcome == "cancelling":
        return {"inspection_id": inspection_id, "status": "cancelling"}
    if inspection["status"] in ("queued", "running"):
        repo.set_inspection(inspection_id, status="cancelled", cancel_requested=0,
                            finished_at=time.time())
        repo.record_event(inspection_id, {"type": "done", "status": "cancelled", "at": time.time()})
        job_manager.wake()
        repo.audit(inspection_id=inspection_id, agent="Operator", phase="ops", action="job.cancel")
        return {"inspection_id": inspection_id, "status": "cancelled"}
    raise InfraScanError("not_cancellable",
                         f"an inspection that already {inspection['status']} cannot be cancelled",
                         status=409, detail={"status": inspection["status"]})


@app.post("/api/inspections/{inspection_id}/retry")
async def retry_inspection(inspection_id: str, data: RetryIn) -> dict[str, Any]:
    _get_inspection(inspection_id)
    outcome = await job_manager.retry(inspection_id, data.mode)
    if outcome == "not_found":
        raise InfraScanError("not_found", "inspection no longer exists", status=404)
    if outcome == "running":
        raise InfraScanError("already_running", "this inspection is still being processed", status=409)
    repo.audit(inspection_id=inspection_id, agent="Operator", phase="ops",
               action="job.retry", mode=data.mode)
    return {"inspection_id": inspection_id, "mode": outcome}


@app.get("/api/inspections/{inspection_id}/events")
async def inspection_events(inspection_id: str, after_seq: int = Query(0, ge=0)) -> StreamingResponse:
    _get_inspection(inspection_id)

    async def encode() -> AsyncIterator[str]:
        async for event in stream_events(inspection_id, after_seq):
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(encode(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


def _contained(path: Path) -> Path:
    resolved = path.resolve()
    roots = [BASE_DIR.resolve(), (ROOT / "samples").resolve()]
    if not any(str(resolved).startswith(str(root)) for root in roots):
        raise InfraScanError("path_rejected", "the requested file is outside the data directory", status=400)
    if not resolved.exists():
        raise InfraScanError("media_missing", "the file is no longer on disk", status=410)
    return resolved


@app.get("/api/inspections/{inspection_id}/media")
def inspection_media(inspection_id: str, frame: int = Query(-1, ge=-1),
                     variant: str = Query("prepared", pattern="^(prepared|source)$")) -> FileResponse:
    """Serve the exact frame the backend segmented, so overlays always sit on real pixels."""
    inspection = _get_inspection(inspection_id)
    if variant == "prepared":
        rows = repo.frames_of(inspection_id)
        wanted = frame if frame >= 0 else (inspection["findings"][0]["source_frame"] if inspection["findings"] else 0)
        match = next((row for row in rows if row["frame_index"] == wanted), None)
        if match is None and rows:
            match = rows[min(wanted, len(rows) - 1)]
        if match is not None and match["path"]:
            return FileResponse(_contained(Path(match["path"])), headers={"Cache-Control": "no-store"})
    if not inspection["source_path"]:
        raise InfraScanError("media_missing", "no source media is stored for this inspection", status=410)
    return FileResponse(_contained(Path(inspection["source_path"])), headers={"Cache-Control": "no-store"})


@app.get("/api/inspections/{inspection_id}/artifacts")
def inspection_artifacts(inspection_id: str) -> list[dict[str, Any]]:
    return _get_inspection(inspection_id)["artifacts"]


@app.get("/api/inspections/{inspection_id}/artifacts/{name}")
def artifact_download(inspection_id: str, name: str) -> FileResponse:
    artifact = repo.get_artifact(inspection_id, name)
    if artifact is None:
        raise InfraScanError("not_found", f"no artifact {name} for this inspection", status=404,
                             detail={"available": [row["name"] for row in _get_inspection(inspection_id)["artifacts"]]})
    repo.audit(inspection_id=inspection_id, agent="Operator", phase="IV", action="artifact.download", name=name)
    return FileResponse(_contained(Path(artifact["path"])), filename=Path(artifact["path"]).name,
                        media_type=artifact["media_type"])


@app.get("/api/inspections/{inspection_id}/report.pdf")
def inspection_report_pdf(inspection_id: str) -> Response:
    """Printable report rendered from the same rows the API serves, at request time."""
    inspection = _get_inspection(inspection_id)
    orders = repo.list_work_orders(inspection_id=inspection_id, page_size=100)["items"]
    body = inspection_pdf(inspection, orders)
    repo.audit(inspection_id=inspection_id, agent="Operator", phase="IV", action="report.pdf",
               bytes=len(body), mode=settings.mode)
    return Response(content=body, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="infrascan_{inspection_id}.pdf"'})


@app.get("/api/inspections/{inspection_id}/audit")
def inspection_audit(inspection_id: str, page: int = Query(1, ge=1),
                     page_size: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    _get_inspection(inspection_id)
    page, page_size = _page(page, page_size)
    return repo.list_audit(inspection_id, page=page, page_size=page_size)


# --------------------------------------------------------------------------- findings


@app.get("/api/findings")
def list_findings(severity: str = "", status: str = "", asset_id: str = "", inspection_id: str = "",
                  search: str = "", needs_review: str = "", page: int = Query(1, ge=1),
                  page_size: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
    page, page_size = _page(page, page_size)
    return repo.list_findings(severity=severity, status=status, asset_id=asset_id,
                              inspection_id=inspection_id, search=search, needs_review=needs_review,
                              page=page, page_size=page_size)


@app.get("/api/findings/{finding_id}")
def get_finding(finding_id: str) -> dict[str, Any]:
    finding = repo.get_finding(finding_id)
    if finding is None:
        raise InfraScanError("not_found", f"no finding {finding_id}", status=404)
    return finding


@app.patch("/api/findings/{finding_id}")
def patch_finding(finding_id: str, data: FindingPatch) -> dict[str, Any]:
    finding = repo.get_finding(finding_id)
    if finding is None:
        raise InfraScanError("not_found", f"no finding {finding_id}", status=404)
    if data.status is not None and data.status != finding["status"]:
        repo.annotate_finding(finding_id, status=data.status)
        repo.audit(inspection_id=finding["inspection_id"], asset_id=finding["asset_id"],
                   agent="Operator", phase="III", action="finding.triage", finding=finding_id,
                   from_status=finding["status"], to_status=data.status, actor=data.actor)
    return repo.get_finding(finding_id) or {}


# ------------------------------------------------------------------------ work orders


@app.get("/api/work-orders")
def list_work_orders(status: str = "", priority: str = "", asset_id: str = "", inspection_id: str = "",
                     search: str = "", page: int = Query(1, ge=1),
                     page_size: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
    page, page_size = _page(page, page_size)
    return repo.list_work_orders(status=status, priority=priority, asset_id=asset_id,
                                 inspection_id=inspection_id, search=search,
                                 page=page, page_size=page_size)


@app.get("/api/work-orders/export")
def export_work_orders(status: str = "", priority: str = "", asset_id: str = "") -> Response:
    payload = _cmms_export(status=status, priority=priority, asset_id=asset_id)
    body = json.dumps(payload, indent=2, default=str).encode()
    filename = f"infrascan_workorders_{time.strftime('%Y%m%d')}.json"
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _cmms_export(*, status: str, priority: str, asset_id: str) -> dict[str, Any]:
    orders = repo.list_work_orders(status=status, priority=priority, asset_id=asset_id,
                                   page=1, page_size=1000)
    return {
        "export": "infrascan.work_orders",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "connection": {
            "cmms": False, "email": False, "sms": False,
            "note": "No external system is wired up in this build. This file is an import payload; "
                    "map the fields onto your CMMS importer.",
        },
        "approval_rule": "Only work orders with status approved or in_progress represent authorised "
                         "maintenance. AI generation never approves anything.",
        "filters": {"status": status, "priority": priority, "asset_id": asset_id},
        "count": orders["total"],
        "items": orders["items"],
    }


@app.post("/api/work-orders", status_code=201)
def create_work_order(data: WorkOrderIn) -> dict[str, Any]:
    inspection = _get_inspection(data.inspection_id)
    finding_ids = list(dict.fromkeys(data.finding_ids))
    rows = {finding_id: repo.get_finding(finding_id) for finding_id in finding_ids}
    found = {key: row for key, row in rows.items() if row is not None}
    missing = [finding_id for finding_id in finding_ids
               if finding_id not in found or not finding_id.startswith(f"{inspection['id']}-")]
    if missing:
        raise InfraScanError("finding_not_found",
                             "one or more findings do not belong to this inspection", status=422,
                             detail={"missing": missing})
    titles = [str(found[finding_id]["concept"]) for finding_id in finding_ids[:3]]
    order = repo.create_work_order(
        inspection_id=inspection["id"],
        asset_id=inspection["asset_id"],
        finding_ids=finding_ids,
        title=data.title or f"Repair {', '.join(titles)}",
        description="Raised manually from " + ", ".join(finding_ids),
        evidence={"finding_ids": finding_ids, "source_media": inspection["source_name"],
                  "raised_by": data.actor},
        priority="P2",
        risk_rationale="",
        recommended_action=[],
        source_mode=inspection["mode"],
        status="pending_approval",
        created_by=data.actor,
    )
    repo.audit(inspection_id=inspection["id"], asset_id=inspection["asset_id"], agent="Operator",
               phase="IV", action="work_order.create", order=order["id"], actor=data.actor,
               findings=finding_ids)
    return order


@app.get("/api/work-orders/{order_id}")
def get_work_order(order_id: str) -> dict[str, Any]:
    order = repo.get_work_order(order_id)
    if order is None:
        raise InfraScanError("not_found", f"no work order {order_id}", status=404)
    return order


@app.patch("/api/work-orders/{order_id}")
def patch_work_order(order_id: str, data: WorkOrderPatch) -> dict[str, Any]:
    order = repo.get_work_order(order_id)
    if order is None:
        raise InfraScanError("not_found", f"no work order {order_id}", status=404)
    if order["status"] in ("completed", "cancelled"):
        raise InfraScanError("locked",
                             f"a {order['status']} work order is part of the record and can no longer be edited",
                             status=409, detail={"status": order["status"]})
    patch = as_json(data)
    patch.pop("actor", None)
    updated = repo.update_work_order(order_id, patch, actor=data.actor) or {}
    repo.audit(inspection_id=order["inspection_id"], asset_id=order["asset_id"], agent="Operator",
               phase="IV", action="work_order.edit", order=order_id, actor=data.actor,
               fields=sorted(patch))
    return updated


@app.post("/api/work-orders/{order_id}/transition")
def transition_work_order(order_id: str, data: TransitionIn) -> dict[str, Any]:
    return _decide(order_id, data.to_status, data.actor, data.note)


@app.post("/api/work-orders/{order_id}/approve")
def approve_work_order(order_id: str, data: DecisionIn) -> dict[str, Any]:
    return _decide(order_id, "approved", data.actor, data.note)


@app.post("/api/work-orders/{order_id}/reject")
def reject_work_order(order_id: str, data: DecisionIn) -> dict[str, Any]:
    return _decide(order_id, "rejected", data.actor, data.note)


def _decide(order_id: str, to_status: str, actor: str, note: str) -> dict[str, Any]:
    order, outcome = repo.transition_work_order(order_id, to_status, actor=actor, note=note)
    if order is None or outcome == "not_found":
        raise InfraScanError("not_found", f"no work order {order_id}", status=404)
    if outcome == "actor_required":
        raise InfraScanError("actor_required", "an approval must name the person giving it", status=422)
    if outcome == "invalid_transition":
        allowed = sorted(WORK_ORDER_TRANSITIONS.get(order["status"], []))
        raise InfraScanError("invalid_transition",
                             f"cannot move a work order from {order['status']} to {to_status}",
                             status=409, detail={"allowed": allowed})
    repo.audit(inspection_id=order["inspection_id"], asset_id=order["asset_id"], agent="Operator",
               phase="IV", action=f"work_order.{to_status}", order=order_id, actor=actor, note=note)
    return order


# ---------------------------------------------------------------------- dashboards


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    stats = repo.dashboard_stats()
    stats["mode"] = settings.mode
    stats["engines"] = {
        "segmentation": settings.sam_model if settings.live else "simulated",
        "reasoning": settings.spark_model if settings.live else "rule engine",
        "live": settings.live,
    }
    stats["stages"] = list(STAGES)
    stats["terminal"] = list(TERMINAL)
    return stats


@app.get("/api/audit")
def audit(action: str = "", inspection_id: str = "", page: int = Query(1, ge=1),
          page_size: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    page, page_size = _page(page, page_size)
    result = repo.list_audit(inspection_id, action=action, page=page, page_size=page_size)
    result["note"] = "Append-only log of real backend events. Credentials are redacted before storage."
    return result
