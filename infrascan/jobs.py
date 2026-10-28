"""Job engine: persisted lifecycle, live progress, cancellation, partial success and retry.

State lives in SQLite, so a page refresh, a dropped SSE connection or a server restart all
recover the same job. The browser only ever sees events the backend actually emitted.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from . import repo
from .config import BASE_DIR, settings
from .db import connect
from .errors import InfraScanError, ProviderError, normalize, redact
from .models import AssetContext, Defect, InspectionContext
from .phases.act import ARTIFACT_MEDIA, ActionAgent
from .phases.detect import VisionAgent, merge_tracks, normalise_prompt
from .phases.ingest import IngestAgent
from .phases.reason import ReasoningAgent
from .simulation import load_ground_truth

OUTPUT_DIR = BASE_DIR / "outputs"

STAGES = ("queued", "validating", "preprocessing", "detecting", "aggregating",
          "reasoning", "generating_report", "completed")
TERMINAL = ("completed", "failed", "cancelled")
STAGE_ORDER = {name: index for index, name in enumerate(STAGES)}


class CancelledError(RuntimeError):
    """Raised internally when an operator cancels a running job."""


class AuditRecorder:
    """Adapts the phase agents' audit calls onto the append-only DB log and the JSONL artifact."""

    def __init__(self, inspection_id: str, asset_id: str) -> None:
        self.inspection_id = inspection_id
        self.asset_id = asset_id
        self.lines: list[dict[str, Any]] = []

    def record(self, *, agent: str = "", phase: str = "", action: str = "", **detail: Any) -> None:
        detail_payload: dict[str, Any] = {key: _truncate(value) for key, value in detail.items()}
        event = {
            "at": time.time(),
            "agent": agent,
            "phase": phase,
            "action": action,
            "detail": detail_payload,
        }
        self.lines.append(event)
        repo.audit(inspection_id=self.inspection_id, asset_id=self.asset_id, agent=agent,
                   phase=phase, action=action, **detail_payload)


def _truncate(value: Any, limit: int = 4000) -> Any:
    if isinstance(value, str):
        return redact(value[:limit])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(json.dumps(value, default=str)[:limit])


class JobManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._wakers: set[asyncio.Event] = set()

    # ------------------------------------------------------------------ control

    def submit(self, inspection_id: str) -> None:
        if self.is_running(inspection_id):
            return
        task = asyncio.create_task(self._supervised(inspection_id), name=f"job-{inspection_id}")
        self._tasks[inspection_id] = task
        task.add_done_callback(self._forget(inspection_id))

    def _forget(self, inspection_id: str):
        def forget(_task: asyncio.Task) -> None:
            self._tasks.pop(inspection_id, None)
        return forget

    def is_running(self, inspection_id: str) -> bool:
        task = self._tasks.get(inspection_id)
        return bool(task and not task.done())

    def request_cancel(self, inspection_id: str) -> str:
        if not self.is_running(inspection_id):
            return "not_running"
        repo.set_inspection(inspection_id, cancel_requested=1)
        self._log(inspection_id, {"type": "log", "level": "warn", "message": "cancellation requested"})
        return "cancelling"

    def wake(self) -> None:
        for event in list(self._wakers):
            event.set()

    def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        self._wakers.add(event)
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        self._wakers.discard(event)

    def _log(self, inspection_id: str, payload: dict[str, Any]) -> None:
        repo.record_event(inspection_id, payload)
        self.wake()

    # ------------------------------------------------------------------ running

    async def _supervised(self, inspection_id: str) -> None:
        try:
            await self.run(inspection_id)
        except CancelledError:
            self._finish(inspection_id, "cancelled", None)
        except Exception as error:  # noqa: BLE001 - a job must always reach a terminal state
            problem = normalize(error)
            self._finish(inspection_id, "failed", problem)

    async def run(self, inspection_id: str) -> None:
        inspection = repo.get_inspection(inspection_id)
        if inspection is None:
            raise InfraScanError("not_found", "inspection no longer exists", status=404)
        asset = repo.get_asset(inspection["asset_id"]) or {}
        context = AssetContext(
            name=asset.get("name", "Unnamed asset"),
            asset_type=asset.get("asset_type", "power_grid"),
            region=asset.get("region", ""),
            latitude=asset.get("latitude"),
            longitude=asset.get("longitude"),
            material=asset.get("material", ""),
            installed_year=asset.get("installed_year"),
            last_inspection=asset.get("last_inspection", ""),
            notes=asset.get("notes", ""),
        )
        audit = AuditRecorder(inspection_id, inspection["asset_id"])
        warnings: list[str] = []

        repo.set_inspection(inspection_id, status="running", started_at=time.time(),
                            cancel_requested=0, error=None, warnings=[])
        self._stage(inspection_id, "queued", status="running")
        audit.record(agent="Orchestrator", phase="0", action="job.start",
                     mode=settings.mode, source=inspection["source_name"],
                     prompts=inspection["prompts"], media_kind=inspection["media_kind"])

        await self._validating(inspection, context, audit)
        frames = await self._preprocessing(inspection, context, audit)
        tracks = await self._detecting(inspection, context, frames, audit, warnings)
        defects = self._aggregating(inspection, tracks, warnings)
        await self._reasoning(inspection, context, defects, audit, warnings)
        await self._reporting(inspection, context, defects, audit)

        self._finish(inspection_id, "completed", None, warnings)
        audit.record(agent="Orchestrator", phase="IV", action="job.complete",
                     findings=len(defects), posture=_posture(defects), warnings=warnings)

    # ------------------------------------------------------------------- stages

    async def _validating(self, inspection: dict[str, Any], asset: AssetContext, audit: AuditRecorder) -> None:
        inspection_id = inspection["id"]
        self._stage(inspection_id, "validating", progress=0.05)
        path = Path(inspection["source_path"])
        if inspection["media_kind"] == "frames":
            stored = repo.frames_of(inspection_id)
            if not stored:
                raise InfraScanError("no_frames", "the inspection has no stored frames to process", status=422)
        elif inspection["media_kind"] == "video":
            if not path.exists():
                raise InfraScanError("media_missing", "the uploaded clip is no longer on disk", status=410)
            if not settings.live:
                raise InfraScanError(
                    "video_requires_key",
                    "native clip segmentation needs a live MODEL_API_KEY; extract frames in the "
                    "browser (the default path here) or configure the key",
                    status=415,
                )
        elif not path.exists():
            raise InfraScanError("media_missing", "the uploaded image is no longer on disk", status=410)
        audit.record(agent="Ingest", phase="I", action="media.validate", media_kind=inspection["media_kind"],
                     source=inspection["source_name"], asset=asset.name)
        self._stage(inspection_id, "validating", status="completed", progress=0.1,
                    summary="media and asset context validated")

    async def _preprocessing(self, inspection: dict[str, Any], asset: AssetContext,
                             audit: AuditRecorder) -> list[dict[str, Any]]:
        inspection_id = inspection["id"]
        self._stage(inspection_id, "preprocessing", progress=0.15)
        agent = IngestAgent()
        out_dir = OUTPUT_DIR / inspection_id

        if inspection["media_kind"] == "video":
            prepared = await asyncio.to_thread(agent.prepare, Path(inspection["source_path"]), out_dir)
            repo.set_inspection(inspection_id, width=0, height=0)
            audit.record(agent="Ingest", phase="I", action="preprocess.clip",
                         size_bytes=prepared.original_size[0], enhancements=prepared.enhancements)
            self._stage(inspection_id, "preprocessing", status="completed", progress=0.25,
                        summary="clip accepted for native per-frame segmentation")
            return [{"path": Path(prepared.path), "width": 0, "height": 0, "frame_index": -1,
                     "native": True, "sidecar": None}]

        stored = (
            [{"frame_index": 0, "path": Path(inspection["source_path"]), "width": inspection["width"],
              "height": inspection["height"], "native": False, "source_name": inspection["source_name"]}]
            if inspection["media_kind"] == "image"
            else [dict(row) for row in repo.frames_of(inspection_id)]
        )

        frames: list[dict[str, Any]] = []
        for position, row in enumerate(stored):
            raw = Path(row["path"])
            prepared = await asyncio.to_thread(agent.prepare, raw, out_dir / f"frame_{row['frame_index']:03d}")
            sidecar = prepared.sidecar
            frames.append({
                "frame_index": row["frame_index"],
                "path": prepared.path,
                "width": prepared.width,
                "height": prepared.height,
                "native": False,
                "sidecar": sidecar,
                "enhancements": prepared.enhancements,
            })
            repo.set_frame_path(inspection_id, row["frame_index"], str(prepared.path),
                                prepared.width, prepared.height, prepared.enhancements)
            self._log(inspection_id, {
                "type": "frame", "stage": "preprocessing", "index": row["frame_index"],
                "total": len(stored), "done": position + 1,
                "size": [prepared.width, prepared.height],
            })
            audit.record(agent="Ingest", phase="I", action="preprocess", engine="local",
                         frame_index=row["frame_index"], size=[prepared.width, prepared.height],
                         enhancements=prepared.enhancements)

        first = frames[0]
        repo.set_inspection(inspection_id, width=first["width"], height=first["height"],
                            frames_total=len(frames), frames_done=0)
        self._stage(inspection_id, "preprocessing", status="completed", progress=0.3,
                    summary=f"{len(frames)} frame(s) normalised to {first['width']}x{first['height']}",
                    geotag=asset.coordinates)
        return frames

    async def _detecting(self, inspection: dict[str, Any], asset: AssetContext,
                         frames: list[dict[str, Any]], audit: AuditRecorder,
                         warnings: list[str]) -> list[dict[str, Any]]:
        inspection_id = inspection["id"]
        prompts = inspection["prompts"] or []
        concepts, notes = _concepts_for(prompts, asset.asset_type)
        for note in notes:
            warnings.append(note)
            self._log(inspection_id, {"type": "log", "level": "warn", "message": note})
        self._stage(inspection_id, "detecting", progress=0.32, summary=f"prompting sam with {len(concepts)} concept(s)")

        agent = VisionAgent(audit)
        per_frame: list[dict[str, Any]] = []

        if frames and frames[0].get("native"):
            result = await agent.run_clip(
                media_path=frames[0]["path"], concepts=concepts, inspection_id=inspection_id
            )
            per_frame = result["frames"]
            self._log(inspection_id, {"type": "log", "level": "info",
                                      "message": f"clip segmented natively: {len(per_frame)} frame lines returned"})
        else:
            total = len(frames)
            raw_lanes = []
            for position, frame in enumerate(frames):
                self._check_cancel(inspection_id)
                result = await agent.run_frame(
                    media_path=frame["path"], width=frame["width"], height=frame["height"],
                    concepts=concepts, frame_index=frame["frame_index"],
                    inspection_id=inspection_id,
                    ground_truth=load_ground_truth(frame.get("sidecar")),
                )
                per_frame.append(result)
                raw_lanes.append(result["raw_detections"])
                repo.set_inspection(inspection_id, frames_done=position + 1)
                self._log(inspection_id, {
                    "type": "frame", "stage": "detecting", "index": frame["frame_index"],
                    "total": total, "done": position + 1,
                    "detections": result["raw_detections"], "regions": len(result["groups"]),
                })
                self._stage(inspection_id, "detecting",
                            progress=0.32 + 0.33 * (position + 1) / max(1, total))

        if not per_frame:
            raise InfraScanError("no_media_returned", "the vision stage produced no frames", status=502)
        repo.set_inspection(
            inspection_id,
            sam_engine=settings.sam_model if settings.live else "simulated sam-3.1",
        )
        regions = sum(len(frame["groups"]) for frame in per_frame)
        raw = sum(frame.get("raw_detections", 0) for frame in per_frame)
        self._stage(inspection_id, "detecting", status="completed", progress=0.65,
                    summary=f"{raw} raw detections resolved into {regions} region(s) "
                            f"across {len(per_frame)} frame(s)",
                    concepts=concepts)
        return per_frame

    def _aggregating(self, inspection: dict[str, Any], per_frame: list[dict[str, Any]],
                     warnings: list[str]) -> list[Defect]:
        inspection_id = inspection["id"]
        self._stage(inspection_id, "aggregating", progress=0.7)
        tracks = merge_tracks(per_frame)
        multi = [track for track in tracks if len(track["frames_seen"]) > 1]
        if multi:
            warnings_note = f"{len(multi)} region(s) seen across frames were merged into single defects"
            self._log(inspection_id, {"type": "log", "level": "info", "message": warnings_note})

        defects: list[Defect] = []
        for index, track in enumerate(tracks, start=1):
            detection: Any = track["primary"]
            defect = Defect(
                defect_id=f"F{index:03d}",
                concept=track["concept"],
                frame_index=track["frame_index"],
                box=(detection.left, detection.top, detection.right, detection.bottom),
                source_size=(detection.source_width, detection.source_height),
                confidence=track["confidence"],
                agreement=track["agreement"],
                corroborated_by=track["corroborated_by"],
                frames_seen=sorted(set(track["frames_seen"])),
                mask_svg=track["mask_svg"],
                mask_origin=(detection.left, detection.top),
                mask_size=(detection.mask_width, detection.mask_height),
                mask_fill=track["mask_fill"],
                detections=track["detection_count"],
                area_m2=_area(track, inspection["pixels_per_meter"]),
            )
            defects.append(defect)

        repo.replace_findings(inspection_id, inspection["asset_id"],
                              [_finding_row(defect) for defect in defects],
                              settings.sam_model if settings.live else "simulated sam-3.1")
        summary = (f"{len(defects)} distinct defect(s) after IoU consensus and frame merging "
                   f"({len(per_frame)} frame(s) analysed)")
        self._stage(inspection_id, "aggregating", status="completed", progress=0.75, summary=summary)
        return defects

    async def _reasoning(self, inspection: dict[str, Any], asset: AssetContext, defects: list[Defect],
                         audit: AuditRecorder, warnings: list[str]) -> None:
        inspection_id = inspection["id"]
        if not defects:
            self._stage(inspection_id, "reasoning", status="completed", progress=0.85,
                        summary="nothing to reason about: no defect region survived consensus")
            return
        self._stage(inspection_id, "reasoning", progress=0.8)
        agent = ReasoningAgent(audit)
        try:
            output = await agent.run(defects, asset, pixels_per_meter=inspection["pixels_per_meter"])
        except ProviderError as error:
            # Partial success: detections are already stored, reasoning can be retried.
            warning = f"contextual reasoning failed ({error.detail.get('kind', 'provider')}); detections preserved - retry reasoning when the provider is healthy"
            warnings.append(warning)
            self._log(inspection_id, {"type": "log", "level": "error", "message": warning})
            audit.record(agent=agent.name, phase="III", action="reasoning.failed",
                         kind=error.detail.get("kind"), message=error.message, retryable=error.retryable)
            for defect in defects:
                defect.needs_human_review = True
                if not defect.uncertainties:
                    defect.uncertainties = ["no contextual assessment was returned by the reasoning model"]
            repo.replace_findings(inspection_id, inspection["asset_id"],
                                  [_finding_row(defect, engine="") for defect in defects],
                                  settings.sam_model if settings.live else "simulated sam-3.1")
            repo.set_inspection(inspection_id, warnings=warnings, spark_engine="unavailable")
            self._stage(inspection_id, "reasoning", status="failed", progress=0.85, summary=warning,
                        escalated=len(defects))
            return

        repo.set_inspection(inspection_id, spark_engine=output["engine"])
        repo.replace_findings(inspection_id, inspection["asset_id"],
                              [_finding_row(defect, reasoned=defect.reasoned,
                                            engine=output["engine"] if defect.reasoned else "")
                               for defect in defects],
                              settings.sam_model if settings.live else "simulated sam-3.1")
        if not output["documents"]:
            warnings.append("no control document matched this asset; severity is geometry-based only")
        if not output["complete"]:
            warnings.append("the reasoning model returned verdicts for only part of the findings")
        summary = (
            f"correlated against {len(output['documents'])} corpus document(s) "
            f"({output['corpus_words']} words of standards and history)"
            if output["documents"] else
            "no corpus documents matched; findings left uncontextualised"
        )
        self._stage(inspection_id, "reasoning", status="completed", progress=0.9, summary=summary,
                    documents=output["documents"], escalated=sum(1 for d in defects if d.needs_human_review))
        self._log(inspection_id, {"type": "findings", "count": len(defects)})

    async def _reporting(self, inspection: dict[str, Any], asset: AssetContext, defects: list[Defect],
                         audit: AuditRecorder) -> None:
        inspection_id = inspection["id"]
        self._stage(inspection_id, "generating_report", progress=0.92)
        context = InspectionContext(
            inspection_id=inspection_id,
            asset=asset,
            width=inspection["width"],
            height=inspection["height"],
            mode=settings.mode,
            defects=defects,
            pixels_per_meter=inspection["pixels_per_meter"],
            media_kind=inspection["media_kind"],
            source_name=inspection["source_name"],
            frames_total=max(1, inspection["frames_total"]),
            engines={
                "preprocessing": "local",
                "segmentation": settings.sam_model if settings.live else "simulated sam-3.1",
                "reasoning": inspection["spark_engine"] or ("simulated rule engine" if not settings.live else "unavailable"),
                "artifacts": ActionAgent(audit).engine,
            },
        )
        agent = ActionAgent(audit)
        artifacts = await agent.run(context, OUTPUT_DIR / inspection_id, audit_lines=audit.lines)
        for name, path in artifacts.items():
            repo.put_artifact(inspection_id, name=name, label=_label(name), path=str(path),
                              media_type=ARTIFACT_MEDIA.get(name, "application/octet-stream"),
                              size_bytes=path.stat().st_size)
        created, skipped = self._register_work_orders(inspection, defects)
        note = (f"{skipped} finding(s) left unassessed by the reasoning stage, so no work order was raised for them"
                if skipped else "")
        self._stage(inspection_id, "generating_report", status="completed", progress=1.0,
                    summary=f"{len(artifacts)} artifact(s) written, {created} work order(s) raised for review"
                            + (f"; {note}" if note else ""))
        if note:
            self._log(inspection_id, {"type": "log", "level": "warn", "message": note})

    def _register_work_orders(self, inspection: dict[str, Any], defects: list[Defect]) -> tuple[int, int]:
        """Draft work orders from assessed findings. Nothing is ever approved automatically."""
        inspection_id = inspection["id"]
        if repo.list_work_orders(inspection_id=inspection_id, page_size=1)["total"]:
            return 0, 0
        assessed = [defect for defect in defects if defect.reasoned]
        created = 0
        for defect in assessed:
            finding_id = f"{inspection_id}-{defect.defect_id}"
            if repo.get_finding(finding_id) is None:
                continue
            status = "pending_approval" if (
                defect.needs_human_review or defect.severity in ("High", "Critical")
            ) else "draft"
            repo.create_work_order(
                inspection_id=inspection_id,
                asset_id=inspection["asset_id"],
                finding_ids=[finding_id],
                title=f"{defect.severity} {defect.concept} on {inspection['asset_name']}",
                description=defect.analysis or defect.cause,
                evidence={
                    "finding_id": finding_id,
                    "box_px": list(defect.box),
                    "frames_seen": defect.frames_seen,
                    "documents": defect.references,
                    "source_media": inspection["source_name"],
                    "confidence": defect.confidence,
                    "agreement": defect.agreement,
                },
                priority=defect.priority,
                risk_rationale=defect.cause,
                recommended_action=defect.recommendations,
                source_mode=settings.mode,
                status=status,
            )
            created += 1
        return created, len(defects) - created

    # ------------------------------------------------------------------ plumbing

    def _check_cancel(self, inspection_id: str) -> None:
        with connect() as db:
            row = db.execute("SELECT cancel_requested FROM inspections WHERE id = ?", (inspection_id,)).fetchone()
        if row and row["cancel_requested"]:
            raise CancelledError("cancelled by operator")

    def _stage(self, inspection_id: str, stage: str, *, status: str = "running",
               progress: float | None = None, summary: str = "", **metrics: Any) -> None:
        if status == "running":
            repo.set_inspection(inspection_id, stage=stage, status="running")
        if progress is not None:
            repo.set_inspection(inspection_id, stage_progress=progress)
        payload = {
            "type": "stage", "stage": stage, "status": status, "progress": progress,
            "summary": summary, "metrics": metrics, "at": time.time(),
            "elapsed_ms": self._elapsed(inspection_id),
        }
        repo.record_event(inspection_id, payload)
        self.wake()

    def _elapsed(self, inspection_id: str) -> int | None:
        inspection = repo.get_inspection(inspection_id)
        if not inspection or not inspection.get("started_at"):
            return None
        return round((time.time() - inspection["started_at"]) * 1000)

    def _finish(self, inspection_id: str, status: str, error: InfraScanError | None,
                warnings: list[str] | None = None) -> None:
        payload: dict[str, Any] = {"status": status, "finished_at": time.time()}
        if status == "completed":
            payload["stage"] = "completed"
        if error is not None:
            payload["error"] = json.dumps({"code": error.code, "message": error.message,
                                           "detail": error.detail}, default=str)
        if warnings is not None:
            payload["warnings"] = warnings
        repo.set_inspection(inspection_id, **payload)
        repo.record_event(inspection_id, {
            "type": "done", "status": status, "at": time.time(),
            "error": {"code": error.code, "message": error.message} if error else None,
        })
        repo.audit(inspection_id=inspection_id, agent="Orchestrator", phase="end",
                   action=f"job.{status}", **({} if error is None else {"code": error.code, "message": error.message}))
        self.wake()

    # --------------------------------------------------------------------- retry

    async def retry(self, inspection_id: str, mode: str = "full") -> str:
        inspection = repo.get_inspection(inspection_id)
        if inspection is None:
            return "not_found"
        if self.is_running(inspection_id):
            return "running"
        if mode == "reasoning":
            task = asyncio.create_task(self._supervised_retry(inspection), name=f"retry-{inspection_id}")
            self._tasks[inspection_id] = task
            task.add_done_callback(self._forget(inspection_id))
            return "reasoning"
        repo.set_inspection(inspection_id, status="queued", stage="queued", stage_progress=0,
                            error=None, warnings=[], cancel_requested=0, finished_at=None,
                            frames_done=0)
        self.submit(inspection_id)
        return "full"

    async def _supervised_retry(self, inspection: dict[str, Any]) -> None:
        try:
            await self._retry_reasoning(inspection)
        except CancelledError:
            self._finish(inspection["id"], "cancelled", None)
        except Exception as error:  # noqa: BLE001 - a retry must also reach a terminal state
            self._finish(inspection["id"], "failed", normalize(error))

    async def _retry_reasoning(self, inspection: dict[str, Any]) -> None:
        """Re-run only the stage that failed, keeping every stored detection."""
        inspection_id = inspection["id"]
        defects = [_defect_from_row(row) for row in inspection["findings"]]
        asset = AssetContext(
            name=inspection["asset_name"], asset_type=inspection["asset_type"],
            region=inspection["region"] or "", latitude=inspection["latitude"],
            longitude=inspection["longitude"], material=inspection["material"] or "",
        )
        audit = AuditRecorder(inspection_id, inspection["asset_id"])
        repo.set_inspection(inspection_id, status="running", error=None)
        warnings = list(inspection["warnings"])
        self._stage(inspection_id, "reasoning", progress=0.8, summary="retrying contextual reasoning")
        await self._reasoning(inspection, asset, defects, audit, warnings)
        await self._reporting(inspection, asset, defects, audit)
        self._finish(inspection_id, "completed", None, warnings)
        repo.audit(inspection_id=inspection_id, agent="Operator", phase="III", action="reasoning.retry",
                   findings=len(defects))


