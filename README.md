# InfraScan 3D

Defect intelligence for physical infrastructure: ingest a photo or clip, get segmented defects
with visual evidence, a cause/repair/urgency reading backed by a document corpus, and work
orders that a named human must approve before anything is treated as authorised maintenance.

Built on the Meta Model API (`sam-3.1` segmentation + `muse-spark-1.3` reasoning) following the
four-phase pattern in *From Concept to Code: A Four-Phase Agentic Workflow for Infrastructure
Inspection*.

## The four phases

| Phase | Agent | Model | What happens |
| --- | --- | --- | --- |
| I — Ingest and Process | Ingest | local (Pillow) | EXIF orientation, longest-side cap at 1280 px, defect-edge enhancement, re-encode |
| II — Analyze | Vision & Detection | `sam-3.1` | One concept prompt per defect type, documented wire grammar parsed to boxes + decoded masks, multi-signal IoU consensus, cross-frame merging |
| III — Transform and Enrich | Contextual Reasoning | `muse-spark-1.3` | Retrieved corpus documents + defect packets → cause, consequences, repair, urgency, severity, human-review flag |
| IV — Visualize and Activate | Action & Planning | `muse exec` / `muse-spark-1.3` | Work orders, dispatch payload, Markdown + PDF report, robotic-arm target script |

Design principles carried through the code: **Latency Discipline** (one prompt per concept, run
concurrently; streaming everywhere), **Precision over Aggression** (a region is trusted when
several concept prompts corroborate it; corroboration raises confidence, never severity), and
**Governance by Design** (every model call, parse, artifact write and human decision lands in an
append-only `audit_events` table).

## Setup

```bash
cd infrascan-3d
python -m venv .venv && .venv/Scripts/activate      # optional
pip install -r requirements.txt
```

Then either double-click `run.bat` or:

