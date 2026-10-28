"""Request/response contracts. Validation happens here, once, at the API boundary."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetType = Literal["road_network", "bridge", "pipeline", "power_grid", "facade", "other"]
Severity = Literal["Unknown", "Low", "Medium", "High", "Critical"]
Priority = Literal["P0", "P1", "P2", "P3"]
FindingStatus = Literal["open", "triaged", "converted", "closed"]
WorkOrderStatus = Literal["draft", "pending_approval", "approved", "in_progress", "completed", "rejected", "cancelled"]

MAX_PROMPTS = 8


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AssetIn(StrictModel):
    name: str = Field(min_length=2, max_length=120)
    asset_type: AssetType = "other"
    region: str = Field(default="", max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    material: str = Field(default="", max_length=120)
    installed_year: int | None = Field(default=None, ge=1800, le=2100)
    last_inspection: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=1000)

    @field_validator("last_inspection")
    @classmethod
    def _date_or_blank(cls, value: str) -> str:
        if not value:
            return ""
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("last_inspection must be an ISO date such as 2026-03-01") from error
        return value

    @model_validator(mode="after")
    def _coordinates_come_as_a_pair(self) -> AssetIn:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


class AssetPatch(StrictModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    asset_type: AssetType | None = None
    region: str | None = Field(default=None, max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    material: str | None = Field(default=None, max_length=120)
    installed_year: int | None = Field(default=None, ge=1800, le=2100)
    last_inspection: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=1000)
    archived: bool | None = None
    actor: str = Field(default="", max_length=60)


class InspectionCreate(StrictModel):
    """The JSON half of inspection creation; media arrives as multipart parts."""

    asset_id: str = Field(min_length=3, max_length=40)
    title: str = Field(default="", max_length=140)
    prompts: list[str] = Field(default_factory=list, max_length=MAX_PROMPTS)
    pixels_per_meter: float | None = Field(default=None, gt=0, le=100_000)
    idempotency_key: str | None = Field(default=None, max_length=80)

    @field_validator("prompts")
    @classmethod
    def _clean_prompts(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = re.sub(r"\s+", " ", item or "").strip()
            if len(text) < 2:
                continue
            if len(text) > 80:
                raise ValueError(f"prompt too long ({len(text)} chars): keep it under 80")
            if text.lower() not in [entry.lower() for entry in cleaned]:
                cleaned.append(text)
        return cleaned


class WorkOrderIn(StrictModel):
    inspection_id: str = Field(min_length=3, max_length=40)
    finding_ids: list[str] = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=140)
    assigned_team: str = Field(default="", max_length=80)
    assignee: str = Field(default="", max_length=80)
    due_date: str = Field(default="", max_length=40)
    actor: str = Field(min_length=2, max_length=60)

    @field_validator("due_date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        if not value:
            return ""
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("due_date must be an ISO date such as 2026-11-30") from error
        return value


class WorkOrderPatch(StrictModel):
    title: str | None = Field(default=None, min_length=3, max_length=140)
    description: str | None = Field(default=None, max_length=4000)
    assigned_team: str | None = Field(default=None, max_length=80)
    assignee: str | None = Field(default=None, max_length=80)
    due_date: str | None = Field(default=None, max_length=40)
    priority: Priority | None = None
    risk_rationale: str | None = Field(default=None, max_length=1000)
    recommended_action: list[str] | None = Field(default=None, max_length=12)
    actor: str = Field(min_length=2, max_length=60)

    @field_validator("due_date")
    @classmethod
    def _iso_date(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return value or ""
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("due_date must be an ISO date") from error
        return value


class DecisionIn(StrictModel):
    """Human sign-off. The actor name is what makes an approval attributable."""

    actor: str = Field(min_length=2, max_length=60)
    note: str = Field(default="", max_length=1000)


class TransitionIn(StrictModel):
    to_status: WorkOrderStatus
    actor: str = Field(min_length=2, max_length=60)
    note: str = Field(default="", max_length=1000)


class FindingPatch(StrictModel):
    status: FindingStatus | None = None
    actor: str = Field(min_length=2, max_length=60)


class RetryIn(StrictModel):
    mode: Literal["full", "reasoning"] = "full"


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    pages: int = 1


def as_json(data: Any) -> dict[str, Any]:
    if hasattr(data, "model_dump"):
        return data.model_dump(exclude_none=True)
    return dict(data)


def parse_iso(value: str) -> str:
    """Normalise an ISO timestamp for display without leaning on a third-party parser."""
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return value