job_manager = JobManager()


# ------------------------------------------------------------------------- helpers


def _concepts_for(prompts: list[str], asset_type: str) -> tuple[list[str], list[str]]:
    from .phases.detect import concepts_for

    notes: list[str] = []
    concepts: list[str] = []
    for prompt in prompts:
        concept, note = normalise_prompt(prompt)
        if note:
            notes.append(note)
        if concept and concept not in concepts:
            concepts.append(concept)
    if not concepts:
        concepts = concepts_for(asset_type)
        notes.append(f"no usable prompt supplied; defaulted to the {asset_type} concept set")
    return concepts[:8], notes


def _area(track: dict[str, Any], pixels_per_meter: float | None) -> float | None:
    if not pixels_per_meter:
        return None
    detection = track["primary"]
    width_m = (detection.right - detection.left) / pixels_per_meter
    height_m = (detection.bottom - detection.top) / pixels_per_meter
    return round(width_m * height_m * max(track["mask_fill"], 0.05), 4)


def _finding_row(defect: Defect, *, reasoned: bool = False, engine: str = "") -> dict[str, Any]:
    return {
        "id": defect.defect_id,
        "source_frame": defect.frame_index,
        "concept": defect.concept,
        "prompt": defect.concept,
        "box": list(defect.box),
        "mask": {"svg": defect.mask_svg, "origin": list(defect.mask_origin),
                 "size": list(defect.mask_size), "fill": defect.mask_fill},
        "source_size": list(defect.source_size),
        "confidence": defect.confidence,
        "agreement": defect.agreement,
        "corroborated_by": defect.corroborated_by,
        "detections": defect.detections,
        "frames_seen": defect.frames_seen,
        "area_m2": defect.area_m2,
        "severity": defect.severity,
        "priority": defect.priority,
        "cause": defect.cause,
        "analysis": defect.analysis,
        "consequences": defect.consequences,
        "recommendations": defect.recommendations,
        "references": defect.references,
        "uncertainties": defect.uncertainties,
        "needs_human_review": defect.needs_human_review,
        "reasoning_done": reasoned,
        "reasoning_engine": engine,
    }


