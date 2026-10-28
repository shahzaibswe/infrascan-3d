"""Phase III - Contextual Reasoning and Historical Correlation (Contextual Reasoning Agent).

Visually observed evidence stays separated from model interpretation: geometry and confidence
come from Phase II, everything else is labelled as analysis and is checked against the corpus
before it is stored. A missing corpus document, a missing model verdict or a weak signal is
reported as an uncertainty instead of being papered over.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import settings
from ..knowledge import format_context, load_corpus, retrieve
from ..meta_client import client
from ..models import AssetContext, Defect

SEVERITY_BANDS = ("Low", "Medium", "High", "Critical")
PRIORITIES = ("P0", "P1", "P2", "P3")

PROMPT = """You are the Contextual Reasoning Agent in InfraScan 3D, an infrastructure inspection pipeline.

Below is a control document corpus (repair playbooks, regional context, maintenance history) and a
list of defects detected by a vision model. For EACH defect, correlate the finding with the corpus and
return engineering-grade analysis.

Rules:
- Geometry, confidence and corroboration are measured facts from the vision stage; do not restate them
  as your own observation, build on them.
- Judge severity from the corpus thresholds, the measured geometry and the corroboration strength.
- Cite corpus document ids you actually used in "references"; never invent ids, clauses or numbers.
- If corroboration is weak (agreement below 0.5), the corpus does not cover the defect, or the scale is
  uncalibrated, list that explicitly in "uncertainties" and set "needs_human_review" to true.
- This output supports a qualified human decision. It is not a certified engineering assessment.
- Return ONLY a JSON object, no prose, shaped exactly like:
{"defects": [{"defect_id": "...", "severity": "Low|Medium|High|Critical", "priority": "P0|P1|P2|P3",
"cause": "...", "analysis": "...", "consequences": ["..."], "recommendations": ["..."],
"references": ["doc-id"], "uncertainties": ["..."], "needs_human_review": false}]}

=== CONTROL DOCUMENT CORPUS ===
{corpus}

=== ASSET CONTEXT ===
{asset}