```bash
PYTHONPATH=. python -m uvicorn infrascan.server:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/>. If `data/infrascan.db` is empty, `infrascan/seed.py` creates three
demo assets on first boot.

## The demo journey

1. **New inspection** — pick an asset, choose intake (drag-drop media, a bundled sample, or
   frames the browser extracts from a clip), add up to 8 concept prompts, press
   **Start inspection**. The upload shows byte progress and returns `202` as soon as the job is
   queued.
2. **Inspection workspace** — the stage ladder walks
   `queued → validating → preprocessing → detecting → aggregating → reasoning → generating_report
   → completed`, the console replays persisted SSE events by `seq`, and Cancel/Retry are live.
3. **Evidence** — every overlay is drawn from a mask or box the detector actually returned; the
   Observed block holds measurements, the Interpretation block holds model inference.
4. **Defects** — triage with an attributable name. `converted` is never set by hand; it comes
   only from approving a work order.
5. **Work orders** — approve or reject inside the drawer (a name is mandatory), move
   `approved → in_progress → completed`, or export the queue as JSON.
6. **Reports & audit** — per-run artifacts, the on-demand PDF, and the append-only event log.
7. **Providers & config** — key presence, model ids, enforced limits and a real connectivity
   check that stores its result.

## Live mode vs simulation mode

Without a key the pipeline runs end-to-end in **clearly-labelled simulation mode**: findings carry
`simulated sam-3.1` / `simulated rule engine`, the sidebar pill reads *Simulated · no key*, and a
banner states that nothing here came from Meta. The simulator emits the real SAM 3.1 wire grammar,
including base85 one-bit masks, so parsing, decoding, SVG paths, consensus and scoring run through
exactly the code path live traffic uses.

To use the real models, copy `.env.example` to `.env` and set the key — the server reads it from
its own process environment and never sends it to the browser:

```
MODEL_API_KEY=your-key-here
```

Restart, then press **Run connectivity check** on *Providers & config*. It sends one
`GET /models` request and records the outcome; until that check succeeds, no claim of live Meta
connectivity is true.

Requirements for live use:
- Video scans need a key (the clip is uploaded via `POST /files` with purpose `user_data`, then
  segmented by `file_id`); MP4/MOV with H.264/H.265 only. Browser frame extraction works in
  either mode.
- Phase IV prefers the `muse` CLI (`muse exec --prompt`) when it is on `PATH`; otherwise it
  generates the arm script with Muse Spark, validates the AST against a banned-import list, and
  falls back to a checked-in template. On this machine `muse` is not installed.

Force the simulator even with a key present: `INFRASCAN_MODE=mock`.

## HTTP API

| Area | Endpoints |
| --- | --- |
| Config & health | `GET /api/config`, `GET /api/providers`, `POST /api/providers/check`, `GET /api/activity`, `GET /api/dashboard` |
| Assets | `GET/POST /api/assets`, `GET/PATCH/DELETE /api/assets/{id}`, `GET /api/assets/{id}/inspections` |
| Inspections | `POST /api/inspections` (multipart `payload` + `file`/`frames`/`frame_times`/`sample`), `GET /api/inspections`, `GET/DELETE /api/inspections/{id}`, `POST .../cancel`, `POST .../retry`, `GET .../events` (SSE), `GET .../media`, `GET .../artifacts[/{name}]`, `GET .../audit`, `GET .../report.pdf` |
| Findings | `GET /api/findings`, `GET /api/findings/{id}`, `PATCH /api/findings/{id}` |
| Work orders | `GET/POST /api/work-orders`, `GET/PATCH /api/work-orders/{id}`, `POST .../approve`, `POST .../reject`, `POST .../transition`, `GET /api/work-orders/export` |
| Audit | `GET /api/audit` (newest first, paginated) |
| Docs | `/api/docs`, `/api/openapi.json` |

Errors share one envelope: `{"error": {"code", "message", "detail"}}`. `/api/*` and `/static/*`
are served with `Cache-Control: no-store` so a long-open dashboard never reads a stale job state.

## Layout

```
infrascan/
  config.py        env loading, settings, live/mock decision
  db.py            SQLite connection, WAL, PRAGMA user_version migrations
  repo.py          typed data access for every table
  schemas.py       pydantic contracts, extra="forbid" at the boundary
  errors.py        error taxonomy, redaction of credentials
  meta_client.py   Responses API client: streaming, Files API, reasoning, code generation
  sam_grammar.py   documented SAM 3.1 line parser + mask decode + IoU
  mask_codec.py    one-bit encoder/decoder the simulation and live path share
  simulation.py    deterministic offline lane generator in the real wire grammar
  knowledge.py     corpus loading and retrieval
  uploads.py       media validation, storage layout, frame intake, retention
  models.py        AssetContext, Defect, PhaseResult, Inspection
  jobs.py          queue, job lifecycle, stage events, SSE replay, cancel/retry
  phases/          ingest.py (I) detect.py (II) reason.py (III) act.py (IV)
  report_pdf.py    on-demand PDF report
  seed.py          demo assets for a first run
  server.py        FastAPI app and route handlers
  web/static/      index.html, styles.css, app.js (core), views.js (ten views)
knowledge/         synthetic repair playbook, regulatory context, maintenance history
samples/           generate_samples.py + three test frames with .defects.json ground truth
tests/             grammar, provider adapter, engine, API contract, PDF
data/              infrascan.db, uploads/, prepared/, outputs/ (created at runtime)
```

## Checks

```bash
PYTHONPATH=. python -m pytest tests/ -q      # 54 passed, 1 skipped without a key
python -m ruff check infrascan tests
python -m mypy infrascan
```

The skipped test is the live-provider smoke test: it skips with a stated reason when
`MODEL_API_KEY` is absent, so it can never report a connectivity result the machine did not
obtain.

## Notes and limits

- Documents under `knowledge/` are synthetic fixtures marked as such; they are not real standards
  and no defect here describes a real asset.
- 3D assets (`.glb`, `.gltf`, `.obj`, `.ply`) are refused with an explanation — no 3D parser or
  viewer is installed, so accepting one would only produce a broken preview.
- Area in metres requires `pixels_per_meter`; without it findings say "not computed (no px/m)"
  instead of inventing a size.
- In simulation mode one region is seeded per inspection and drifts slightly per frame, so
  cross-frame merging is exercised rather than defeated by fresh random regions each frame.
  With a live key the same merging runs against whatever SAM 3.1 actually returns.
- No CMMS, email, SMS or robotic-arm integration is wired up. `GET /api/work-orders/export` and
  `dispatch_payload.json` are import payloads and say so; the arm script is an export, never an
  executed action.
- Nothing dispatches without a named human approval. Work orders start as `draft` or
  `pending_approval`, and approval records who decided and why.
