"""Typed repository helpers over the SQLite layer. Everything the API persists goes through here."""
from __future__ import annotations

import time
import uuid
from typing import Any

from .db import connect, dump, load

INSPECTION_JSON = ("prompts", "warnings")
FINDING_JSON = ("box", "mask", "source_size", "corroborated_by", "frames_seen", "consequences",
                "recommendations", "references_doc", "uncertainties")
WORK_ORDER_JSON = ("finding_ids", "evidence", "recommended_action")
BOOL_COLUMNS = ("needs_human_review", "reasoning_done", "archived", "is_sample",
                "cancel_requested", "configured", "verified_ok")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _row(row: Any, json_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    data = dict(row)
    for field in json_fields:
        if field in data:
            target: Any = {} if isinstance(data[field], str) and data[field].lstrip().startswith("{") else []
            data[field] = load(data[field], target)
    for field in BOOL_COLUMNS:
        if isinstance(data.get(field), int):
            data[field] = bool(data[field])
    return data


def _rows(rows: list[Any], json_fields: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [_row(row, json_fields) for row in rows]


# ----------------------------------------------------------------------- assets


def create_asset(*, name: str, asset_type: str, region: str = "", latitude: float | None = None,
                 longitude: float | None = None, material: str = "", installed_year: int | None = None,
                 last_inspection: str = "", notes: str = "", is_sample: int = 0) -> dict[str, Any]:
    asset_id = _new_id("A")
    now = time.time()
    with connect() as db:
        db.execute(
            "INSERT INTO assets (id, name, asset_type, region, latitude, longitude, material,"
            " installed_year, last_inspection, notes, is_sample, archived, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (asset_id, name, asset_type, region, latitude, longitude, material, installed_year,
             last_inspection, notes, is_sample, now, now),
        )
    return get_asset(asset_id) or {}


def get_asset(asset_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            return None
        data = _row(row)
        data["inspection_count"] = db.execute(
            "SELECT COUNT(*) c FROM inspections WHERE asset_id = ?", (asset_id,)
        ).fetchone()["c"]
        data["open_findings"] = db.execute(
            "SELECT COUNT(*) c FROM findings WHERE asset_id = ? AND status = 'open'", (asset_id,)
        ).fetchone()["c"]
    return data


def list_assets(*, search: str = "", asset_type: str = "", status: str = "active",
                sort: str = "updated_desc", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    order = {
        "updated_desc": "a.updated_at DESC", "updated_asc": "a.updated_at ASC",
        "name_asc": "a.name COLLATE NOCASE ASC", "name_desc": "a.name COLLATE NOCASE DESC",
        "inspections_desc": "inspections DESC",
    }.get(sort, "a.updated_at DESC")

    where, params = [], []
    if search:
        where.append("(a.name LIKE ? OR a.region LIKE ? OR a.material LIKE ? OR a.notes LIKE ?)")
        params += [f"%{search}%"] * 4
    if asset_type:
        where.append("a.asset_type = ?")
        params.append(asset_type)
    if status == "active":
        where.append("a.archived = 0")
    elif status == "archived":
        where.append("a.archived = 1")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sort_sql = order if "inspections" not in order else "inspections DESC"

    with connect() as db:
        base = (
            "SELECT a.*, ("
            "  SELECT COUNT(*) FROM inspections i WHERE i.asset_id = a.id"
            ") AS inspections FROM assets a " + clause
        )
        total = db.execute(f"SELECT COUNT(*) c FROM ({base})", tuple(params)).fetchone()["c"]
        rows = db.execute(
            f"{base} ORDER BY {sort_sql} LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": _rows(rows), "total": total, "page": page, "page_size": page_size,
            "pages": max(1, -(-total // page_size))}


def update_asset(asset_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"name", "asset_type", "region", "latitude", "longitude", "material",
               "installed_year", "last_inspection", "notes", "archived"}
    fields = {key: value for key, value in patch.items() if key in allowed}
    if not fields:
        return get_asset(asset_id)
    sets = ", ".join(f"{key} = ?" for key in fields)
    with connect() as db:
        db.execute(
            f"UPDATE assets SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), time.time(), asset_id),
        )
    return get_asset(asset_id)


def delete_asset(asset_id: str, *, cascade: bool = False) -> str:
    with connect() as db:
        count = db.execute("SELECT COUNT(*) c FROM inspections WHERE asset_id = ?", (asset_id,)).fetchone()["c"]
        if count and not cascade:
            return "blocked"
        db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    return "deleted"


def seed_sample_assets(samples: list[dict[str, Any]]) -> int:
    """Deterministic demo assets, flagged is_sample so the UI can label them."""
    created = 0
    with connect() as db:
        for sample in samples:
            exists = db.execute("SELECT 1 FROM assets WHERE name = ? AND is_sample = 1", (sample["name"],)).fetchone()
            if exists:
                continue
            now = time.time()
            db.execute(
                "INSERT INTO assets (id, name, asset_type, region, latitude, longitude, material,"
                " installed_year, last_inspection, notes, is_sample, archived, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                (_new_id("A"), sample["name"], sample["asset_type"], sample["region"],
                 sample.get("latitude"), sample.get("longitude"), sample.get("material", ""),
                 sample.get("installed_year"), sample.get("last_inspection", ""),
                 sample.get("notes", ""), now, now),
            )
            created += 1
    return created


# ------------------------------------------------------------------- inspections


def find_by_idempotency_key(key: str) -> dict[str, Any] | None:
    if not key:
        return None
    with connect() as db:
        row = db.execute("SELECT id FROM inspections WHERE idempotency_key = ?", (key,)).fetchone()
    return get_inspection(row["id"]) if row else None


def create_inspection(*, asset_id: str, title: str, prompts: list[str], media_kind: str,
                      source_name: str, source_path: str, pixels_per_meter: float | None, mode: str,
                      frames_total: int = 1,
                      idempotency_key: str | None = None) -> dict[str, Any]:
    inspection_id = _new_id("I")
    now = time.time()
    with connect() as db:
        if idempotency_key:
            existing = db.execute(
                "SELECT id FROM inspections WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return get_inspection(existing["id"]) or {}
        db.execute(
            "INSERT INTO inspections (id, asset_id, title, prompts, media_kind, source_name, source_path,"
            " pixels_per_meter, status, stage, mode, frames_total, idempotency_key, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,'queued','queued',?,?,?,?,?)",
            (inspection_id, asset_id, title, dump(prompts), media_kind, source_name, source_path,
             pixels_per_meter, mode, frames_total, idempotency_key, now, now),
        )
    return get_inspection(inspection_id) or {}


def get_inspection(inspection_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT i.*, a.name AS asset_name, a.asset_type, a.region, a.latitude, a.longitude,"
            " a.material FROM inspections i JOIN assets a ON a.id = i.asset_id WHERE i.id = ?",
            (inspection_id,),
        ).fetchone()
        if row is None:
            return None
        data = _row(row, INSPECTION_JSON)
        data["findings"] = _rows(
            db.execute("SELECT * FROM findings WHERE inspection_id = ? ORDER BY id", (inspection_id,)).fetchall(),
            FINDING_JSON,
        )
        data["frames"] = _rows(
            db.execute("SELECT * FROM frames WHERE inspection_id = ? ORDER BY frame_index", (inspection_id,)).fetchall(),
            ("enhancements",),
        )
        data["artifacts"] = _rows(db.execute(
            "SELECT name, label, path, media_type, bytes, created_at FROM artifacts"
            " WHERE inspection_id = ? ORDER BY name", (inspection_id,)
        ).fetchall())
    return data


def list_inspections(*, asset_id: str = "", status: str = "", search: str = "",
                     page: int = 1, page_size: int = 20) -> dict[str, Any]:
    where, params = [], []
    if asset_id:
        where.append("i.asset_id = ?")
        params.append(asset_id)
    if status:
        where.append("i.status = ?")
        params.append(status)
    if search:
        where.append("(i.title LIKE ? OR a.name LIKE ? OR i.id LIKE ?)")
        params += [f"%{search}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as db:
        base = f"SELECT i.*, a.name AS asset_name, a.asset_type FROM inspections i JOIN assets a ON a.id = i.asset_id {clause}"
        total = db.execute(f"SELECT COUNT(*) c FROM ({base})", tuple(params)).fetchone()["c"]
        rows = db.execute(
            f"{base} ORDER BY i.created_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": _rows(rows, INSPECTION_JSON), "total": total, "page": page,
            "page_size": page_size, "pages": max(1, -(-total // page_size))}


def delete_inspection(inspection_id: str) -> bool:
    """Drop an inspection row; frames, findings, events, artifacts and work orders cascade."""
    with connect() as db:
        cursor = db.execute("DELETE FROM inspections WHERE id = ?", (inspection_id,))
    return cursor.rowcount > 0


def set_inspection(inspection_id: str, **fields: Any) -> None:
    """Whitelisted single-statement update used by the job engine."""
    allowed = {"status", "stage", "stage_progress", "sam_engine", "spark_engine", "frames_total",
               "frames_done", "width", "height", "error", "warnings", "cancel_requested", "started_at",
               "finished_at", "media_kind", "source_name", "source_path", "prompts", "title", "mode",
               "pixels_per_meter"}
    payload = {key: (dump(value) if key in {"prompts", "warnings"} and isinstance(value, list) else value)
               for key, value in fields.items() if key in allowed}
    if not payload:
        return
    payload["updated_at"] = time.time()
    sets = ", ".join(f"{key} = ?" for key in payload)
    with connect() as db:
        db.execute(f"UPDATE inspections SET {sets} WHERE id = ?", (*payload.values(), inspection_id))


def add_frame(inspection_id: str, *, frame_index: int, path: str, width: int, height: int,
              source_time_ms: int = 0, enhancements: list[str] | None = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO frames (inspection_id, frame_index, source_time_ms, path, width, height,"
            " enhancements, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (inspection_id, frame_index, source_time_ms, path, width, height,
             dump(enhancements or []), time.time()),
        )


def frames_of(inspection_id: str) -> list[dict[str, Any]]:
    with connect() as db:
        return _rows(
            db.execute("SELECT * FROM frames WHERE inspection_id = ? ORDER BY frame_index", (inspection_id,)).fetchall(),
            ("enhancements",),
        )


def put_artifact(inspection_id: str, *, name: str, label: str, path: str, media_type: str,
                 size_bytes: int) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO artifacts (inspection_id, name, label, path, media_type, bytes, created_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT (inspection_id, name) DO UPDATE SET path = excluded.path,"
            " label = excluded.label, media_type = excluded.media_type, bytes = excluded.bytes,"
            " created_at = excluded.created_at",
            (inspection_id, name, label, path, media_type, size_bytes, time.time()),
        )


def get_artifact(inspection_id: str, name: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM artifacts WHERE inspection_id = ? AND name = ?", (inspection_id, name)
        ).fetchone()
    return dict(row) if row else None


def set_frame_path(inspection_id: str, frame_index: int, path: str, width: int, height: int,
                   enhancements: list[str]) -> None:
    with connect() as db:
        db.execute(
            "UPDATE frames SET path = ?, width = ?, height = ?, enhancements = ?"
            " WHERE inspection_id = ? AND frame_index = ?",
            (path, width, height, dump(enhancements), inspection_id, frame_index),
        )


def record_event(inspection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a job event so a reconnecting browser can replay real backend progress."""
    with connect() as db:
        seq = (db.execute("SELECT MAX(seq) m FROM job_events WHERE inspection_id = ?", (inspection_id,)).fetchone()["m"] or 0) + 1
        db.execute(
            "INSERT INTO job_events (inspection_id, seq, payload, created_at) VALUES (?,?,?,?)",
            (inspection_id, seq, dump(payload), time.time()),
        )
    return {**payload, "seq": seq, "inspection_id": inspection_id}


def events_after(inspection_id: str, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT seq, payload, created_at FROM job_events WHERE inspection_id = ? AND seq > ?"
            " ORDER BY seq LIMIT ?",
            (inspection_id, after_seq, limit),
        ).fetchall()
    out = []
    for row in rows:
        payload = load(row["payload"], {})
        if isinstance(payload, dict):
            out.append({**payload, "seq": row["seq"], "at": row["created_at"]})
    return out


def last_activity(minutes: int = 60) -> list[dict[str, Any]]:
    with connect() as db:
        return _rows(
            db.execute(
                "SELECT a.id, a.inspection_id, a.action, a.agent, a.detail, a.created_at"
                " FROM audit_events a WHERE a.created_at > ? ORDER BY a.id DESC LIMIT 30",
                (time.time() - minutes * 60,),
            ).fetchall(),
        )


# ---------------------------------------------------------------------- findings


def replace_findings(inspection_id: str, asset_id: str, records: list[dict[str, Any]],
                     detection_engine: str) -> None:
    now = time.time()
    with connect() as db:
        existing = {
            row["id"]: row for row in db.execute(
                "SELECT id, frames_seen, detections FROM findings WHERE inspection_id = ?", (inspection_id,)
            ).fetchall()
        }
        for record in records:
            finding_id = f"{inspection_id}-{record['id']}"
            payload = {
                "source_frame": record.get("source_frame", 0),
                "concept": record.get("concept", ""),
                "prompt": record.get("prompt", ""),
                "box": dump(record.get("box", [])),
                "mask": dump(record.get("mask", {})),
                "source_size": dump(record.get("source_size", [])),
                "confidence": record.get("confidence", 0.0),
                "agreement": record.get("agreement", 0.0),
                "corroborated_by": dump(record.get("corroborated_by", [])),
                "detections": record.get("detections", 1),
                "frames_seen": dump(record.get("frames_seen", [])),
                "area_m2": record.get("area_m2"),
                "severity": record.get("severity", "Unknown"),
                "priority": record.get("priority", "P2"),
                "cause": record.get("cause", ""),
                "analysis": record.get("analysis", ""),
                "consequences": dump(record.get("consequences", [])),
                "recommendations": dump(record.get("recommendations", [])),
                "references_doc": dump(record.get("references", [])),
                "uncertainties": dump(record.get("uncertainties", [])),
                "needs_human_review": 1 if record.get("needs_human_review") else 0,
                "reasoning_done": 1 if record.get("reasoning_done") else 0,
                "detection_engine": detection_engine,
                "reasoning_engine": record.get("reasoning_engine", ""),
                "updated_at": now,
            }
            columns = ", ".join(f"{key} = ?" for key in payload)
            if finding_id in existing:
                db.execute(f"UPDATE findings SET {columns} WHERE id = ?", (*payload.values(), finding_id))
            else:
                db.execute(
                    "INSERT INTO findings (id, inspection_id, asset_id, status, created_at,"
                    f" {', '.join(payload)}) VALUES (?,?,?, 'open', ?,{','.join('?' * len(payload))})",
                    (finding_id, inspection_id, asset_id, now, *payload.values()),
                )
        keep = {f"{inspection_id}-{record['id']}" for record in records}
        for stale in existing.keys() - keep:
            db.execute("DELETE FROM findings WHERE id = ?", (stale,))


def annotate_finding(finding_id: str, **fields: Any) -> None:
    allowed = {"severity", "priority", "cause", "analysis", "consequences", "recommendations",
               "references", "uncertainties", "needs_human_review", "reasoning_done",
               "reasoning_engine", "area_m2", "status"}
    aliases = {"references": "references_doc"}
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        column = aliases.get(key, key)
        if column not in allowed | set(FINDING_JSON):
            continue
        if isinstance(value, list):
            value = dump(value)
        elif isinstance(value, bool):
            value = 1 if value else 0
        payload[column] = value
    if not payload:
        return
    payload["updated_at"] = time.time()
    sets = ", ".join(f"{key} = ?" for key in payload)
    with connect() as db:
        db.execute(f"UPDATE findings SET {sets} WHERE id = ?", (*payload.values(), finding_id))


def get_finding(finding_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT f.*, a.name AS asset_name, a.asset_type, i.title AS inspection_title,"
            " i.mode AS inspection_mode, i.source_name FROM findings f"
            " JOIN assets a ON a.id = f.asset_id JOIN inspections i ON i.id = f.inspection_id"
            " WHERE f.id = ?",
            (finding_id,),
        ).fetchone()
    return _row(row, FINDING_JSON) if row else None


def list_findings(*, severity: str = "", status: str = "", asset_id: str = "", inspection_id: str = "",
                  search: str = "", needs_review: str = "", page: int = 1, page_size: int = 25) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    for column, value in (("f.severity", severity), ("f.status", status), ("f.asset_id", asset_id),
                          ("f.inspection_id", inspection_id)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if needs_review in ("0", "1"):
        where.append("f.needs_human_review = ?")
        params.append(int(needs_review))
    if search:
        where.append("(f.concept LIKE ? OR f.cause LIKE ? OR f.analysis LIKE ? OR a.name LIKE ?)")
        params += [f"%{search}%"] * 4
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as db:
        base = (f"SELECT f.*, a.name AS asset_name, a.asset_type, i.mode AS inspection_mode"
                f" FROM findings f JOIN assets a ON a.id = f.asset_id"
                f" JOIN inspections i ON i.id = f.inspection_id {clause}")
        total = db.execute(f"SELECT COUNT(*) c FROM ({base})", tuple(params)).fetchone()["c"]
        rows = db.execute(
            f"{base} ORDER BY CASE f.severity WHEN 'Critical' THEN 0 WHEN 'High' THEN 1"
            " WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END, f.created_at DESC"
            " LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": _rows(rows, FINDING_JSON), "total": total, "page": page,
            "page_size": page_size, "pages": max(1, -(-total // page_size))}


# -------------------------------------------------------------------- work orders

WORK_ORDER_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_approval", "cancelled"},
    "pending_approval": {"approved", "rejected", "cancelled"},
    "approved": {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "rejected": {"draft", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def create_work_order(*, inspection_id: str, asset_id: str, finding_ids: list[str], title: str,
                      description: str, evidence: dict[str, Any], priority: str, risk_rationale: str,
                      recommended_action: list[str], source_mode: str, status: str = "draft",
                      created_by: str = "InfraScan 3D") -> dict[str, Any]:
    order_id = _new_id("WO")
    now = time.time()
    with connect() as db:
        db.execute(
            "INSERT INTO work_orders (id, inspection_id, asset_id, finding_ids, title, description,"
            " evidence, priority, risk_rationale, recommended_action, status, source_mode, created_by,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, inspection_id, asset_id, dump(finding_ids), title, description, dump(evidence),
             priority, risk_rationale, dump(recommended_action), status, source_mode, created_by, now, now),
        )
        db.execute(
            "INSERT INTO work_order_history (work_order_id, actor, action, from_status, to_status, note,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (order_id, created_by, "created", "", status, f"generated from {len(finding_ids)} finding(s)", now),
        )
    return get_work_order(order_id) or {}


def get_work_order(order_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(
            "SELECT w.*, a.name AS asset_name, a.asset_type, i.title AS inspection_title"
            " FROM work_orders w JOIN assets a ON a.id = w.asset_id"
            " JOIN inspections i ON i.id = w.inspection_id WHERE w.id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        data = _row(row, WORK_ORDER_JSON)
        data["history"] = _rows(db.execute(
            "SELECT * FROM work_order_history WHERE work_order_id = ? ORDER BY id", (order_id,)
        ).fetchall())
    return data


def list_work_orders(*, status: str = "", priority: str = "", asset_id: str = "", inspection_id: str = "",
                     search: str = "", page: int = 1, page_size: int = 25) -> dict[str, Any]:
    where, params = [], []
    for column, value in (("w.status", status), ("w.priority", priority), ("w.asset_id", asset_id),
                          ("w.inspection_id", inspection_id)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if search:
        where.append("(w.title LIKE ? OR w.description LIKE ? OR a.name LIKE ?)")
        params += [f"%{search}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as db:
        base = (f"SELECT w.*, a.name AS asset_name, a.asset_type, i.title AS inspection_title"
                f" FROM work_orders w JOIN assets a ON a.id = w.asset_id"
                f" JOIN inspections i ON i.id = w.inspection_id {clause}")
        total = db.execute(f"SELECT COUNT(*) c FROM ({base})", tuple(params)).fetchone()["c"]
        rows = db.execute(
            f"{base} ORDER BY CASE w.priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2"
            " ELSE 3 END, w.created_at DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": _rows(rows, WORK_ORDER_JSON), "total": total, "page": page,
            "page_size": page_size, "pages": max(1, -(-total // page_size))}


def update_work_order(order_id: str, patch: dict[str, Any], *, actor: str) -> dict[str, Any] | None:
    """Editable before execution: assignment, due date and descriptive fields only."""
    allowed = {"title", "description", "assigned_team", "assignee", "due_date", "priority",
               "risk_rationale", "recommended_action"}
    payload = {}
    for key, value in patch.items():
        if key not in allowed:
            continue
        payload[key] = dump(value) if isinstance(value, list) else value
    if payload:
        payload["updated_at"] = time.time()
        sets = ", ".join(f"{key} = ?" for key in payload)
        with connect() as db:
            db.execute(f"UPDATE work_orders SET {sets} WHERE id = ?", (*payload.values(), order_id))
            db.execute(
                "INSERT INTO work_order_history (work_order_id, actor, action, from_status, to_status,"
                " note, created_at) VALUES (?,?,?,?,?,?,?)",
                (order_id, actor, "edited", "", "", ", ".join(payload.keys()) or "no change", time.time()),
            )
    return get_work_order(order_id)


def transition_work_order(order_id: str, to_status: str, *, actor: str, note: str = "") -> tuple[dict[str, Any] | None, str]:
    with connect() as db:
        row = db.execute("SELECT status FROM work_orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            return None, "not_found"
        current = row["status"]
        if to_status not in WORK_ORDER_TRANSITIONS.get(current, set()):
            return get_work_order(order_id), "invalid_transition"
        if to_status in {"approved", "rejected"} and not actor.strip():
            return get_work_order(order_id), "actor_required"
        db.execute(
            "UPDATE work_orders SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, time.time(), order_id),
        )
        db.execute(
            "INSERT INTO work_order_history (work_order_id, actor, action, from_status, to_status, note,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (order_id, actor, to_status, current, to_status, note, time.time()),
        )
        if to_status in {"approved", "completed", "rejected"}:
            finding_status = {"approved": "converted", "completed": "closed", "rejected": "open"}[to_status]
            db.execute(
                "UPDATE findings SET status = ?, updated_at = ?"
                " WHERE id IN (SELECT value FROM json_each("
                "  (SELECT finding_ids FROM work_orders WHERE id = ?)))",
                (finding_status, time.time(), order_id),
            )
    return get_work_order(order_id), "ok"


# ------------------------------------------------------------------------- audit


def audit(*, inspection_id: str = "", asset_id: str = "", agent: str = "", phase: str = "",
          action: str = "", **detail: Any) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO audit_events (inspection_id, asset_id, agent, phase, action, detail, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (inspection_id, asset_id, agent, phase, action, dump(detail), time.time()),
        )


def list_audit(inspection_id: str = "", *, action: str = "", page: int = 1, page_size: int = 200) -> dict[str, Any]:
    where, params = [], []
    if inspection_id:
        where.append("inspection_id = ?")
        params.append(inspection_id)
    if action:
        where.append("action LIKE ?")
        params.append(f"%{action}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as db:
        total = db.execute(f"SELECT COUNT(*) c FROM audit_events {clause}", tuple(params)).fetchone()["c"]
        rows = db.execute(
            f"SELECT * FROM audit_events {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": _rows(rows, ("detail",)), "total": total, "page": page, "page_size": page_size,
            "pages": max(1, -(-total // page_size))}


def record_provider_check(*, provider: str, model: str, configured: bool, verified_ok: bool,
                          status_code: int, latency_ms: int, message: str, mode: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO provider_checks (provider, model, configured, verified_ok, status_code,"
            " latency_ms, message, mode, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (provider, model, 1 if configured else 0, 1 if verified_ok else 0, status_code,
             latency_ms, message[:400], mode, time.time()),
        )


def latest_provider_checks() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM provider_checks WHERE id IN ("
            " SELECT MAX(id) FROM provider_checks GROUP BY provider, model) ORDER BY provider, model"
        ).fetchall()
    return _rows(rows)


# ---------------------------------------------------------------------- dashboard


def dashboard_stats() -> dict[str, Any]:
    with connect() as db:
        scalar = lambda sql, *p: db.execute(sql, p).fetchone()[0]
        stats: dict[str, Any] = {
            "assets": scalar("SELECT COUNT(*) FROM assets WHERE archived = 0"),
            "inspections_total": scalar("SELECT COUNT(*) FROM inspections"),
            "inspections_completed": scalar("SELECT COUNT(*) FROM inspections WHERE status = 'completed'"),
            "inspections_running": scalar(
                "SELECT COUNT(*) FROM inspections WHERE status IN ('queued','running')"),
            "inspections_failed": scalar("SELECT COUNT(*) FROM inspections WHERE status = 'failed'"),
            "findings_total": scalar("SELECT COUNT(*) FROM findings"),
            "findings_open": scalar("SELECT COUNT(*) FROM findings WHERE status = 'open'"),
            "findings_review": scalar("SELECT COUNT(*) FROM findings WHERE needs_human_review = 1"),
            "work_orders_total": scalar("SELECT COUNT(*) FROM work_orders"),
            "work_orders_awaiting": scalar(
                "SELECT COUNT(*) FROM work_orders WHERE status = 'pending_approval'"),
            "work_orders_active": scalar(
                "SELECT COUNT(*) FROM work_orders WHERE status IN ('approved','in_progress')"),
        }
        stats["by_severity"] = {
            row["severity"]: row["c"] for row in db.execute(
                "SELECT severity, COUNT(*) c FROM findings GROUP BY severity")
        }
        stats["by_concept"] = [
            dict(row) for row in db.execute(
                "SELECT concept, COUNT(*) c FROM findings GROUP BY concept ORDER BY c DESC LIMIT 8")
        ]
        stats["by_asset_type"] = [
            dict(row) for row in db.execute(
                "SELECT a.asset_type AS type, COUNT(*) c FROM findings f"
                " JOIN assets a ON a.id = f.asset_id GROUP BY a.asset_type ORDER BY c DESC")
        ]
        stats["work_orders_by_status"] = {
            row["status"]: row["c"] for row in db.execute(
                "SELECT status, COUNT(*) c FROM work_orders GROUP BY status")
        }
        stats["activity"] = [
            dict(row) for row in db.execute(
                "SELECT d AS day, COUNT(*) c FROM ("
                " SELECT strftime('%m-%d', created_at, 'unixepoch', 'localtime') d"
                " FROM inspections WHERE created_at > strftime('%s','now','-13 days'))"
                " GROUP BY d ORDER BY d")
        ]
        stats["priority_backlog"] = [
            dict(row) for row in db.execute(
                "SELECT priority, COUNT(*) c FROM findings WHERE status = 'open'"
                " GROUP BY priority ORDER BY priority")
        ]
        stats["recent_inspections"] = _rows(db.execute(
            "SELECT i.*, a.name AS asset_name FROM inspections i JOIN assets a ON a.id = i.asset_id"
            " ORDER BY i.created_at DESC LIMIT 8").fetchall(), INSPECTION_JSON)
        stats["top_risks"] = _rows(db.execute(
            "SELECT f.id, f.concept, f.severity, f.priority, f.needs_human_review, f.status,"
            " f.area_m2, a.name AS asset_name FROM findings f JOIN assets a ON a.id = f.asset_id"
            " WHERE f.status = 'open' ORDER BY CASE f.severity WHEN 'Critical' THEN 0"
            " WHEN 'High' THEN 1 ELSE 2 END LIMIT 6").fetchall())
    stats["engine"] = {
        "note": "counts derived from stored records",
    }
    return stats
