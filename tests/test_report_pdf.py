"""The PDF report must be a real, structurally valid PDF rendered from stored rows — not a stub.

PDF validity here is checked the way a parser checks it: the xref offsets have to point at the
objects they claim, and every stream's declared /Length has to equal its actual bytes.
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from infrascan.report_pdf import PdfReport, inspection_pdf


def _assert_valid_pdf(data: bytes) -> list[int]:
    assert data.startswith(b"%PDF-1."), data[:12]
    assert data.rstrip().endswith(b"%%EOF"), data[-16:]

    start = int(re.search(rb"startxref\n(\d+)\n", data).group(1))
    assert data[start:start + 4] == b"xref"
    numbers = [int(n) for n in re.findall(rb"(\d+) 0 obj", data)]
    entries = data[start:].split(b"\n")[2:]

    for number in numbers:
        offset = int(entries[number][:10])
        token = f"{number} 0 obj".encode()
        assert data[offset:offset + len(token)] == token, f"xref entry {number} points at the wrong byte"

    declared = [int(m.group(1)) for m in re.finditer(rb"/Length (\d+) >>", data)]
    actual = [len(m.group(1)) for m in re.finditer(rb">>\nstream\n(.*?)\nendstream", data, re.DOTALL)]
    assert declared == actual, "a stream's declared length must match its bytes"
    return numbers


def _inspection(**overrides) -> dict:
    row = {
        "id": "I-TEST00000000-abcdef", "title": "Raiwind km 42 survey", "mode": "mock",
        "asset_name": "Raiwind Road Corridor, km 42", "asset_type": "road_network",
        "region": "Punjab, Pakistan", "source_name": "road_surface.jpg", "media_kind": "image",
        "frames_total": 1, "status": "completed", "prompts": ["asphalt crack"],
        "findings": [{
            "id": "F001", "severity": "High", "concept": "asphalt crack", "concept_aliases": ["surface crack"],
            "confidence": 0.81, "source_frame": 0, "detections": 3, "frames_seen": [0],
            "corroborated_by": ["asphalt crack", "surface crack"], "area_m2": 0.87,
            "analysis": "Differential movement and fatigue cracking.",
            "detection_engine": "simulated sam-3.1",
        }],
    }
    row.update(overrides)
    return row


def test_pdf_of_a_mock_run_is_valid_and_labelled_simulated() -> None:
    data = inspection_pdf(_inspection(), [{"id": "WO-1", "status": "pending_approval", "priority": "P1",
                                           "title": "Seal crack", "history": []}])
    _assert_valid_pdf(data)
    assert b"/Type /Page " in data and b"/MediaBox [0 0 595 842]" in data
    assert b"SIMULATED" in data, "simulation output must be labelled in the printed report too"
    assert b"awaiting a named human decision" in data


def test_pdf_labels_a_live_run_without_inventing_a_key() -> None:
    data = inspection_pdf(_inspection(mode="live"), [])
    assert b"LIVE Meta Model API" in data and b"SIMULATED" not in data
    assert b"No work order was raised" in data


def test_empty_run_prints_a_result_not_a_placeholder() -> None:
    data = inspection_pdf(_inspection(findings=[]), [])
    _assert_valid_pdf(data)
    assert b"passed the corroboration threshold" in data


def test_long_reports_page_and_renumber() -> None:
    findings = [{
        "id": f"F{i:03d}", "severity": "High", "concept": "asphalt crack with a long descriptive concept name",
        "confidence": 0.8, "source_frame": i, "detections": 2, "frames_seen": [i, i + 1],
        "corroborated_by": ["asphalt crack"], "area_m2": 0.4,
        "analysis": "Fatigue cracking aggravated by thermal cycling. " * 6,
        "detection_engine": "simulated sam-3.1",
    } for i in range(40)]
    orders = [{"id": f"WO-{i}", "status": "approved", "priority": "P1", "title": "Repair",
               "history": [{"action": "approved", "actor": "A. Khan"}]} for i in range(10)]
    data = inspection_pdf(_inspection(findings=findings), orders)
    numbers = _assert_valid_pdf(data)

    pages = data.count(b"/Type /Page ")
    assert pages > 1, "content longer than one A4 page must flow onto the next"
    assert re.search(rf"/Count {pages} >>".encode(), data), "the page tree must match the page count"
    assert b"A. Khan \\(approved\\)" in data, "parentheses must be escaped inside PDF strings"
    assert len(numbers) == 4 + 2 * pages, "catalog, pages, two fonts, then a page and a stream each"


def test_winansi_only_glyphs_survive_encoding() -> None:
    doc = PdfReport()
    doc.title("Crack report — 42 %")
    doc.bullet("Inject epoxy")
    doc.body("edge case: (parentheses) and \\ backslash and • bullet")
    data = doc.build(stamp="2026-10-27 16:00")
    _assert_valid_pdf(data)
    assert b"\\(parentheses\\)" in data and b"\\\\ backslash" in data
    assert b"(parentheses)" not in data, "unescaped parentheses would break the string literal"
    assert "•".encode() not in data, "the bullet must be written as its WinAnsi byte, not UTF-8"
    assert b"\x95 Inject epoxy" in data or b"\x95 " in data


def test_endpoint_serves_the_pdf_from_stored_rows(client: TestClient, completed_inspection: dict) -> None:
    response = client.get(f"/api/inspections/{completed_inspection['id']}/report.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-1.")
    assert response.headers["content-disposition"].count(completed_inspection["id"]) == 1