=== DETECTED DEFECTS ===
{defects}
"""


def _defect_packet(defect: Defect, asset: AssetContext, pixels_per_meter: float | None) -> dict[str, Any]:
    left, top, right, bottom = defect.box
    packet: dict[str, Any] = {
        "defect_id": defect.defect_id,
        "concept_prompt": defect.concept,
        "frame_index": defect.frame_index,
        "observed_by": "vision model (Phase II)",
        "bbox_source_px": [left, top, right, bottom],
        "source_frame_px": list(defect.source_size),
        "bbox_area_px": defect.area_px,
        "mask_fill_ratio": round(defect.mask_fill, 3),
        "model_confidence": defect.confidence,
        "corroborated_by_prompts": defect.corroborated_by,
        "signal_count": len(defect.corroborated_by),
        "agreement": defect.agreement,
        "frames_seen": defect.frames_seen,
        "asset_coordinates": asset.coordinates,
    }
    if pixels_per_meter:
        width_m = (right - left) / pixels_per_meter
        height_m = (bottom - top) / pixels_per_meter
        packet["extent_m"] = [round(width_m, 3), round(height_m, 3)]
        packet["area_m2"] = round(width_m * height_m * max(defect.mask_fill, 0.05), 4)
        packet["calibration"] = f"{pixels_per_meter} px/m supplied by the operator"
    else:
        packet["calibration"] = "no pixels-per-metre supplied; real-world extent is not measurable"
    return packet


class ReasoningAgent:
    name = "Contextual Reasoning Agent"

    def __init__(self, audit: Any | None = None) -> None:
        self.audit = audit
        self.corpus = load_corpus()

    def relevant_documents(self, defects: list[Defect], asset: AssetContext) -> list[Any]:
        query = " ".join([d.concept for d in defects] + [asset.asset_type, asset.region, asset.material])
        picked = retrieve(query, self.corpus)
        if picked:
            return picked
        self._note("no corpus document matched the defect vocabulary; findings are left uncontextualised")
        return []

    async def run(self, defects: list[Defect], asset: AssetContext, *,
                  pixels_per_meter: float | None = None) -> dict[str, Any]:
        if not defects:
            return {"engine": "n/a", "documents": [], "annotated": [], "complete": True, "corpus_words": 0}

        documents = self.relevant_documents(defects, asset)
        packets = [_defect_packet(defect, asset, pixels_per_meter) for defect in defects]
        known_ids = {document.doc_id for document in self.corpus}

        if settings.live:
            prompt = PROMPT.format(
                corpus=format_context(documents)[:60_000] or "(no control documents available)",
                asset=json.dumps({
                    "name": asset.name, "type": asset.asset_type, "region": asset.region,
                    "coordinates": asset.coordinates, "material": asset.material,
                    "installed_year": asset.installed_year, "last_inspection": asset.last_inspection,
                }, indent=1),
                defects=json.dumps(packets, indent=1),
            )
            payload = await client.reason(prompt)
            engine = settings.spark_model
        else:
            payload = {"defects": [_simulate(p, asset, bool(documents)) for p in packets]}
            engine = "simulated rule engine"

        self._record(
            action="context.correlate",
            engine=engine,
            model=engine,
            documents=[document.doc_id for document in documents],
            prompt_defects=len(packets),
            response_keys=sorted((payload.get("defects") or [{}])[0].keys()) if payload.get("defects") else [],
            raw_response=json.dumps(payload)[:4000],
        )

        by_id = {str(item.get("defect_id")): item for item in payload.get("defects", []) if isinstance(item, dict)}
        annotated: list[Defect] = []
        missing = 0
        for defect, packet in zip(defects, packets):
            verdict = by_id.get(defect.defect_id)
            if verdict is None:
                missing += 1
                verdict = (
                    _simulate(packet, asset, bool(documents))
                    if not settings.live
                    else _no_verdict(defect)
                )
            _apply(defect, verdict, known_ids, asset, packet)
            annotated.append(defect)

        if missing and settings.live:
            self._note(f"{missing} of {len(defects)} defects returned no reasoning verdict; they are flagged for review")

        return {
            "engine": engine,
            "documents": [{"doc_id": d.doc_id, "title": d.title, "words": d.words} for d in documents],
            "corpus_words": sum(d.words for d in documents),
            "annotated": annotated,
            "complete": missing == 0,
        }

    def _record(self, **fields: Any) -> None:
        if self.audit:
            self.audit.record(agent=self.name, phase="III", **fields)

    def _note(self, message: str) -> None:
        if self.audit:
            self.audit.record(agent=self.name, phase="III", action="warning", message=message)


def _apply(defect: Defect, verdict: dict[str, Any], known_ids: set[str], asset: AssetContext,
           packet: dict[str, Any]) -> None:
    defect.severity = _severity(verdict.get("severity"))
    defect.priority = _priority(verdict.get("priority"))
    defect.cause = str(verdict.get("cause", ""))[:600]
    defect.analysis = str(verdict.get("analysis", ""))[:1600]
    defect.consequences = _strings(verdict.get("consequences"), 5)
    defect.recommendations = _strings(verdict.get("recommendations"), 6)
    # Filter to corpus ids that actually exist: an invented citation is worse than no citation.
    cited = _strings(verdict.get("references"), 6)
    invented = [item for item in cited if item not in known_ids]
    defect.references = [item for item in cited if item in known_ids]
    uncertainties = _strings(verdict.get("uncertainties"), 6)
    if invented:
        uncertainties.append(f"model cited unknown documents: {', '.join(invented)} (dropped)")
    if not defect.references:
        uncertainties.append("no control document covered this defect type")
    if not packet.get("area_m2"):
        uncertainties.append("real-world size unmeasured: no calibration supplied")
    if len(defect.frames_seen) > 1:
        uncertainties.append(f"seen in {len(defect.frames_seen)} frames; geometry taken from the clearest frame")
    defect.uncertainties = uncertainties
    defect.needs_human_review = bool(
        verdict.get("needs_human_review")
        or defect.agreement < 0.5
        or defect.confidence < 0.35
        or defect.severity == "Critical"
    )
    defect.reasoned = True


def _no_verdict(defect: Defect) -> dict[str, Any]:
    """A live model that skipped a defect leaves it unreasoned rather than fake-annotated."""
    return {
        "severity": "Medium",
        "priority": "P2",
        "cause": "",
        "analysis": "The reasoning model returned no verdict for this defect.",
        "consequences": [],
        "recommendations": ["Have a qualified inspector review the detected region"],
        "references": [],
        "uncertainties": ["no model verdict available; retry contextual reasoning"],
        "needs_human_review": True,
    }


def _severity(value: Any) -> str:
    return value if value in SEVERITY_BANDS else "Medium"


def _priority(value: Any) -> str:
    return value if value in PRIORITIES else "P2"


def _strings(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item)[:300] for item in value[:limit] if str(item).strip()]


def _simulate(packet: dict[str, Any], asset: AssetContext, has_corpus: bool) -> dict[str, Any]:
    """Deterministic rule engine mirroring the bundled playbook bands, for keyless demos."""
    concept = (packet.get("concept_prompt") or "").lower()
    fill = float(packet.get("mask_fill_ratio") or 0)
    area = float(packet.get("bbox_area_px") or 0)
    frame_area = max(1, (packet["source_frame_px"][0] or 1) * (packet["source_frame_px"][1] or 1))
    coverage = area / frame_area
    agreement = float(packet.get("agreement") or 0)

    if "crack" in concept:
        cause = f"Differential movement and fatigue cracking, aggravated by thermal and wet-cycle exposure in the {asset.region} corridor."
        consequences = ["Water ingress into the crack network", "Loss of section stiffness if unsealed", "Spalling once reinforcement is reached"]
        low, high = (["Seal with penetrating sealer", "Re-survey annually"], ["Inject epoxy or polyurethane resin", "Verify with monthly surveys for 6 months", "Structural assessment for partial depth patch"])
        references = ["repair-playbook", "maintenance-history"] if has_corpus else []
    elif "corros" in concept or "rust" in concept:
        cause = "Coating breakdown with under-deposit corrosion from industrial and chloride exposure."
        consequences = ["Progressive section loss", "Weld joint strength reduction", "Service interruption at the next load peak"]
        low, high = (["Wire brush to St 2 and recoat with specified primer", "Add to 6-month monitoring"], ["Isolate the section and cut out, weld repair sleeve", "Engineering review of remaining wall thickness"])
        references = ["repair-playbook", "regulatory-context", "maintenance-history"] if has_corpus else []
    elif "spall" in concept or "pothole" in concept:
        cause = "Substrate delamination from freeze-thaw and standing-water damage behind the surface."
        consequences = ["Rapid loss of bearing area", "Traffic hazard and liability exposure", "Base course contamination"]
        low, high = (["Surface patch and seal edges", "Re-survey after the next wet season"], ["Full-depth patch with 25 mm sound margin", "Restrict loading until repaired"])
        references = ["repair-playbook", "maintenance-history"] if has_corpus else []
    else:
        cause = "Surface degradation consistent with age; corpus coverage is limited for this concept."
        consequences = ["Unknown progression rate"]
        low, high = (["Document and monitor", "Compare against next survey"], ["Field verification required", "Scoped repair after assessment"])
        references = ["regulatory-context"] if has_corpus else []

    score = 0
    if coverage > 0.03:
        score += 1
    if coverage > 0.10:
        score += 1
    if fill > 0.55:
        score += 1
    extent = packet.get("area_m2")
    if extent and float(extent) > 0.5:
        score += 1
    # Precision over Aggression: corroboration raises confidence, it does not inflate severity.
    severity = SEVERITY_BANDS[min(score, 3)]
    priority = {0: "P3", 1: "P2", 2: "P1", 3: "P0"}[severity_rank(severity)]
    playbook = low if severity in ("Low", "Medium") else high
    uncertainties: list[str] = []
    if not has_corpus:
        uncertainties.append("no historical or standards context was available to correlate against")
    if not extent:
        uncertainties.append("defect extent is pixel-only; add a scale to size it in metres")
    return {
        "defect_id": packet["defect_id"],
        "severity": severity,
        "priority": priority,
        "cause": cause,
        "analysis": (
            f"Observed region covers {coverage * 100:.1f} percent of the frame at {fill * 100:.0f} percent "
            f"mask fill and was corroborated by {agreement:.0%} of the concept prompts that fired on it. "
            f"Mapped to {severity} using the "
            f"{references[0] if references else 'generic severity bands (no corpus match)'}. "
            "This is an AI-generated recommendation, not a certified engineering assessment."
        ),
        "consequences": consequences,
        "recommendations": playbook,
        "references": references,
        "uncertainties": uncertainties,
        "needs_human_review": severity == "Critical" or agreement < 0.5 or "limited" in cause,
    }


def severity_rank(severity: str) -> int:
    return SEVERITY_BANDS.index(severity) if severity in SEVERITY_BANDS else 1
