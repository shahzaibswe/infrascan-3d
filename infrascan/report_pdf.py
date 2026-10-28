"""Dependency-free PDF writer for inspection reports.

Base-14 Helvetica text on A4 pages: no template engine, no image library, no network. The bytes
come from the same rows the API serves, so a printed report can never claim more than the UI does.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 48.0, 56.0, 54.0
CONTENT_W = PAGE_W - 2 * MARGIN_X
BODY_SIZE = 9.5
LEAD_SIZE = 12.5

# Helvetica widths are not uniform; the average is close enough to wrap without a font metric file.
AVG_FACTOR = 0.5


def _escape(text: str) -> str:
    # WinAnsi is a cp1252 superset: normalise first so bullets and ellipses survive as bytes.
    text = (text or "").encode("cp1252", errors="replace").decode("cp1252", errors="replace")
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, size: float) -> list[str]:
    limit = max(20, int(CONTENT_W / (size * AVG_FACTOR)))
    words = (text or "").split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit:
            if current:
                lines.append(current)
            current = word[:limit]
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


class PdfReport:
    def __init__(self) -> None:
        self._pages: list[list[str]] = []
        self._ops: list[str] = []
        self._y = PAGE_H - MARGIN_TOP
        self._stamp = ""

    # ------------------------------------------------------------------ layout

    def _room(self, needed: float) -> None:
        if self._y - needed < MARGIN_BOTTOM:
            self._flush()

    def _text(self, value: str, *, size: float, bold: bool, gap: float, color: str = "0 0 0 rg",
              indent: float = 0.0) -> None:
        for index, line in enumerate(_wrap(value, size)):
            self._room(gap)
            self._ops.append(
                f"BT {color} /F{'2' if bold else '1'} {size:.1f} Tf "
                f"1 0 0 1 {MARGIN_X + indent:.2f} {self._y:.2f} Tm ({_escape(line)}) Tj ET"
            )
            self._y -= gap

    def title(self, value: str) -> None:
        self._text(value, size=20, bold=True, gap=24, color="0.06 0.13 0.25 rg")
        self.rule()

    def heading(self, value: str) -> None:
        self._y -= 8
        self._text(value.upper(), size=11, bold=True, gap=16, color="0.06 0.13 0.25 rg")

    def body(self, value: str) -> None:
        self._text(value, size=BODY_SIZE, bold=False, gap=12.5, color="0.25 0.28 0.33 rg")

    def bullet(self, value: str) -> None:
        self._text(f"• {value}", size=BODY_SIZE, bold=False, gap=12.5, indent=6)

    def pair(self, label: str, value: str) -> None:
        self._room(13)
        y = self._y
        self._ops.append(f"BT 0.45 0.48 0.55 rg /F1 {BODY_SIZE:.1f} Tf "
                         f"1 0 0 1 {MARGIN_X:.2f} {y:.2f} Tm ({_escape(label)}) Tj ET")
        self._ops.append(f"BT 0 0 0 rg /F1 {BODY_SIZE:.1f} Tf "
                         f"1 0 0 1 {MARGIN_X + 150:.2f} {y:.2f} Tm ({_escape(value)}) Tj ET")
        self._y -= 13.5

    def rule(self) -> None:
        self._room(12)
        self._ops.append(f"0.82 0.85 0.89 RG 0.8 w {MARGIN_X:.2f} {self._y:.2f} m "
                         f"{PAGE_W - MARGIN_X:.2f} {self._y:.2f} l S")
        self._y -= 12

    def row(self, cells: list[str], widths: Sequence[float], *, bold: bool = False) -> None:
        self._room(13)
        x = MARGIN_X
        for cell, width in zip(cells, widths):
            text = cell if len(cell) <= int(width / (BODY_SIZE * AVG_FACTOR)) else f"{cell[:int(width / (BODY_SIZE * AVG_FACTOR)) - 1]}…"
            self._ops.append(
                f"BT {'0.25 0.28 0.33' if bold else '0 0 0'} rg /F{'2' if bold else '1'} {BODY_SIZE:.1f} Tf "
                f"1 0 0 1 {x:.2f} {self._y:.2f} Tm ({_escape(text)}) Tj ET"
            )
            x += width
        self._y -= 13.5

    def _flush(self) -> None:
        if self._ops or self._pages == []:
            footer = (f"BT 0.45 0.48 0.55 rg /F1 8 Tf 1 0 0 1 {MARGIN_X:.2f} {MARGIN_BOTTOM - 14:.2f} Tm "
                      f"(InfraScan 3D - generated {_escape(self._stamp)} - page {len(self._pages) + 1}) Tj ET")
            self._pages.append(self._ops + [footer])
            self._ops = []
            self._y = PAGE_H - MARGIN_TOP

    # ------------------------------------------------------------------- bytes

    def build(self, *, stamp: str) -> bytes:
        self._stamp = stamp
        self._flush()
        if self._ops:
            self._flush()

        objects: list[bytes] = []

        def add(payload: str | bytes) -> int:
            objects.append(payload if isinstance(payload, bytes) else payload.encode("cp1252", errors="replace"))
            return len(objects)

        kids_start = len(objects) + 5  # catalog, pages, 2 fonts, then pairs
        page_ids: list[int] = []
        streams: list[tuple[int, bytes]] = []
        for index, ops in enumerate(self._pages):
            raw = "\n".join(ops).encode("cp1252", errors="replace")
            streams.append((index, raw))
            page_ids.append(kids_start + 2 * index)

        add("<< /Type /Catalog /Pages 2 0 R >>")
        add(f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] /Count {len(page_ids)} >>")
        add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        for index, raw in streams:
            add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {page_ids[index] + 1} 0 R >>")
            add(f"<< /Length {len(raw)} >>\nstream\n".encode("cp1252") + raw + b"\nendstream")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode("latin-1")
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
                .encode("latin-1"))
        return bytes(out)


def _clean(value: Any, fallback: str = "not recorded") -> str:
    text = str(value).strip() if value not in (None, "") else ""
    return text or fallback


def inspection_pdf(inspection: dict[str, Any], orders: list[dict[str, Any]]) -> bytes:
    """Render one inspection row (as the API serves it) plus its work orders."""
    doc = PdfReport()
    mode = "SIMULATED - produced locally, not by a Meta model" if inspection.get("mode") == "mock" else "LIVE Meta Model API"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc.title(inspection.get("title") or inspection["id"])
    doc.body(f"Engine provenance: {mode}")

    doc.heading("Run")
    doc.pair("Inspection id", inspection["id"])
    doc.pair("Asset", _clean(inspection.get("asset_name")))
    doc.pair("Asset type", _clean(inspection.get("asset_type")))
    doc.pair("Region", _clean(inspection.get("region")))
    doc.pair("Source media", _clean(inspection.get("source_name")))
    doc.pair("Media kind", _clean(inspection.get("media_kind")))
    doc.pair("Frames analysed", str(inspection.get("frames_total") or 0))
    doc.pair("Status", _clean(inspection.get("status")))
    doc.pair("Severity peak", _clean(max((f.get("severity") or "Unknown" for f in inspection.get("findings") or []),
                                        key=["Critical", "High", "Medium", "Low", "Unknown"].index, default="none")))
    doc.pair("Prompts", _clean(", ".join(inspection.get("prompts") or []), "default concept set"))

    findings = inspection.get("findings") or []
    doc.heading(f"Defects ({len(findings)})")
    if not findings:
        doc.body("No defect passed the corroboration threshold in this run. That is a result, not a failure.")
    else:
        widths = [70, 170, 60, 55, 60, 85]
        doc.row(["Severity", "Concept", "Confidence", "Frame", "Detections", "Engine"], widths, bold=True)
        for item in findings:
            aliases = ", ".join(item.get("concept_aliases") or [])
            doc.row([
                _clean(item.get("severity")),
                _clean(f"{item.get('concept') or ''}{f' / {aliases}' if aliases else ''}"),
                f"{float(item.get('confidence') or 0) * 100:.0f}%",
                str(item.get("source_frame") or 0),
                str(item.get("detections") or 1),
                _clean(item.get("detection_engine")),
            ], widths)

    doc.heading("Observed evidence")
    for item in findings[:8]:
        area = item.get("area_m2")
        scale = f", area {area:.2f} m2" if area else " (no scale calibrated)"
        doc.body(f"{_clean(item.get('severity'))} {_clean(item.get('concept'))} - "
                 f"{float(item.get('confidence') or 0) * 100:.0f}% confidence, "
                 f"{item.get('detections') or 1} detection(s) merged from frame(s) "
                 f"{', '.join(str(f) for f in (item.get('frames_seen') or [0]))}, "
                 f"{_clean(', '.join(item.get('corroborated_by') or []), 'single prompt')}{scale}")

    doc.heading("Model interpretation")
    for item in findings[:8]:
        doc.body(f"{_clean(item.get('concept'))}: {_clean(item.get('analysis') or item.get('cause'), 'no interpretation generated')}")
    doc.body("Interpretation is generated by the reasoning engine and is not a certified engineering "
             "assessment. Verify on site before acting.")

    doc.heading(f"Work orders ({len(orders)})")
    if not orders:
        doc.body("No work order was raised for this run.")
    else:
        for order in orders:
            history = order.get("history") or []
            decided = [h for h in history if h.get("action") in ("approved", "rejected")]
            sign_off = f"{decided[-1]['actor']} ({decided[-1]['action']})" if decided else "awaiting a named human decision"
            doc.row([_clean(order.get("status")), _clean(order.get("priority")), _clean(order["id"]), sign_off],
                    [70, 40, 170, 210])
            doc.bullet(_clean(order.get("title")))
    doc.rule()
    doc.body("Approval rule: an AI-generated work order is never authorised by generation alone. Only a "
             "named human approval in the decision history permits field work.")
    return doc.build(stamp=stamp)
