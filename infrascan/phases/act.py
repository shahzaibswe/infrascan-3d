"""Phase IV - Actionable Output Generation (Action & Planning Agent).

Writes the machine-readable evidence bundle: a CMMS-shaped work order export, a dispatch
payload for whatever notifier the operator actually configures, a human-readable report and a
robotic-arm target script. Nothing here is sent, executed or approved: every artifact is an
export awaiting a qualified human decision.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..meta_client import client
from ..models import Defect, InspectionContext

MUSE_CODE_PROMPT = """You are the Action & Planning Agent in InfraScan 3D.

Turn this enriched defect packet into an executable artifact. Emit ONLY the file content, no prose.

Artifact: {artifact}
Target schema: {schema}

Enriched packet (JSON):
{packet}
"""

ARTIFACT_MEDIA = {
    "work_orders": "application/json",
    "dispatch_payload": "application/json",
    "inspection_report": "text/markdown",
    "robotic_arm_script": "text/x-python",
    "audit_log": "application/x-ndjson",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ActionAgent:
    name = "Action & Planning Agent"

    def __init__(self, audit: Any | None = None) -> None:
        self.audit = audit
        self.muse_code = shutil.which("muse")

    async def run(self, context: InspectionContext, out_dir: Path,
                  audit_lines: list[dict[str, Any]] | None = None) -> dict[str, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        artifacts: dict[str, Path] = {}

        payload = self.work_orders(context)
        artifacts["work_orders"] = _write(out_dir / "work_orders.json", json.dumps(payload, indent=2, default=str))

        dispatch = self.dispatch_payload(context)
        artifacts["dispatch_payload"] = _write(out_dir / "dispatch_payload.json", json.dumps(dispatch, indent=2, default=str))

        artifacts["inspection_report"] = _write(out_dir / "inspection_report.md", self.report(context))

        if audit_lines:
            ndjson = "\n".join(json.dumps(line, default=str) for line in audit_lines)
            artifacts["audit_log"] = _write(out_dir / "audit_log.jsonl", ndjson)

        if context.defects:
            script = await self._robot_script(context)
            artifacts["robotic_arm_script"] = _write(out_dir / "robotic_arm_script.py", script)

        self._record(
            action="artifacts.generate",
            engine=self.engine,
            artifacts={name: path.name for name, path in artifacts.items()},
            work_orders=len(payload["orders"]),
            dispatch_ready=dispatch["delivery"]["configured"],
        )
        return artifacts

    @property
    def engine(self) -> str:
        if not settings.live:
            return "template-simulated"
        return "muse-code" if self.muse_code else settings.spark_model

    # ----------------------------------------------------------------- exports

    def work_orders(self, context: InspectionContext) -> dict[str, Any]:
        orders = [self.work_order_row(context, defect) for defect in context.defects]
        return {
            "inspection_id": context.inspection_id,
            "generated_at": _now(),
            "generator": self.engine,
            "pipeline_mode": context.mode,
            "risk_posture": context.risk_posture,
            "note": (
                "Export for CMMS import. No system connection is configured in this build, so these "
                "rows are not dispatched anywhere and no repair is authorised by generating them."
            ),
            "orders": orders,
        }

    def work_order_row(self, context: InspectionContext, defect: Defect) -> dict[str, Any]:
        left, top, right, bottom = defect.box
        return {
            "work_order_id": f"WO-{context.inspection_id}-{defect.defect_id}",
            "asset": {
                "name": context.asset.name,
                "type": context.asset.asset_type,
                "region": context.asset.region,
                "coordinates": context.asset.coordinates,
                "material": context.asset.material or "unspecified",
            },
            "defect": {
                "defect_id": defect.defect_id,
                "prompt_concept": defect.concept,
                "frame_index": defect.frame_index,
                "frames_seen": defect.frames_seen,
                "bounding_box_px": list(defect.box),
                "bbox_normalized": [
                    round(left / max(context.width, 1), 4),
                    round(top / max(context.height, 1), 4),
                    round(right / max(context.width, 1), 4),
                    round(bottom / max(context.height, 1), 4),
                ],
                "extent_in_frame": [context.width, context.height],
                "area_m2": defect.area_m2,
                "model_confidence": defect.confidence,
                "corroborated_by_prompts": defect.corroborated_by,
                "agreement": defect.agreement,
            },
            "assessment": {
                "severity": defect.severity,
                "priority": defect.priority,
                "probable_cause": defect.cause,
                "analysis": defect.analysis,
                "consequences_if_unaddressed": defect.consequences,
                "uncertainties": defect.uncertainties,
                "disclaimer": "AI-generated recommendation; not a certified engineering assessment.",
            },
            "recommended_procedure": defect.recommendations,
            "evidence": {
                "documents": defect.references,
                "source_media": context.source_name,
                "audit_log": f"data/audits/{context.inspection_id}.jsonl",
            },
            "dispatch": {
                "requires_human_signoff": defect.needs_human_review or defect.severity in ("High", "Critical"),
                "generated_at": _now(),
                "pipeline_mode": context.mode,
            },
        }

    def dispatch_payload(self, context: InspectionContext) -> dict[str, Any]:
        """What an on-call notifier would receive, ready to hand to a real gateway."""
        urgent = [d for d in context.defects if d.priority in ("P0", "P1")]
        return {
            "inspection_id": context.inspection_id,
            "generated_at": _now(),
            "delivery": {
                "configured": False,
                "transport": "none",
                "note": "SMS/email/Teams delivery is not implemented in this build; this file is the payload only.",
            },
            "alerts": [
                {
                    "intended_channel": "sms",
                    "severity": defect.severity,
                    "priority": defect.priority,
                    "defect_id": defect.defect_id,
                    "message": (
                        f"InfraScan {defect.priority}: {defect.concept} on {context.asset.name} "
                        f"({context.asset.region}) - {defect.cause[:120]}"
                    ),
                }
                for defect in urgent
            ],
            "escalations": [
                {
                    "defect_id": defect.defect_id,
                    "reasons": (["severity Critical"] if defect.severity == "Critical" else [])
                    + (["agreement below threshold"] if defect.agreement < 0.5 else [])
                    + (["model confidence below threshold"] if defect.confidence < 0.35 else []),
                    "frame_index": defect.frame_index,
                }
                for defect in context.defects
                if defect.needs_human_review
            ],
        }

    def report(self, context: InspectionContext) -> str:
        lines = [
            f"# InfraScan 3D inspection report - {context.inspection_id}",
            "",
            f"- Asset: {context.asset.name} ({context.asset.asset_type}, {context.asset.region}, {context.asset.coordinates})",
            f"- Source media: {context.source_name or 'uploaded frame set'} ({context.media_kind}, {context.width}x{context.height}, {context.frames_total} frame(s) analysed)",
            f"- Mode: {context.mode}  |  Risk posture: {context.risk_posture}  |  Awaiting human review: {context.awaiting_review}",
            "- Engines: " + ", ".join(f"{key}={value or 'n/a'}" for key, value in context.engines.items()),
            f"- Generated: {_now()}",
            "",
            "Observed geometry and confidence come from the vision stage. Cause, severity and",
            "recommendations are model-generated analysis for a qualified reviewer, not certified",
            "engineering judgements.",
            "",
            "| defect | prompt concept | severity | priority | confidence | agreement | frames | human review |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for defect in context.defects:
            lines.append(
                f"| {defect.defect_id} | {defect.concept} | {defect.severity} | {defect.priority} "
                f"| {defect.confidence:.2f} | {defect.agreement:.2f} | {len(defect.frames_seen) or 1} "
                f"| {'yes' if defect.needs_human_review else 'no'} |"
            )
        lines += ["", "## Findings", ""]
        for defect in context.defects:
            lines += [
                f"### {defect.defect_id} - {defect.concept}",
                (
                    f"- Observed: box {list(defect.box)} px in frame {defect.frame_index}, "
                    f"mask fill {defect.mask_fill:.0%}, area {defect.area_m2 if defect.area_m2 is not None else 'uncalibrated'} m2"
                ),
                f"- Probable cause: {defect.cause}",
                f"- Analysis: {defect.analysis}",
                "- Consequences: " + ("; ".join(defect.consequences) or "not assessed"),
                "- Recommended procedure:",
                *[f"  - {step}" for step in defect.recommendations],
                "- Uncertainties: " + ("; ".join(defect.uncertainties) or "none recorded"),
                f"- Evidence documents: {', '.join(defect.references) or 'none cited'}",
                "",
            ]
        return "\n".join(lines)

    # ------------------------------------------------------------ robot script

    async def _robot_script(self, context: InspectionContext) -> str:
        target = max(context.defects, key=lambda defect: defect.confidence)
        if settings.live and self.muse_code:
            generated = await self._muse_exec(context, target)
            if generated:
                return generated
        if settings.live:
            generated = await self._spark_script(context, target)
            if generated:
                return generated
        return self._template_script(context, target)

    async def _muse_exec(self, context: InspectionContext, defect: Defect) -> str | None:
        executable = self.muse_code
        if not executable:
            return None
        schema = "a Python module exposing execute(arm, vision) that moves to the defect and applies the repair"
        prompt = MUSE_CODE_PROMPT.format(
            artifact="robotic_arm_script.py",
            schema=schema,
            packet=json.dumps(_packet_of(context, defect), indent=1),
        )
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                [executable, "exec", "--prompt", prompt],
                capture_output=True,
                text=True,
                timeout=int(settings.request_timeout),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0 or not completed.stdout.strip():
            self._record(action="muse.exec.failed", returncode=completed.returncode,
                         stderr=completed.stderr[:400])
            return None
        self._record(action="muse.exec", returncode=completed.returncode, chars=len(completed.stdout))
        return self._validated(_strip_fences(completed.stdout), context, defect)

    async def _spark_script(self, context: InspectionContext, defect: Defect) -> str | None:
        prompt = (
            "Write a Python module for a wall-climbing inspection robot that localises and treats one defect. "
            "Expose execute(arm, vision). Use the packet fields for approach order and procedure. "
            "Never call subprocess, os.system, network or file APIs. Output only Python code.\n\n"
            + json.dumps(_packet_of(context, defect), indent=1)
        )
        try:
            code = await client.generate_code(prompt)
        except Exception as error:  # noqa: BLE001 - fall back to the template, never lose the artifact
            self._record(action="codegen.failed", message=str(error)[:300])
            return None
        return self._validated(_strip_fences(code), context, defect)

    def _validated(self, code: str, context: InspectionContext, defect: Defect) -> str | None:
        """Model-generated code is only ever a reviewed artifact: it must parse, expose execute(),
        and must not reach for shells, sockets or the filesystem before we write it to disk."""
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            self._record(action="codegen.rejected", reason=f"syntax error: {error.msg}")
            return None
        names: set[str] = set()
        has_execute = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "execute":
                has_execute = True
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        banned = {"os", "sys", "subprocess", "socket", "shutil", "pathlib", "http", "urllib", "requests"}
        if names & banned:
            self._record(action="codegen.rejected", reason=f"imports {sorted(names & banned)}")
            return None
        if not has_execute:
            self._record(action="codegen.rejected", reason="no execute() entry point")
            return None
        header = (
            f'"""Generated by {self.engine} for inspection {context.inspection_id} / {defect.defect_id}.\n\n'
            "Review before use. InfraScan 3D never runs this file; a human authorises the platform that does.\n"
            '"""\n'
        )
        return header + code

    def _template_script(self, context: InspectionContext, defect: Defect) -> str:
        left, top, right, bottom = defect.box
        target = {
            "asset": context.asset.name,
            "inspection_id": context.inspection_id,
            "defect_id": defect.defect_id,
            "severity": defect.severity,
            "priority": defect.priority,
            "bbox_px": list(defect.box),
            "center_px": [(left + right) // 2, (top + bottom) // 2],
            "frame_px": [context.width, context.height],
            "procedure": defect.recommendations,
        }
        return ROBOT_TEMPLATE.format(
            mode=context.mode,
            inspection_id=context.inspection_id,
            target=json.dumps(target, indent=4),
        )

    def _record(self, **fields: Any) -> None:
        if self.audit:
            self.audit.record(agent=self.name, phase="IV", **fields)


ROBOT_TEMPLATE = '''"""Robotics target script generated by InfraScan 3D ({mode} mode).

Inspection {inspection_id}. Coordinates are frame pixels in the source image recorded in TARGET.
Review before use: this file is an export, not an executed action. Consumed by a wall-climbing
maintenance platform only after a qualified human approves the matching work order.
"""

import json

TARGET = {target}


def approach(bbox):
    """Spiral inward from the box edge so the tool meets sound material first."""
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    inset = 1.0
    while inset > 0.1:
        yield cx, y1 + (y2 - y1) * 0.5 * (1 - inset), inset
        yield cx, y2 - (y2 - y1) * 0.5 * (1 - inset), inset
        inset -= 0.2


def execute(arm, vision=None):
    """Drive the arm over the repair procedure recorded in TARGET."""
    arm.home()
    for x, y, inset in approach(TARGET["bbox_px"]):
        arm.move_to(pixels=(x, y), standoff_mm=8.0 * inset)
        if vision is not None:
            vision.confirm(TARGET["defect_id"])
    for step in TARGET["procedure"]:
        arm.run_step(step, timeout_s=90)
    arm.capture_after()
    arm.home()
    return {{"defect_id": TARGET["defect_id"], "steps": len(TARGET["procedure"]), "status": "queued"}}


if __name__ == "__main__":
    print(json.dumps(TARGET, indent=2))
'''


def _packet_of(context: InspectionContext, defect: Defect) -> dict[str, Any]:
    return {
        "inspection_id": context.inspection_id,
        "asset": context.asset.name,
        "region": context.asset.region,
        "defect_id": defect.defect_id,
        "concept": defect.concept,
        "bbox_px": list(defect.box),
        "frame_px": [context.width, context.height],
        "severity": defect.severity,
        "priority": defect.priority,
        "procedure": defect.recommendations,
        "references": defect.references,
    }


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.DOTALL)
    return match.group(1) if match else text


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path