def _defect_from_row(row: dict[str, Any]) -> Defect:
    mask = row["mask"] if isinstance(row["mask"], dict) else {}
    return Defect(
        defect_id=row["id"].rsplit("-", 1)[-1],
        concept=row["concept"],
        frame_index=row["source_frame"],
        box=tuple(row["box"] or (0, 0, 0, 0)),
        source_size=tuple(row["source_size"] or (0, 0)),
        confidence=row["confidence"],
        agreement=row["agreement"],
        corroborated_by=list(row["corroborated_by"] or []),
        frames_seen=list(row["frames_seen"] or []),
        mask_svg=mask.get("svg", ""),
        mask_origin=tuple(mask.get("origin", (0, 0))),
        mask_size=tuple(mask.get("size", (0, 0))),
        mask_fill=mask.get("fill", 0.0),
        detections=row["detections"],
        area_m2=row["area_m2"],
        severity=row["severity"],
        priority=row["priority"],
        cause=row["cause"],
        analysis=row["analysis"],
        consequences=list(row["consequences"] or []),
        recommendations=list(row["recommendations"] or []),
        references=list(row["references_doc"] or []),
        uncertainties=list(row["uncertainties"] or []),
        needs_human_review=bool(row["needs_human_review"]),
        reasoned=bool(row["reasoning_done"]),
    )


def _posture(defects: list[Defect]) -> str:
    context = InspectionContext(inspection_id="", asset=AssetContext(), width=0, height=0,
                                mode=settings.mode, defects=defects)
    return context.risk_posture


def _label(name: str) -> str:
    return {
        "work_orders": "Work orders (CMMS export)",
        "dispatch_payload": "Dispatch payload",
        "inspection_report": "Inspection report",
        "robotic_arm_script": "Robotic arm target script",
        "audit_log": "Audit trail (JSONL)",
    }.get(name, name)


async def stream_events(inspection_id: str, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
    """Replay persisted events, then follow the job until it reaches a terminal state."""
    wake = job_manager.subscribe()
    cursor = after_seq
    try:
        while True:
            batch = repo.events_after(inspection_id, cursor)
            for event in batch:
                cursor = event["seq"]
                yield event
            if batch and batch[-1].get("type") == "done":
                return
            inspection = repo.get_inspection(inspection_id)
            if inspection is None:
                return
            if inspection["status"] in TERMINAL and not batch:
                yield {"type": "done", "status": inspection["status"], "seq": cursor + 1}
                return
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=1.0)
            except TimeoutError:
                pass
    finally:
        job_manager.unsubscribe(wake)
