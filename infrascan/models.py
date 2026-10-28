"""Shared data models for the InfraScan 3D pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_ORDER = {"Unknown": -1, "Low": 0, "Medium": 1, "High": 2, "Critical": 3}
PRIORITY_ORDER = {"P3": 3, "P2": 2, "P1": 1, "P0": 0}


@dataclass(slots=True)
class AssetContext:
    """What is being inspected; feeds Phase I enrichment and Phase III reasoning."""

    name: str = "Unnamed asset"
    asset_type: str = "power_grid"
    region: str = "Pakistan"
    latitude: float | None = None
    longitude: float | None = None
    material: str = ""
    installed_year: int | None = None
    last_inspection: str = ""
    notes: str = ""

    @property
    def coordinates(self) -> str:
        if self.latitude is None or self.longitude is None:
            return "unreferenced"
        return f"{self.latitude:.4f}N, {self.longitude:.4f}E"


@dataclass(slots=True)
class Defect:
    """One consensus defect finding after multi-signal cross-validation and frame merging."""

    defect_id: str
    concept: str
    frame_index: int
    box: tuple[int, int, int, int]
    source_size: tuple[int, int]
    confidence: float
    agreement: float
    corroborated_by: list[str] = field(default_factory=list)
    frames_seen: list[int] = field(default_factory=list)
    mask_svg: str = ""
    mask_origin: tuple[int, int] = (0, 0)
    mask_size: tuple[int, int] = (0, 0)
    mask_fill: float = 0.0
    detections: int = 1
    area_m2: float | None = None
    severity: str = "Unknown"
    cause: str = ""
    consequences: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    analysis: str = ""
    priority: str = "P2"
    needs_human_review: bool = False
    reasoned: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def area_px(self) -> int:
        left, top, right, bottom = self.box
        return max(0, right - left) * max(0, bottom - top)


@dataclass(slots=True)
class InspectionContext:
    """Everything Phase IV needs about one finished inspection."""

    inspection_id: str
    asset: AssetContext
    width: int
    height: int
    mode: str
    defects: list[Defect] = field(default_factory=list)
    pixels_per_meter: float | None = None
    media_kind: str = "image"
    source_name: str = ""
    frames_total: int = 1
    engines: dict[str, str] = field(default_factory=dict)

    @property
    def risk_posture(self) -> str:
        if not self.defects:
            return "No actionable findings"
        worst = max(self.defects, key=lambda defect: SEVERITY_ORDER.get(defect.severity, -1))
        return f"{worst.severity} - {worst.concept}"

    @property
    def awaiting_review(self) -> int:
        return sum(1 for defect in self.defects if defect.needs_human_review)
