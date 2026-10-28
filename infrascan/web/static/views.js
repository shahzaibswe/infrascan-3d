/* InfraScan 3D — views. Every number, badge and overlay here is rendered from API data.
   Wrapped in an IIFE: both files are classic scripts, so top-level names would collide. */
(function () {
"use strict";

const { Api, UI, Router, Store, Fmt, esc, elFrom, qs, qsa, followJob, overlaySvg, banner, navCount, uploadInspection, debounce } = window.Infrascan;

/* ------------------------------------------------------------------ shared */

let timers = [];
let closeStream = null;

function every(ms, fn) { timers.push(setInterval(fn, ms)); }
function leavePage() {
  timers.splice(0).forEach(clearInterval);
  if (closeStream) { closeStream(); closeStream = null; }
}

function register(id, render) {
  Router.register(id, (host, params, query) => {
    leavePage();
    return render(host, params, query || {});
  });
}

const actor = {
  get() { return localStorage.getItem("infrascan.actor") || ""; },
  set(value) { localStorage.setItem("infrascan.actor", value.trim()); },
};

function actorField(name = "actor", label = "Your name (required for the audit trail)") {
  return `<div class="field" data-field="${name}">
    <label for="${name}Id">${esc(label)}</label>
    <input id="${name}Id" name="${name}" type="text" value="${esc(actor.get())}" placeholder="e.g. A. Khan" autocomplete="name">
    <span class="error-text" data-error></span>
  </div>`;
}

const SEVERITIES = ["Critical", "High", "Medium", "Low", "Unknown"];
const ORDER_STATUSES = ["draft", "pending_approval", "approved", "in_progress", "completed", "rejected", "cancelled"];
const ORDER_TONE = {
  draft: "badge", pending_approval: "badge badge-warn", approved: "badge badge-ok",
  in_progress: "badge badge-info", completed: "badge badge-ok", rejected: "badge badge-danger", cancelled: "badge",
};

function orderBadge(status) {
  return `<span class="${ORDER_TONE[status] || "badge"}">${esc(status.replace("_", " "))}</span>`;
}

function selectOptions(values, current, labels = {}) {
  return values.map((value) =>
    `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(labels[value] || value)}</option>`).join("");
}

function engineBadge(value, fallback = "not run") {
  const label = value || fallback;
  const tone = !value ? "" : (String(value).startsWith("simulated") || value === "unavailable" || value === "rule engine") ? "badge-mock" : "badge-info";
  return `<span class="badge ${tone}">${esc(label)}</span>`;
}

function exportLine(filters) {
  const search = new URLSearchParams();
  ["status", "priority", "asset_id"].forEach((key) => { if (filters[key]) search.set(key, filters[key]); });
  const line = search.toString();
  return line ? `?${line}` : "";
}

function readQuery(patch) {  const current = new URLSearchParams(location.hash.split("?")[1] || "");
  Object.entries(patch).forEach(([key, value]) => {
    if (value === "" || value === null || value === undefined) current.delete(key);
    else current.set(key, value);
  });
  const line = current.toString();
  const base = location.hash.split("?")[0];
  location.hash = line ? `${base}?${line}` : base;
}

/* ---------------------------------------------------------------- dashboard */

async function viewDashboard(host) {
  const stats = await Api.get("/api/dashboard");
  navCount("orders", stats.work_orders_awaiting || null);
  navCount("inspections", stats.inspections_running || null);
  navCount("defects", stats.findings_open || null);

  const severityMax = Math.max(1, ...SEVERITIES.map((level) => stats.by_severity[level] || 0));
  const activityMax = Math.max(1, ...stats.activity.map((entry) => entry.c));

  host.innerHTML = `
    <div class="grid cols-4">
      ${statCard("Assets tracked", stats.assets, `${stats.inspections_total} inspection(s) on record`, "neutral")}
      ${statCard("Inspections completed", stats.inspections_completed, `${stats.inspections_total - stats.inspections_completed} not completed`, "ok")}
      ${statCard("Running now", stats.inspections_running, stats.inspections_running ? "Live progress in Inspections" : "Queue is empty", "neutral")}
      ${statCard("Failed runs", stats.inspections_failed, stats.inspections_failed ? "Open one to retry reasoning" : "No failures", stats.inspections_failed ? "danger" : "neutral")}
      ${statCard("Open defects", stats.findings_open, `${stats.findings_total} detected in total`, "warn")}
      ${statCard("Flagged for review", stats.findings_review, "Model asked a human to look", stats.findings_review ? "warn" : "neutral")}
      ${statCard("Pending approval", stats.work_orders_awaiting, "drafts stay unauthorised until approved", stats.work_orders_awaiting ? "danger" : "ok")}
      ${statCard("Authorised work", stats.work_orders_active, "approved or in progress", "ok")}
    </div>

    <div class="split" style="margin-top:20px">
      <div class="grid" style="gap:20px">
        <div class="card">
          <div class="card-head"><h2>Defect severity distribution</h2>
            <span class="muted tiny row-end">${esc(stats.engine.note)}</span></div>
          <div class="card-body stack">
            ${SEVERITIES.map((level) => barRow(level, stats.by_severity[level] || 0, severityMax, `var(--sev-${level.toLowerCase()})`)).join("")}
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Inspections, last 14 days</h2><span class="muted tiny row-end">created per day</span></div>
          <div class="card-body">
            ${stats.activity.length
              ? `<div class="spark">${stats.activity.map((entry) => `
                  <div class="spark-col" title="${entry.c} on ${esc(entry.day)}">
                    <i style="height:${Math.round((entry.c / activityMax) * 54)}px"></i><span>${esc(entry.day)}</span>
                  </div>`).join("")}</div>`
              : UI.empty({ icon: "◍", title: "No inspections yet", text: "Start one and the timeline fills in.", actionHref: "#/inspections/new", actionLabel: "New inspection" })}
          </div>
        </div>
        <div class="card card-flush">
          <div class="card-head"><h2>Highest open risks</h2>
            <a class="button button-sm row-end" href="#/defects">All defects</a></div>
          <div class="table-wrap">${riskTable(stats.top_risks)}</div>
        </div>
      </div>

      <div class="grid" style="gap:20px">
        <div class="card">
          <div class="card-head"><h2>Where the output came from</h2></div>
          <div class="card-body stack">
            <dl class="kv">
              <dt>Mode</dt><dd>${esc(stats.mode)}</dd>
              <dt>Segmentation</dt><dd>${engineBadge(stats.engines.live ? stats.engines.segmentation : "simulated sam-3.1")}</dd>
              <dt>Reasoning</dt><dd>${engineBadge(stats.engines.live ? stats.engines.reasoning : "simulated rule engine")}</dd>
            </dl>
            <p class="tiny muted">Simulated output is produced locally by this build. It is never presented as a Meta API result.</p>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Approval backlog</h2><span class="muted tiny row-end">by priority</span></div>
          <div class="card-body stack">
            ${(stats.priority_backlog || []).length
              ? stats.priority_backlog.map((entry) => barRow(entry.priority, entry.c,
                  Math.max(1, ...stats.priority_backlog.map((x) => x.c)), "var(--accent)")).join("")
              : `<p class="muted tiny">No open findings are waiting on a priority decision.</p>`}
            <div class="row"><a class="button button-sm button-primary" href="#/work-orders?status=pending_approval">Review work orders</a></div>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Live activity</h2><span class="badge" id="feedState">polling</span></div>
          <div class="card-body tight" id="activityFeed">${UI.skeleton(3)}</div>
        </div>
        <div class="card card-flush">
          <div class="card-head"><h2>Recent inspections</h2><a class="button button-sm row-end" href="#/inspections">All</a></div>
          <div class="table-wrap">${recentTable(stats.recent_inspections)}</div>
        </div>
      </div>
    </div>`;

  const paintFeed = async () => {
    const rows = await Api.get("/api/activity?minutes=240");
    const feed = qs("#activityFeed", host);
    if (!feed) return;
    feed.innerHTML = rows.length
      ? `<div class="timeline">${rows.slice(0, 8).map((row) => `
          <div class="timeline-item"><span class="timeline-dot"></span><div class="t-body">
            <div><b>${esc(row.action)}</b>${row.inspection_id ? ` · <a href="#/inspections/${esc(row.inspection_id)}">${esc(row.inspection_id)}</a>` : ""}</div>
            <div class="t-when">${esc(row.agent || "system")} · ${esc(Fmt.rel(row.created_at))}</div>
          </div></div>`).join("")}</div>`
      : `<p class="muted tiny">Nothing has happened in the last 4 hours.</p>`;
  };
  paintFeed().catch(() => {});
  every(15000, () => paintFeed().catch(() => {}));
}

function statCard(label, value, note, tone) {
  return `<div class="stat tone-${tone}">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${esc(value)}</div>
    <div class="stat-note ${tone === "warn" || tone === "danger" ? "warn" : ""}">${esc(note)}</div>
  </div>`;
}

function barRow(label, value, max, color) {
  return `<div class="bar-row"><span>${esc(label)}</span>
    <span class="bar-track"><i class="bar-fill" style="width:${Math.round((value / max) * 100)}%;background:${color}"></i></span>
    <span class="bar-value">${esc(value)}</span></div>`;
}

function riskTable(rows) {
  if (!rows.length) return UI.empty({ icon: "✓", title: "No open risks", text: "Nothing is waiting on a triage decision." });
  return `<table class="data"><thead><tr><th>Concept</th><th>Asset</th><th>Severity</th><th>Priority</th><th class="num">Area</th><th></th></tr></thead>
    <tbody>${rows.map((row) => `<tr>
      <td class="cell-title">${esc(row.concept)}${row.needs_human_review ? ' <span class="badge badge-warn">review</span>' : ""}</td>
      <td>${esc(row.asset_name)}</td><td>${UI.severity(row.severity)}</td>
      <td><span class="badge badge-priority">${esc(row.priority)}</span></td>
      <td class="num">${row.area_m2 ? `${Fmt.num(row.area_m2)} m²` : "—"}</td>
      <td><a class="button button-sm" href="#/defects?search=${encodeURIComponent(row.concept)}">Open</a></td>
    </tr>`).join("")}</tbody></table>`;
}

function recentTable(rows) {
  if (!rows.length) return UI.empty({ icon: "◍", title: "No inspections yet", text: "Create one to see it here.", actionHref: "#/inspections/new", actionLabel: "New inspection" });
  return `<table class="data"><thead><tr><th>Title</th><th>Asset</th><th>Status</th><th class="num">Findings</th><th>Created</th></tr></thead>
    <tbody>${rows.map((row) => `<tr role="button" tabindex="0" data-href="#/inspections/${esc(row.id)}">
      <td class="cell-title">${esc(row.title)}</td><td>${esc(row.asset_name)}</td>
      <td>${UI.statusTag(row.status)}</td><td class="num">${row.findings ? row.findings.length : 0}</td>
      <td class="muted tiny">${esc(Fmt.rel(row.created_at))}</td></tr>`).join("")}</tbody></table>`;
}

/* ------------------------------------------------------------------- assets */

async function viewAssets(host, params, query) {
  const filters = {
    search: query.search || "", asset_type: query.type || "", status: query.status || "active",
    sort: query.sort || "updated_desc", page: Number(query.page || 1),
  };
  host.innerHTML = `
    <div class="stack">
      <div class="card"><div class="card-body">
        <div class="filters">
          <div class="field search"><label for="assetSearch">Search</label>
            <input id="assetSearch" type="search" value="${esc(filters.search)}" placeholder="name, region, material or notes"></div>
          <div class="field"><label for="assetType">Asset type</label>
            <select id="assetType"><option value="">All types</option>${selectOptions(Store.config.choices.asset_types, filters.asset_type, typeLabels())}</select></div>
          <div class="field"><label for="assetStatus">Status</label>
            <select id="assetStatus"><option value="active"${filters.status === "active" ? " selected" : ""}>Active</option>
              <option value="archived"${filters.status === "archived" ? " selected" : ""}>Archived</option>
              <option value="all"${filters.status === "all" ? " selected" : ""}>All</option></select></div>
          <div class="field"><label for="assetSort">Sort</label>
            <select id="assetSort">
              <option value="updated_desc"${filters.sort === "updated_desc" ? " selected" : ""}>Recently updated</option>
              <option value="name_asc"${filters.sort === "name_asc" ? " selected" : ""}>Name A–Z</option>
              <option value="name_desc"${filters.sort === "name_desc" ? " selected" : ""}>Name Z–A</option>
              <option value="inspections_desc"${filters.sort === "inspections_desc" ? " selected" : ""}>Most inspections</option>
            </select></div>
          <div class="row-end"><button class="button button-primary" id="newAsset" type="button">+ New asset</button></div>
        </div>
      </div></div>
      <div class="card card-flush"><div id="assetTable">${UI.skeleton(6)}</div>
        <div class="pagination" id="assetPager"></div></div>
    </div>`;

  qs("#newAsset", host).addEventListener("click", () => assetForm(null, reload));
  qs("#assetSearch", host).addEventListener("input", debounce((event) => {
    readQuery({ search: event.target.value.trim(), page: "" });
  }));
  ["assetType", "assetStatus", "assetSort"].forEach((id) => {
    qs(`#${id}`, host).addEventListener("change", (event) => {
      const key = { assetType: "type", assetStatus: "status", assetSort: "sort" }[id];
      readQuery({ [key]: event.target.value, page: "" });
    });
  });

  async function reload() { await Router.go(); }

  const page = await Api.get(Api.query("/api/assets", filters));
  navCount("assets", page.total);
  const table = qs("#assetTable", host);
  if (!page.items.length) {
    table.innerHTML = UI.empty({
      icon: "▤", title: filters.search || filters.asset_type ? "No asset matches" : "No assets yet",
      text: filters.search || filters.asset_type
        ? "Loosen the filters, or register the asset you want to inspect."
        : "Register an asset first — inspections always attach to one.",
    });
    qs("#assetPager", host).innerHTML = "";
    return;
  }
  table.innerHTML = `<table class="data"><thead><tr>
      <th>Asset</th><th>Type</th><th>Region</th><th class="num">Inspections</th>
      <th class="num">Open defects</th><th>Last inspection</th><th>Updated</th><th></th></tr></thead>
    <tbody>${page.items.map((asset) => `<tr data-id="${esc(asset.id)}">
      <td><a class="cell-title" href="#/assets/${esc(asset.id)}">${esc(asset.name)}</a>
        <div class="cell-sub mono">${esc(asset.id)}${asset.archived ? ' · <span class="badge">archived</span>' : ""}</div></td>
      <td>${esc(typeLabel(asset.asset_type))}</td>
      <td>${esc(asset.region || "—")}${asset.latitude !== null && asset.latitude !== undefined
        ? `<div class="cell-sub">${Fmt.num(asset.latitude, 4)}, ${Fmt.num(asset.longitude, 4)}</div>` : ""}</td>
      <td class="num">${asset.inspections ?? asset.inspection_count ?? 0}</td>
      <td class="num">${asset.open_findings === undefined ? "—" : asset.open_findings}</td>
      <td>${esc(asset.last_inspection || "—")}</td>
      <td class="muted tiny">${esc(Fmt.rel(asset.updated_at))}</td>
      <td><div class="row" style="justify-content:flex-end">
        <button class="button button-sm" type="button" data-act="edit">Edit</button>
        <button class="button button-sm button-ghost" type="button" data-act="archive">${asset.archived ? "Restore" : "Archive"}</button>
        <button class="button button-sm button-danger" type="button" data-act="delete">Delete</button>
      </div></td></tr>`).join("")}</tbody></table>`;

  UI.pager(qs("#assetPager", host), page, (next) => readQuery({ page: next }));

  qsa("tbody tr", table).forEach((row) => {
    const asset = page.items.find((item) => item.id === row.dataset.id);
    row.addEventListener("click", (event) => { if (event.target.closest("button")) return; location.hash = `#/assets/${asset.id}`; });
    qsa("button", row).forEach((button) => button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const action = button.dataset.act;
      if (action === "edit") assetForm(asset, reload);
      if (action === "archive") {
        button.disabled = true;
        try {
          await Api.patch(`/api/assets/${asset.id}`, { archived: !asset.archived, actor: actor.get() || "operator" });
          UI.toast("ok", asset.archived ? "Asset restored" : "Asset archived", asset.name);
          reload();
        } catch (error) { UI.error(error); button.disabled = false; }
      }
      if (action === "delete") deleteAsset(asset, reload);
    }));
  });
}

function typeLabel(value) {
  return { road_network: "Road network", bridge: "Bridge", pipeline: "Pipeline", power_grid: "Power grid", facade: "Facade", other: "Other" }[value] || value;
}
function typeLabels() {
  return Object.fromEntries(Object.entries({
    road_network: "Road network", bridge: "Bridge", pipeline: "Pipeline",
    power_grid: "Power grid", facade: "Facade", other: "Other",
  }));
}

function assetForm(asset, done) {
  const isEdit = Boolean(asset);
  const body = elFrom(`<form class="form-grid" novalidate>
    <div class="field full" data-field="name"><label for="fName">Asset name *</label>
      <input id="fName" name="name" type="text" required value="${esc(asset ? asset.name : "")}" placeholder="Raiwind Road Corridor, km 42">
      <span class="hint">2–120 characters.</span><span class="error-text" data-error></span></div>
    <div class="field" data-field="asset_type"><label for="fType">Asset type</label>
      <select id="fType" name="asset_type">${selectOptions(Store.config.choices.asset_types, asset ? asset.asset_type : "road_network", typeLabels())}</select>
      <span class="error-text" data-error></span></div>
    <div class="field" data-field="region"><label for="fRegion">Region</label>
      <input id="fRegion" name="region" type="text" value="${esc(asset ? asset.region : "")}" placeholder="Punjab, Pakistan">
      <span class="error-text" data-error></span></div>
    <div class="field" data-field="latitude"><label for="fLat">Latitude</label>
      <input id="fLat" name="latitude" type="number" step="0.0001" min="-90" max="90" value="${asset && asset.latitude !== null ? asset.latitude : ""}">
      <span class="hint">Supply both or neither.</span><span class="error-text" data-error></span></div>
    <div class="field" data-field="longitude"><label for="fLng">Longitude</label>
      <input id="fLng" name="longitude" type="number" step="0.0001" min="-180" max="180" value="${asset && asset.longitude !== null ? asset.longitude : ""}">
      <span class="error-text" data-error></span></div>
    <div class="field" data-field="material"><label for="fMaterial">Surface / material</label>
      <input id="fMaterial" name="material" type="text" value="${esc(asset ? asset.material : "")}" placeholder="Asphalt concrete">
      <span class="error-text" data-error></span></div>
    <div class="field" data-field="installed_year"><label for="fYear">Year installed</label>
      <input id="fYear" name="installed_year" type="number" min="1800" max="2100" value="${asset && asset.installed_year ? asset.installed_year : ""}">
      <span class="error-text" data-error></span></div>
    <div class="field full" data-field="last_inspection"><label for="fLast">Last physical inspection</label>
      <input id="fLast" name="last_inspection" type="date" value="${esc(asset ? asset.last_inspection : "")}">
      <span class="hint">ISO date. Leave blank if unknown — history is never invented.</span><span class="error-text" data-error></span></div>
    <div class="field full" data-field="notes"><label for="fNotes">Notes</label>
      <textarea id="fNotes" name="notes" placeholder="Context the model may use: traffic load, past repairs, drainage…">${esc(asset ? asset.notes : "")}</textarea>
      <span class="error-text" data-error></span></div>
    ${isEdit ? actorField("fActor", "Your name *") : ""}
  </form>`);

  UI.modal({
    title: isEdit ? `Edit ${asset.name}` : "Register an asset",
    subtitle: isEdit ? asset.id : "An asset is anything you inspect: a road section, a span, a pipeline run.",
    body,
    actions: [
      { label: "Cancel", class: "button-ghost" },
      {
        label: isEdit ? "Save changes" : "Create asset",
        class: "button-primary",
        autofocus: true,
        onClick: async (close) => {
          const payload = readForm(body, ["name", "asset_type", "region", "material", "notes", "last_inspection"]);
          const numeric = { latitude: "fLat", longitude: "fLng", installed_year: "fYear" };
          Object.entries(numeric).forEach(([key, id]) => {
            const raw = qs(`#${id}`, body).value.trim();
            if (raw !== "") payload[key] = Number(raw);
          });
          try {
            if (isEdit) {
              const name = (qs("#fActorId", body).value || "").trim() || actor.get();
              if (name.length < 2) {
                UI.toast("warn", "Name required", "Asset edits are attributed in the audit trail.");
                qs("#fActorId", body).focus();
                return true;
              }
              actor.set(name);
              await Api.patch(`/api/assets/${asset.id}`, { ...payload, actor: name });
            } else {
              const created = await Api.post("/api/assets", payload);
              if (created && created.id) location.hash = `#/assets/${encodeURIComponent(created.id)}`;
            }
            UI.toast("ok", isEdit ? "Asset updated" : "Asset created", payload.name);
            close();
            if (done) done();
          } catch (error) {
            UI.fieldErrors(body, error);
            UI.error(error, "The server rejected this form");
            return true;
          }
          return false;
        },
      },
    ],
  });
}

function readForm(form, names) {
  const out = {};
  names.forEach((name) => {
    const node = qs(`[name="${name}"]`, form);
    if (node) out[name] = node.value.trim();
  });
  return out;
}

async function deleteAsset(asset, done) {
  const ok = await UI.confirm({
    title: "Delete asset",
    message: `Delete “${asset.name}”? Its inspections, findings and work orders are not removed by this action.`,
    label: "Delete asset",
    requireText: asset.id,
  });
  if (!ok) return;
  try {
    await Api.del(`/api/assets/${asset.id}?confirm=${encodeURIComponent(asset.id)}`);
    UI.toast("ok", "Asset deleted", asset.name);
    done();
  } catch (error) {
    if (error.code === "asset_has_inspections") {
      const cascade = await UI.confirm({
        title: "Asset has history",
        message: `“${asset.name}” has ${asset.inspections || ""} inspection(s). Deleting the asset also deletes every inspection, finding and work order attached to it.`,
        label: "Delete everything",
        details: "This cannot be undone. Media files are purged too.",
      });
      if (!cascade) return;
      await Api.del(`/api/assets/${asset.id}?confirm=${encodeURIComponent(asset.id)}&cascade=true`);
      UI.toast("warn", "Asset and its history deleted", asset.name);
      done();
      return;
    }
    UI.error(error);
  }
}

/* -------------------------------------------------------------- asset detail */

async function viewAsset(host, params) {
  const asset = await Api.get(`/api/assets/${encodeURIComponent(params.id)}`);
  const [orders, inspections, findings] = await Promise.all([
    Api.get(Api.query("/api/inspections", { asset_id: asset.id, page_size: 10 })),
    Api.get(Api.query("/api/findings", { asset_id: asset.id, page_size: 25 })),
    Api.get(Api.query("/api/work-orders", { asset_id: asset.id, page_size: 10 })),
  ]);
  navCount("assets", null);

  host.innerHTML = `
    <div class="stack">
      <div class="card"><div class="card-body spread">
        <div>
          <div class="row"><h2>${esc(asset.name)}</h2>
            <span class="badge">${esc(typeLabel(asset.asset_type))}</span>
            ${asset.archived ? '<span class="badge">archived</span>' : ""}
            ${asset.is_sample ? '<span class="badge badge-info">seed sample</span>' : ""}</div>
          <p class="muted tiny mono">${esc(asset.id)}</p>
          <dl class="kv" style="margin-top:12px">
            <dt>Region</dt><dd>${esc(asset.region || "—")}</dd>
            <dt>Coordinates</dt><dd>${asset.latitude === null || asset.latitude === undefined ? "—" : `${Fmt.num(asset.latitude, 5)}, ${Fmt.num(asset.longitude, 5)}`}</dd>
            <dt>Material</dt><dd>${esc(asset.material || "—")}</dd>
            <dt>Installed</dt><dd>${esc(asset.installed_year || "—")}</dd>
            <dt>Last physical inspection</dt><dd>${esc(asset.last_inspection || "not recorded")}</dd>
            <dt>Open defects</dt><dd>${asset.open_findings}</dd>
            <dt>Inspections</dt><dd>${asset.inspection_count}</dd>
          </dl>
          ${asset.notes ? `<p class="tiny muted" style="margin-top:10px">${esc(asset.notes)}</p>` : ""}
        </div>
        <div class="stack" style="align-items:stretch">
          <a class="button button-primary" href="#/inspections/new?asset=${encodeURIComponent(asset.id)}">+ New inspection</a>
          <button class="button" type="button" id="editAsset">Edit asset</button>
          <button class="button button-danger" type="button" id="delAsset">Delete</button>
        </div>
      </div></div>

      <div class="card card-flush">
        <div class="card-head"><h2>Inspection history</h2><span class="muted tiny row-end">newest first</span></div>
        <div class="table-wrap">${inspectionsTable(inspections.items, asset)}</div>
      </div>

      <div class="split">
        <div class="card card-flush">
          <div class="card-head"><h2>Defects on this asset</h2><a class="button button-sm row-end" href="#/defects?asset=${encodeURIComponent(asset.id)}">All</a></div>
          <div class="table-wrap">${findingsTable(findings.items)}</div>
        </div>
        <div class="card card-flush">
          <div class="card-head"><h2>Work orders</h2><a class="button button-sm row-end" href="#/work-orders?asset=${encodeURIComponent(asset.id)}">All</a></div>
          <div class="table-wrap">${ordersTable(orders.items, { compact: true })}</div>
        </div>
      </div>
    </div>`;

  qs("#editAsset", host).addEventListener("click", () => assetForm(asset, () => Router.go()));
  qs("#delAsset", host).addEventListener("click", () => deleteAsset(asset, () => { location.hash = "#/assets"; }));
  wireRows(host);
}

function inspectionsTable(rows, asset) {
  if (!rows.length) return UI.empty({
    icon: "◉", title: "No inspections yet",
    text: asset ? "Run one against this asset to populate the history." : "Create an inspection to begin.",
    actionHref: "#/inspections/new", actionLabel: "New inspection",
  });
  return `<table class="data"><thead><tr><th>Title</th><th>Media</th><th>Status</th><th>Stage</th>
      <th class="num">Frames</th><th class="num">Findings</th><th>Engine</th><th>Created</th></tr></thead>
    <tbody>${rows.map((row) => `<tr role="button" tabindex="0" data-href="#/inspections/${esc(row.id)}">
      <td class="cell-title">${esc(row.title)}<div class="cell-sub mono">${esc(row.id)}</div></td>
      <td class="tiny">${esc(row.media_kind)} · ${esc(row.source_name || "—")}</td>
      <td>${UI.statusTag(row.status)}</td>
      <td class="tiny">${esc(row.stage)}</td>
      <td class="num">${row.frames_done}/${row.frames_total}</td>
      <td class="num">${row.findings ? row.findings.length : "—"}</td>
      <td class="tiny">${engineBadge(row.sam_engine || "not run")}</td>
      <td class="muted tiny">${esc(Fmt.rel(row.created_at))}</td></tr>`).join("")}</tbody></table>`;
}

function findingsTable(rows) {
  if (!rows.length) return UI.empty({ icon: "✓", title: "No defects recorded", text: "Nothing has been detected on these inspections yet." });
  return `<table class="data"><thead><tr><th>Concept</th><th>Severity</th><th>Agreement</th><th class="num">Area</th><th>Status</th></tr></thead>
    <tbody>${rows.map((row) => `<tr role="button" tabindex="0" data-href="#/defects?search=${encodeURIComponent(row.concept)}">
      <td class="cell-title">${esc(row.concept)}<div class="cell-sub">${esc(row.inspection_id)}</div></td>
      <td>${UI.severity(row.severity)}</td>
      <td class="num">${Fmt.pct(row.agreement)}</td>
      <td class="num">${row.area_m2 ? `${Fmt.num(row.area_m2)} m²` : "—"}</td>
      <td><span class="badge">${esc(row.status)}</span>${row.needs_human_review ? ' <span class="badge badge-warn">review</span>' : ""}</td>
    </tr>`).join("")}</tbody></table>`;
}

function wireRows(root) {
  qsa("tr[data-href]", root).forEach((row) => {
    row.addEventListener("click", () => { location.hash = row.dataset.href; });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); location.hash = row.dataset.href; }
    });
  });
}

/* ------------------------------------------------------------- inspections */

async function viewInspections(host, params, query) {
  const filters = { search: query.search || "", status: query.status || "", page: Number(query.page || 1) };
  host.innerHTML = `
    <div class="stack">
      <div class="card"><div class="card-body"><div class="filters">
        <div class="field search"><label for="iSearch">Search</label>
          <input id="iSearch" type="search" value="${esc(filters.search)}" placeholder="title, asset or inspection id"></div>
        <div class="field"><label for="iStatus">Status</label><select id="iStatus">
          <option value="">Any status</option>${selectOptions(["queued", "running", "completed", "failed", "cancelled"], filters.status)}</select></div>
        <div class="row-end"><a class="button button-primary" href="#/inspections/new">+ New inspection</a></div>
      </div></div></div>
      <div class="card card-flush"><div id="iTable">${UI.skeleton(6)}</div><div class="pagination" id="iPager"></div></div>
    </div>`;

  qs("#iSearch", host).addEventListener("input", debounce((event) => readQuery({ search: event.target.value.trim(), page: "" })));
  qs("#iStatus", host).addEventListener("change", (event) => readQuery({ status: event.target.value, page: "" }));

  const page = await Api.get(Api.query("/api/inspections", filters));
  navCount("inspections", page.items.filter((row) => row.status === "running" || row.status === "queued").length || null);
  const table = qs("#iTable", host);
  if (!page.items.length) {
    table.innerHTML = UI.empty({ icon: "◉", title: "No inspections match", text: "Adjust the filters or start a new run.", actionHref: "#/inspections/new", actionLabel: "New inspection" });
    return;
  }
  table.innerHTML = inspectionsTable(page.items);
  UI.pager(qs("#iPager", host), page, (next) => readQuery({ page: next }));
  wireRows(table);
  if (page.items.some((row) => row.status === "running" || row.status === "queued")) every(8000, () => Router.go());
}

/* ---------------------------------------------------------- new inspection */

async function viewNewInspection(host, params) {
  const config = Store.config;
  const [assetPage, samples] = await Promise.all([
    Api.get(Api.query("/api/assets", { status: "active", page_size: 100 })),
    Api.get("/api/samples"),
  ]);

  const state = {
    mode: params.asset ? "upload" : "upload",
    files: [],
    sample: "",
    prompts: [],
    assetId: params.asset || (assetPage.items[0] ? assetPage.items[0].id : ""),
    idempotency: crypto.randomUUID(),
    submitting: false,
  };

  host.innerHTML = `
    <form class="split" id="newForm" novalidate>
      <div class="stack" style="gap:20px">
        <div class="card"><div class="card-head"><h2>1 · What are we inspecting?</h2></div>
          <div class="card-body grid" style="gap:14px">
            <div class="field" data-field="asset_id"><label for="assetSelect">Asset *</label>
              <select id="assetSelect" name="asset_id">
                ${assetPage.items.length
                  ? assetPage.items.map((asset) => `<option value="${esc(asset.id)}" ${asset.id === state.assetId ? "selected" : ""}>${esc(asset.name)} · ${esc(typeLabel(asset.asset_type))}</option>`).join("")
                  : '<option value="">No assets registered</option>'}
              </select>
              <span class="hint">Not listed? <a href="#/assets">Register the asset first</a> — an inspection must attach to real context.</span>
              <span class="error-text" data-error></span></div>
            <div class="field" data-field="title"><label for="titleInput">Inspection title</label>
              <input id="titleInput" name="title" type="text" maxlength="140" placeholder="Leave blank to use the asset name">
              <span class="error-text" data-error></span></div>
            <div class="field" data-field="pixels_per_meter"><label for="ppmInput">Pixels per metre (optional)</label>
              <input id="ppmInput" name="pixels_per_meter" type="number" step="0.1" min="0.1" max="100000" placeholder="e.g. 120">
              <span class="hint">Enables square-metre defect areas. Without it, size is reported in pixels only.</span>
              <span class="error-text" data-error></span></div>
          </div></div>

        <div class="card"><div class="card-head"><h2>2 · Show me the surface</h2>
          <span class="muted tiny row-end">max ${config.limits.max_upload_mb} MB · ${config.limits.max_frames} frames</span></div>
          <div class="card-body stack">
            <div class="segmented" role="tablist" id="intakeTabs">
              <button type="button" class="is-active" data-mode="upload" role="tab" aria-selected="true">Upload media</button>
              <button type="button" data-mode="sample" role="tab" aria-selected="false">Bundled sample</button>
              <button type="button" data-mode="video" role="tab" aria-selected="false">Extract frames from a clip</button>
            </div>

            <div data-pane="upload">
              <label class="dropzone" id="drop" tabindex="0">
                <div class="dz-ico">⤒</div>
                <div class="dz-title">Drop one image, or several frames</div>
                <div class="tiny">JPG · PNG · WEBP · TIFF · BMP · MP4/MOV — or click to browse</div>
                <input type="file" id="fileInput" multiple accept="${config.choices.accepted_suffixes.join(",")}">
              </label>
              <div class="stack" id="fileList" style="margin-top:12px"></div>
            </div>

            <div data-pane="sample" class="hide">
              <div class="sample-grid" id="sampleGrid"></div>
              <p class="tiny muted" style="margin-top:10px">Samples ship with the repository. In simulation mode the bundled ground truth
                is used to place masks on the visible defect, so overlays line up without a provider call.</p>
            </div>

            <div data-pane="video" class="hide">
              <div class="field"><label for="clipInput">Video clip (MP4/MOV, H.264)</label>
                <input id="clipInput" type="file" accept=".mp4,.mov,video/mp4,video/quicktime"></div>
              <div id="sampler" class="hide">
                <div class="sampler">
                  <video id="clipPlayer" muted playsinline controls></video>
                  <div class="stack">
                    <div class="field"><label for="frameCount">Frames to sample</label>
                      <input id="frameCount" type="number" min="2" max="${config.limits.max_frames}" value="6"></div>
                    <div class="field"><label for="quality">Capture quality</label>
                      <select id="quality"><option value="0.82">Balanced</option><option value="0.92">High</option><option value="0.7">Small</option></select></div>
                    <button class="button button-primary" type="button" id="extract">Extract frames</button>
                    <p class="tiny muted">Duration <b id="clipDuration">—</b>. Frames are decoded in your browser
                      and uploaded as stills — this build has no server-side ffmpeg.</p>
                  </div>
                </div>
                <div class="sampler-grid" id="shots"></div>
              </div>
            </div>

            <div class="risk-flag" id="threeDNote">
              <span>ℹ</span><span>3D model files (.glb, .gltf, .obj, .ply) are <b>not accepted</b> by this build: no 3D parser or
              viewer is installed, so a model would silently render as a broken preview. Photograph the asset instead —
              depth is estimated from pixels-per-metre, not from a mesh.</span>
            </div>
          </div></div>

        <div class="card"><div class="card-head"><h2>3 · What should the segmenter look for?</h2>
          <span class="muted tiny row-end">up to ${config.limits.max_prompts} prompts</span></div>
          <div class="card-body stack">
            <div class="field" data-field="prompts">
              <div class="row">
                <label for="promptInput" class="field-label" style="flex:1">Concept prompts</label>
              </div>
              <div class="row"><input id="promptInput" type="text" maxlength="80" placeholder="asphalt crack" style="flex:1;min-width:220px">
                <button class="button" type="button" id="addPrompt">Add</button></div>
              <span class="hint">Name the thing to segment (“pothole”, “exposed rebar”), not an instruction. Multiple prompts on the
                same region raise corroboration and agreement.</span>
              <span class="error-text" data-error></span>
            </div>
            <div class="row" id="promptChips"></div>
            <div id="promptAdvice" class="tiny muted"></div>
          </div></div>
      </div>

      <div class="stack">
        <div class="card"><div class="card-head"><h2>Ready</h2></div>
          <div class="card-body stack">
            <dl class="kv">
              <dt>Mode</dt><dd>${config.live ? engineBadge(config.sam_model) : engineBadge("simulated")}</dd>
              <dt>Segmentation</dt><dd>${esc(config.engine_labels.segmentation)}</dd>
              <dt>Reasoning</dt><dd>${esc(config.engine_labels.reasoning)}</dd>
              <dt>Prompts</dt><dd id="sumPrompts">0</dd>
              <dt>Media</dt><dd id="sumMedia">none</dd>
            </dl>
            <div class="progress" id="uploadBar" hidden><i style="width:0%"></i></div>
            <p class="tiny muted" id="uploadState"></p>
            <button class="button button-primary button-block" type="submit" id="submitBtn" disabled>Start inspection</button>
            <p class="tiny muted">The job is queued and runs on the server; you can follow its progress live and leave the page.</p>
          </div></div>
        <div class="card"><div class="card-body tiny muted stack" style="gap:8px">
          <b>What happens next</b>
          <span>validating → preprocessing → detecting (SAM 3.1 per concept, per frame) → aggregating (IoU consensus,
          cross-frame merging) → reasoning (asset context + control documents) → generating_report (work orders as
          drafts, never auto-approved).</span>
        </div></div>
      </div>
    </form>`;

  qs("#assetSelect", host).addEventListener("change", (event) => { state.assetId = event.target.value; validate(); });

  /* ---- prompts */
  const chipsHost = qs("#promptChips", host);
  const advice = qs("#promptAdvice", host);
  const verbLead = /^(find|detect|look|check|show|search|is|are|the|a|an|please|can you)\b/i;

  function paintPrompts() {
    chipsHost.innerHTML = state.prompts.map((prompt, index) => `<span class="chip">${esc(prompt)}
      <button type="button" aria-label="Remove ${esc(prompt)}" data-index="${index}">✕</button></span>`).join("");
    qsa("button", chipsHost).forEach((button) => button.addEventListener("click", () => {
      state.prompts.splice(Number(button.dataset.index), 1);
      paintPrompts(); validate();
    }));
    qs("#sumPrompts", host).textContent = state.prompts.length || "0 (defaults by asset type)";
    advice.innerHTML = "";
    if (!state.prompts.length) return;
    const risky = state.prompts.filter((prompt) => verbLead.test(prompt) || prompt.split(" ").length > 5);
    if (risky.length) {
      advice.appendChild(elFrom(`<div class="risk-flag"><span>⚠</span><span>
        ${esc(risky.join(", "))} look like instructions rather than concepts. The server normalises prompts, but a
        plain noun phrase (“longitudinal crack”) segments better.</span></div>`));
    }
  }

  function addPrompt() {
    const input = qs("#promptInput", host);
    const value = input.value.trim().replace(/\s+/g, " ");
    if (!value) return;
    if (value.length > 80) { UI.toast("warn", "Prompt too long", "Keep prompts under 80 characters."); return; }
    if (state.prompts.length >= config.limits.max_prompts) { UI.toast("warn", "Prompt limit", `At most ${config.limits.max_prompts} prompts per run.`); return; }
    if (state.prompts.some((item) => item.toLowerCase() === value.toLowerCase())) { UI.toast("warn", "Already added", value); return; }
    state.prompts.push(value);
    input.value = "";
    input.focus();
    paintPrompts(); validate();
  }
  qs("#addPrompt", host).addEventListener("click", addPrompt);
  qs("#promptInput", host).addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addPrompt(); }
  });

  /* ---- intake modes */
  const tabs = qs("#intakeTabs", host);
  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (!button) return;
    state.mode = button.dataset.mode;
    qsa("button", tabs).forEach((node) => {
      const active = node === button;
      node.classList.toggle("is-active", active);
      node.setAttribute("aria-selected", String(active));
    });
    qsa("[data-pane]", host).forEach((pane) => pane.classList.toggle("hide", pane.dataset.pane !== state.mode));
    validate();
  });

  /* ---- upload */
  const drop = qs("#drop", host);
  const fileInput = qs("#fileInput", host);
  fileInput.addEventListener("change", () => takeFiles(Array.from(fileInput.files)));
  ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (event) => {
    event.preventDefault(); drop.classList.add("is-over");
  }));
  ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, (event) => {
    event.preventDefault(); drop.classList.remove("is-over");
  }));
  drop.addEventListener("drop", (event) => takeFiles(Array.from(event.dataTransfer.files || [])));
  drop.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); fileInput.click(); } });

  const accepted = new Set(config.choices.accepted_suffixes);
  function suffixOf(name) { const dot = name.lastIndexOf("."); return dot < 0 ? "" : name.slice(dot).toLowerCase(); }

  function takeFiles(files) {
    if (!files.length) return;
    const kept = [];
    files.forEach((file) => {
      const suffix = suffixOf(file.name);
      if (!accepted.has(suffix)) { UI.toast("warn", "Unsupported file type", `${file.name} (${suffix || "no extension"})`); return; }
      if (file.size > config.limits.max_upload_mb * 1024 * 1024) { UI.toast("warn", "File too large", `${file.name} exceeds ${config.limits.max_upload_mb} MB`); return; }
      kept.push(file);
    });
    state.files = kept;
    qs("#fileList", host).innerHTML = kept.map((file, index) => `
      <div class="upload-card"><div class="u-meta">
        <div class="strong">${esc(file.name)}</div>
        <div class="tiny muted mono">${esc(Fmt.bytes(file.size))} · ${esc(suffixOf(file.name))}</div>
      </div><button class="icon-button" type="button" data-index="${index}" aria-label="Remove file">✕</button></div>`).join("");
    qsa("#fileList button", host).forEach((button) => button.addEventListener("click", () => {
      state.files.splice(Number(button.dataset.index), 1);
      takeFiles(state.files.slice());
    }));
    validate();
  }

  /* ---- samples */
  const sampleHost = qs("#sampleGrid", host);
  sampleHost.innerHTML = samples.length ? samples.map((sample) => `
    <button class="sample-card" type="button" data-stem="${esc(sample.stem)}">
      <div class="frame-pending" style="height:70px">${esc(sample.kind)}</div>
      <div class="s-meta"><b>${esc(sample.stem)}</b>
        <div class="tiny muted">${esc(Fmt.bytes(sample.bytes))}${sample.has_ground_truth ? " · ground truth" : ""}</div>
      </div></button>`).join("")
    : `<p class="muted tiny">No samples found under <code>samples/</code>. Run <code>python samples/generate_samples.py</code> or upload media.</p>`;
  sampleHost.addEventListener("click", (event) => {
    const card = event.target.closest(".sample-card");
    if (!card) return;
    state.sample = state.sample === card.dataset.stem ? "" : card.dataset.stem;
    qsa(".sample-card", sampleHost).forEach((node) => node.classList.toggle("is-active", node.dataset.stem === state.sample));
    validate();
  });

  /* ---- browser frame extraction */
  const clipInput = qs("#clipInput", host);
  const clipPlayer = qs("#clipPlayer", host);
  let clipUrl = null;
  clipInput.addEventListener("change", () => {
    const file = clipInput.files[0];
    if (!file) return;
    if (![".mp4", ".mov"].includes(suffixOf(file.name))) { UI.toast("warn", "Clip not decodable here", "Extract frames from an MP4 or MOV file."); return; }
    if (file.size > config.limits.max_upload_mb * 1024 * 1024) { UI.toast("warn", "Clip too large", `${Fmt.bytes(file.size)}`); return; }
    if (clipUrl) URL.revokeObjectURL(clipUrl);
    clipUrl = URL.createObjectURL(file);
    clipPlayer.src = clipUrl;
    clipPlayer.addEventListener("loadedmetadata", () => {
      qs("#sampler", host).classList.remove("hide");
      qs("#clipDuration", host).textContent = `${Fmt.clock(clipPlayer.duration * 1000)}`;
    }, { once: true });
    clipPlayer.addEventListener("error", () => UI.toast("error", "Browser could not decode this clip", "The container or codec is unsupported — try an H.264 MP4."), { once: true });
  });

  qs("#extract", host).addEventListener("click", () => extractFrames(clipPlayer, host, state, () => validate()));

  /* ---- submit */
  const submitBtn = qs("#submitBtn", host);
  const bar = qs("#uploadBar", host);
  const barFill = qs("i", bar);
  const uploadState = qs("#uploadState", host);

  function validate() {
    const hasMedia = state.mode === "upload" ? state.files.length > 0
      : state.mode === "sample" ? Boolean(state.sample) : state.files.length > 0;
    const problems = [];
    if (!state.assetId) problems.push("choose an asset");
    if (!hasMedia) problems.push(state.mode === "sample" ? "pick a sample" : "attach media");
    qs("#sumMedia", host).textContent = state.mode === "sample"
      ? `sample · ${state.sample || "none"}`
      : state.files.length > 1 ? `${state.files.length} frames`
      : state.files.length === 1 ? `${state.files[0].name} (${Fmt.bytes(state.files[0].size)})` : "none";
    submitBtn.disabled = problems.length > 0 || state.submitting;
    submitBtn.textContent = state.submitting ? "Uploading…" : problems.length ? "Start inspection" : "Start inspection";
    if (!uploadState.textContent && problems.length) uploadState.textContent = "";
    return problems;
  }
  paintPrompts();
  validate();

  qs("#newForm", host).addEventListener("submit", async (event) => {
    event.preventDefault();
    const problems = validate();
    if (problems.length) { UI.toast("warn", "Not ready", `Still needed: ${problems.join(", ")}.`); return; }

    const payload = {
      asset_id: state.assetId,
      title: qs("#titleInput", host).value.trim(),
      prompts: state.prompts,
      idempotency_key: state.idempotency,
    };
    const ppm = qs("#ppmInput", host).value.trim();
    if (ppm) payload.pixels_per_meter = Number(ppm);

    state.submitting = true;
    bar.hidden = false;
    barFill.style.width = "2%";
    uploadState.textContent = "Uploading media…";
    submitBtn.disabled = true;
    try {
      const result = await uploadInspection({
        payload,
        sample: state.mode === "sample" ? state.sample : "",
        files: state.mode === "sample" ? [] : state.files,
        frameTimes: state.frame_times || [],
      }, (ratio) => {
        barFill.style.width = `${Math.max(2, Math.round(ratio * 100))}%`;
        uploadState.textContent = ratio >= 1 ? "Server is queuing the job…" : `Uploading ${Math.round(ratio * 100)}%`;
      });
      barFill.style.width = "100%";
      if (result && result.deduplicated) UI.toast("warn", "Duplicate submission detected", "An inspection with this idempotency key already exists.");
      location.hash = `#/inspections/${result.inspection.id}`;
    } catch (error) {
      state.submitting = false;
      bar.hidden = true;
      uploadState.textContent = "";
      UI.fieldErrors(qs("#newForm", host), error);
      UI.error(error, "Inspection could not be created");
      if (error.code === "unsupported_input") banner("error", error.message);
      validate();
    }
  });

  wireRows(host);
}

async function extractFrames(player, host, state, done) {
  if (!player || !player.duration) { UI.toast("warn", "Nothing to sample yet", "Load a clip first."); return; }
  const count = Math.max(2, Math.min(Store.config.limits.max_frames, Number(qs("#frameCount", host).value) || 4));
  const quality = Number(qs("#quality", host).value);
  const canvas = document.createElement("canvas");
  const scale = Math.min(1, 1280 / (player.videoWidth || 1280));
  canvas.width = Math.round((player.videoWidth || 640) * scale);
  canvas.height = Math.round((player.videoHeight || 480) * scale);
  const ctx = canvas.getContext("2d");
  const shots = qs("#shots", host);
  shots.innerHTML = "";
  state.files = [];
  state.frame_times = [];

  player.pause();
  for (let index = 0; index < count; index += 1) {
    const time = (player.duration * (index + 0.5)) / count;
    try { await seekTo(player, time); }
    catch { UI.toast("error", "Frame decode failed", `Could not decode at ${Fmt.clock(time * 1000)}.`); break; }
    ctx.drawImage(player, 0, 0, canvas.width, canvas.height);
    const blob = await canvasToBlob(canvas, quality);
    if (!blob) continue;
    const ms = Math.round(time * 1000);
    const file = new File([blob], `frame_${String(index).padStart(3, "0")}.jpg`, { type: "image/jpeg" });
    file.__preview = URL.createObjectURL(blob);
    state.files.push(file);
    state.frame_times.push(ms);
    shots.appendChild(elFrom(`<div class="shot"><img src="${file.__preview}" alt="frame ${index} at ${ms}ms">
      <button type="button" aria-label="Discard sample">✕</button></div>`));
  }
  const kept = state.files.slice();
  qsa("#shots .shot button", host).forEach((button, index) => button.addEventListener("click", () => {
    state.files = kept.filter((_, position) => position !== index);
    state.frame_times = state.frame_times.slice();
    button.closest(".shot").remove();
    done();
  }));
  UI.toast("ok", `${state.files.length} frame(s) sampled`, "Frames are uploaded as stills — no server-side decode is needed.");
  done();
}

function seekTo(player, time) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { player.removeEventListener("seeked", onSeek); reject(new Error("seek timed out")); }, 8000);
    const onSeek = () => {
      clearTimeout(timer);
      player.removeEventListener("seeked", onSeek);
      /* Never rAF here: a backgrounded tab freezes animation frames and the sampler would hang. */
      setTimeout(resolve, 40);
    };
    player.addEventListener("seeked", onSeek);
    player.currentTime = time;
  });
}

function canvasToBlob(canvas, quality) {
  return new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), "image/jpeg", quality));
}

/* --------------------------------------------------------- inspection view */

async function viewInspection(host, params) {
  const id = decodeURIComponent(params.id);
  const inspection = await Api.get(`/api/inspections/${encodeURIComponent(id)}`);
  const config = Store.config;
  const stageIndex = config.choices.stages.indexOf(inspection.stage);

  const frameOf = (finding) => (finding ? finding.source_frame ?? 0 : 0);
  const firstFinding = inspection.findings[0] || null;
  const selected = { findingId: firstFinding ? firstFinding.id : "", frame: frameOf(firstFinding) };

  host.innerHTML = `
    <div class="stack">
      <div class="card"><div class="card-body">
        <div class="spread">
          <div>
            <div class="row"><h2>${esc(inspection.title)}</h2>${UI.statusTag(inspection.status)}</div>
            <p class="muted tiny"><a href="#/assets/${esc(inspection.asset_id)}">${esc(inspection.asset_name)}</a>
              · ${esc(inspection.media_kind)} · ${esc(inspection.source_name || "no media")}
              · <span class="mono">${esc(inspection.id)}</span></p>
          </div>
          <div class="row" id="jobActions"></div>
        </div>
        <div class="progress" style="margin:14px 0 8px" id="jobProgress"><i style="width:${Math.round((inspection.stage_progress || 0) * 100)}%"></i></div>
        <div class="ladder" id="ladder"></div>
        <div class="row" style="margin-top:12px">
          <span class="badge">${esc(inspection.mode)} mode</span>
          <span>segmentation ${engineBadge(inspection.sam_engine)}</span>
          <span>reasoning ${engineBadge(inspection.spark_engine)}</span>
          <span class="muted tiny">frames ${inspection.frames_done}/${inspection.frames_total} · ${inspection.width}×${inspection.height}</span>
          ${inspection.pixels_per_meter ? `<span class="badge badge-info">${Fmt.num(inspection.pixels_per_meter, 1)} px/m</span>` : ""}
        </div>
      </div></div>

      ${inspection.error ? `<div class="card" style="border-color:#f2c4c4"><div class="card-body stack">
        <h3 style="color:var(--danger)">Run failed</h3>
        <pre class="mono">${esc(typeof inspection.error === "string" ? inspection.error : JSON.stringify(inspection.error, null, 2))}</pre>
        <div class="row"><button class="button button-primary" type="button" data-retry="full">Retry full run</button>
        <button class="button" type="button" data-retry="reasoning">Retry reasoning only</button></div>
      </div></div>` : ""}

      ${(inspection.warnings || []).length ? `<div class="card"><div class="card-body stack">
        <h3>Warnings recorded by the server</h3>
        ${inspection.warnings.map((note) => `<div class="risk-flag"><span>⚠</span><span>${esc(note)}</span></div>`).join("")}
      </div></div>` : ""}

      <div id="jobConsole"></div>

      <div class="evidence" id="evidencePane"></div>

      <div class="split">
        <div class="card card-flush"><div class="card-head"><h2>Artifacts</h2>
          <span class="muted tiny row-end">written by this run</span></div>
          <div class="card-body" id="artifactList"></div></div>
        <div class="card card-flush"><div class="card-head"><h2>Frames</h2>
          <span class="muted tiny row-end">${inspection.frames_total} analysed</span></div>
          <div class="card-body"><div class="frames" id="frameGrid"></div></div></div>
      </div>

      <div class="card card-flush"><div class="card-head"><h2>Work orders from this run</h2>
        <div class="row-end"><button class="button button-sm" type="button" id="raiseOrder">+ Raise work order</button></div></div>
        <div class="table-wrap" id="orderList"></div>
      </div>
    </div>`;

  /* --- job actions ------------------------------------------------------ */
  const actions = qs("#jobActions", host);
  const running = ["queued", "running"].includes(inspection.status);
  function paintActions() {
    actions.innerHTML = "";
    const add = (label, cls, fn, enabled = true) => {
      const button = elFrom(`<button class="button ${cls}" type="button" ${enabled ? "" : "disabled"}>${esc(label)}</button>`);
      if (enabled) button.addEventListener("click", fn);
      actions.appendChild(button);
      return button;
    };
    add("Refresh", "button-ghost", () => Router.go());
    if (running) add("Cancel job", "button-danger", () => cancelJob(id));
    else add("Retry full run", "", () => retryJob(id, "full"), Boolean(inspection.source_path));
    if (inspection.spark_engine === "unavailable") add("Retry reasoning", "button-primary", () => retryJob(id, "reasoning"));
    if (!running) add("Delete", "button-danger", () => deleteJob(id, inspection.title));
  }
  paintActions();

  /* --- stage ladder ----------------------------------------------------- */
  const statesByStage = {};
  function paintLadder() {
    const ladder = qs("#ladder", host);
    const reached = config.choices.stages.reduce((best, stage, index) => (statesByStage[stage] ? index : best), -1);
    ladder.innerHTML = config.choices.stages.map((stage, index) => {
      let state = "pending";
      if (statesByStage[stage]) state = statesByStage[stage] === "running" && index < reached ? "done" : statesByStage[stage];
      else if (inspection.status === "completed") state = "done";
      else if (inspection.status === "cancelled" && index <= stageIndex) state = "skipped";
      else if (index < stageIndex) state = "done";
      else if (index === stageIndex && inspection.status === "running") state = "running";
      else if (index === stageIndex && inspection.status === "failed") state = "failed";
      const icon = { done: "✓", running: "◌", failed: "!", skipped: "×", pending: String(index + 1) }[state];
      return `<span class="ladder-step ${state}"><span class="step-ico">${icon}</span>${esc(labelOf(stage))}</span>`;
    }).join("");
    const pct = Math.round((inspection.stage_progress || 0) * 100);
    const bar = qs("#jobProgress", host);
    qs("i", bar).style.width = `${pct}%`;
    bar.classList.toggle("is-error", inspection.status === "failed");
  }
  paintLadder();

  /* --- console ---------------------------------------------------------- */
  const consoleHost = qs("#jobConsole", host);
  consoleHost.innerHTML = `<div class="card"><div class="card-head"><h2>Processing log</h2>
    <span class="badge ${running ? "badge-info" : ""}" id="streamState">${running ? "connecting…" : "replay"}</span>
    <span class="muted tiny row-end" id="elapsed"></span></div>
    <div class="card-body"><div class="console" id="console" role="log" aria-live="polite"></div></div></div>`;
  const consoleNode = qs("#console", host);

  function logLine(kind, text, stamp, extra = "") {
    const line = elFrom(`<div class="line"><span class="t">${esc(stamp)}</span>
      <span class="lv-${kind}">${esc(kind)}</span><span>${esc(text)} ${extra}</span></div>`);
    consoleNode.appendChild(line);
    consoleNode.scrollTop = consoleNode.scrollHeight;
    if (qsa(".line", consoleNode).length > 240) qsa(".line", consoleNode)[0].remove();
  }
  const stampOf = (at) => new Date((at || Date.now() / 1000) * 1000).toLocaleTimeString();

  const wasRunning = running;
  closeStream = followJob(id, {
    each: (event) => {
      if (event.type === "stage") {
        statesByStage[event.stage] = event.status === "completed" ? "done" : event.status;
        paintLadder();
        if (event.summary) logLine("info", `${labelOf(event.stage)} — ${event.summary}`, stampOf(event.at));
        if (event.metrics && event.metrics.frames !== undefined) logLine("ok", `${event.metrics.frames} frame(s) prepared`, stampOf(event.at));
        if (event.progress !== null && event.progress !== undefined) {
          qs("i", qs("#jobProgress", host)).style.width = `${Math.round(event.progress * 100)}%`;
        }
      }
      if (event.type === "frame") {
        logLine("info", `frame ${event.index}/${event.total - 1} · ${event.done}/${event.total}`
          + (event.regions === undefined ? ` · ${event.size ? event.size.join("×") : "prepared"}` : ` · ${event.detections} detection(s), ${event.regions} region(s)`),
        stampOf(event.at));
      }
      if (event.type === "log") logLine(event.level, event.message, stampOf(event.at));
      if (event.type === "findings") logLine("ok", `${event.count} distinct defect(s) after consensus`, stampOf(event.at));
      if (event.elapsed_ms) qs("#elapsed", host).textContent = `elapsed ${Fmt.clock(event.elapsed_ms)}`;
    },
    done: (event) => {
      const state = qs("#streamState", host);
      state.textContent = "finished";
      state.className = "badge";
      logLine(event.status === "completed" ? "ok" : "error",
        `job ${event.status}${event.error ? ` · ${event.error.code}: ${event.error.message}` : ""}`, stampOf(event.at));
      closeStream = null;
      if (wasRunning) setTimeout(() => Router.go(), 500);
    },
    error: () => {
      qs("#streamState", host).textContent = "stream closed";
      if (wasRunning) every(6000, () => Router.go());
    },
  });

  /* --- evidence --------------------------------------------------------- */
  paintEvidence();

  function paintEvidence() {
    const pane = qs("#evidencePane", host);
    if (!inspection.findings.length) {
      pane.innerHTML = `<div class="card"><div class="card-body">${
        running
          ? `<div class="stack"><h3>Nothing detected yet</h3><p class="muted tiny">Detections appear here as each frame finishes.</p></div>`
          : UI.empty({ icon: "✓", title: "No defect region survived consensus",
              text: "The segmenter returned no region above the confidence threshold for these prompts. That is a valid result — no overlay is drawn when there is no mask." })
      }</div></div>`;
      return;
    }
    const frame = selected.frame === undefined ? frameOf(inspection.findings[0]) : selected.frame;
    const onFrame = inspection.findings.filter((finding) => frameOf(finding) === frame);
    const chosen = inspection.findings.find((finding) => finding.id === selected.findingId) || onFrame[0];
    const sourceSize = (chosen && chosen.source_size && chosen.source_size[0]) ? chosen.source_size : [inspection.width || 1, inspection.height || 1];

    pane.innerHTML = `
      <div class="stage-view">
        <div class="media-box">
          <img src="${Api.mediaUrl(id, frame)}" alt="Frame ${frame} as segmented by the server" onload="this.parentNode.style.minHeight='auto'">
          ${overlaySvg(onFrame, selected.findingId, sourceSize)}
        </div>
        <div class="stage-foot">
          <span class="legend"><i></i> mask from the returned SAM payload</span>
          <span>frame ${frame}${chosen && chosen.frames_seen && chosen.frames_seen.length > 1 ? ` · seen in ${chosen.frames_seen.length} frame(s)` : ""}</span>
          <span class="row-end" id="variantSwitch"></span>
        </div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-head"><h3>${esc(chosen ? chosen.concept : "Defect")}</h3>
          <span class="row-end">${chosen ? UI.severity(chosen.severity) : ""}</span></div>
          <div class="card-body stack" id="defectDetail"></div></div>
        <div class="card"><div class="card-head"><h3>Defects on this run</h3>
          <span class="muted tiny row-end">${inspection.findings.length}</span></div>
          <div class="card-body"><div class="defect-list" id="defectList"></div></div></div>
      </div>`;

    const variantHost = qs("#variantSwitch", host);
    let variant = "prepared";
    variantHost.innerHTML = `<span class="segmented"><button type="button" class="is-active" data-variant="prepared">Analysed pixels</button>
      <button type="button" data-variant="source">Original</button></span>`;
    qsa("button", variantHost).forEach((button) => button.addEventListener("click", () => {
      variant = button.dataset.variant;
      qsa("button", variantHost).forEach((node) => node.classList.toggle("is-active", node === button));
      qs("img", pane).src = Api.mediaUrl(id, frame, variant);
    }));

    const listHost = qs("#defectList", host);
    listHost.innerHTML = inspection.findings.map((finding) => `
      <button class="defect-item sev-${esc(finding.severity)} ${finding.id === selected.findingId ? "is-selected" : ""}" type="button" data-id="${esc(finding.id)}">
        <span class="defect-top">${UI.severity(finding.severity)}<span class="defect-concept">${esc(finding.concept)}</span>
          ${finding.needs_human_review ? '<span class="badge badge-warn">needs review</span>' : ""}
          ${finding.status !== "open" ? `<span class="badge">${esc(finding.status)}</span>` : ""}</span>
        <span class="defect-facts">
          <div>agreement<b>${Fmt.pct(finding.agreement)}</b></div>
          <div>confidence<b>${Fmt.pct(finding.confidence)}</b></div>
          <div>area<b>${finding.area_m2 ? `${Fmt.num(finding.area_m2)} m²` : "px only"}</b></div>
          <div>frame<b>${frameOf(finding)}</b></div>
        </span></button>`).join("");
    qsa("button", listHost).forEach((button) => button.addEventListener("click", () => {
      const finding = inspection.findings.find((item) => item.id === button.dataset.id);
      selected.findingId = finding.id;
      selected.frame = frameOf(finding);
      paintEvidence();
    }));

    paintDetail(chosen);
  }

  function paintDetail(finding) {
    const host2 = qs("#defectDetail", host);
    if (!finding) { host2.innerHTML = `<p class="muted tiny">Select a defect.</p>`; return; }
    const maskNote = finding.mask && finding.mask.svg
      ? `${Fmt.pct(finding.mask.fill)} of the bounding box is mask-positive`
      : "no mask polygon was returned for this region";
    host2.innerHTML = `
      <div class="observed">
        <h4>Observed in the media</h4>
        <dl class="kv">
          <dt>Prompt(s)</dt><dd>${esc((finding.corroborated_by || []).join(", ") || finding.prompt || "—")}</dd>
          <dt>Agreement</dt><dd>${Fmt.pct(finding.agreement)} of ${((finding.corroborated_by || []).length) || 1} matching prompt(s)</dd>
          <dt>Confidence</dt><dd>${Fmt.pct(finding.confidence)}</dd>
          <dt>Box (px)</dt><dd class="mono">${(finding.box || []).join(", ")}</dd>
          <dt>Mask</dt><dd>${esc(maskNote)}</dd>
          <dt>Frames seen</dt><dd>${(() => {
            const seen = finding.frames_seen || [];
            return seen.length ? `${seen.length} frame(s) · #${seen.join(", #")}` : `frame ${frameOf(finding)}`;
          })()}</dd>
          <dt>Area</dt><dd>${finding.area_m2 ? `${Fmt.num(finding.area_m2)} m²` : "not computed (no px/m)"}</dd>
          <dt>Detected by</dt><dd>${engineBadge(finding.detection_engine)}</dd>
        </dl>
      </div>
      <div class="interpreted">
        <h4>Model interpretation — not measured</h4>
        ${finding.reasoning_done ? `
          <p>${esc(finding.analysis || finding.cause || "No narrative was returned.")}</p>
          <dl class="kv" style="margin-top:8px">
            <dt>Likely cause</dt><dd>${esc(finding.cause || "—")}</dd>
            <dt>Severity</dt><dd>${UI.severity(finding.severity)} <span class="muted tiny">(${esc(finding.reasoning_engine || "rule engine")})</span></dd>
            <dt>Priority</dt><dd><span class="badge badge-priority">${esc(finding.priority)}</span></dd>
          </dl>` : `<p class="muted">Reasoning did not complete for this finding, so severity is
            <b>Unknown</b> and no work order was drafted. Retry the reasoning stage or triage it manually.</p>`}
        ${(finding.consequences || []).length ? `<div class="detail-block"><h4>If untreated</h4><ul>${
          finding.consequences.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
        ${(finding.recommendations || []).length ? `<div class="detail-block"><h4>Recommended action</h4><ul>${
          finding.recommendations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
        ${(finding.references_doc || []).length ? `<div class="detail-block"><h4>Documents consulted</h4><ul>${
          finding.references_doc.map((item) => `<li class="mono tiny">${esc(item)}</li>`).join("")}</ul></div>` : ""}
        ${(finding.uncertainties || []).length ? `<div class="detail-block"><h4>Stated uncertainty</h4><ul>${
          finding.uncertainties.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
      </div>
      <div class="row">
        <a class="button button-sm" href="#/defects?inspection=${encodeURIComponent(id)}">Triage in defect list</a>
        ${finding.needs_human_review ? '<span class="risk-flag"><span>⚑</span><span>Escalated: a person must review before this becomes authorised work.</span></span>' : ""}
      </div>`;
  }

  /* --- frames ----------------------------------------------------------- */
  const grid = qs("#frameGrid", host);
  const frames = inspection.frames || [];
  const tiles = frames.length
    ? frames.map((frame) => ({ index: frame.frame_index, meta: frame.source_time_ms ? Fmt.clock(frame.source_time_ms) : `${frame.width}×${frame.height}` }))
    : (inspection.frames_total === 1 ? [{ index: 0, meta: `${inspection.width}×${inspection.height} still` }] : []);
  if (tiles.length) {
    grid.innerHTML = tiles.map((tile) => `
      <button class="frame-tile ${tile.index === selected.frame ? "is-active" : ""}" type="button" data-frame="${tile.index}">
        <img loading="lazy" src="${Api.mediaUrl(id, tile.index)}" alt="frame ${tile.index}">
        <span class="frame-meta"><span>#${tile.index}</span><span>${esc(tile.meta)}</span></span>
      </button>`).join("");
    qsa("button", grid).forEach((button) => button.addEventListener("click", () => {
      selected.frame = Number(button.dataset.frame);
      const first = inspection.findings.find((finding) => frameOf(finding) === selected.frame);
      if (first) selected.findingId = first.id;
      paintEvidence();
      qsa("button", grid).forEach((node) => node.classList.toggle("is-active", node === button));
    }));
  } else {
    grid.innerHTML = `<p class="muted tiny">No frame records were stored for this run.</p>`;
  }

  /* --- artifacts -------------------------------------------------------- */
  const artHost = qs("#artifactList", host);
  const pdfRow = `<div class="artifact-row"><span class="a-ico">⎙</span>
    <div style="flex:1"><div class="a-name">Printable report (PDF)</div>
      <div class="a-meta mono">report.pdf · application/pdf · rendered from these rows on request</div></div>
    <a class="button button-sm" href="/api/inspections/${encodeURIComponent(id)}/report.pdf" download>Download</a>
  </div>`;
  artHost.innerHTML = pdfRow + ((inspection.artifacts || []).length ? inspection.artifacts.map((artifact) => `
    <div class="artifact-row"><span class="a-ico">⤓</span>
      <div style="flex:1"><div class="a-name">${esc(artifact.label || artifact.name)}</div>
        <div class="a-meta mono">${esc(artifact.name)} · ${esc(artifact.media_type)} · ${esc(Fmt.bytes(artifact.bytes))}</div></div>
      <a class="button button-sm" href="/api/inspections/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifact.name)}" download>Download</a>
    </div>`).join("") : `<p class="muted tiny">No run artifacts yet — they are written in the final stage.</p>`);

  /* --- orders ----------------------------------------------------------- */
  const orders = await Api.get(Api.query("/api/work-orders", { inspection_id: id, page_size: 50 }));
  qs("#orderList", host).innerHTML = ordersTable(orders.items);
  wireRows(host);
  qs("#raiseOrder", host).addEventListener("click", () => orderForm(inspection, () => Router.go()));

  qsa("[data-retry]", host).forEach((button) => button.addEventListener("click", () => retryJob(id, button.dataset.retry)));
}

function labelOf(stage) {
  return {
    queued: "Queued", validating: "Validating", preprocessing: "Preprocessing", detecting: "Detecting",
    aggregating: "Aggregating", reasoning: "Reasoning", generating_report: "Reporting", completed: "Completed",
  }[stage] || stage;
}

async function cancelJob(id) {
  const ok = await UI.confirm({
    title: "Cancel this run",
    message: "The engine stops at the next checkpoint. Detections already stored stay stored.",
    label: "Cancel job", tone: "danger",
  });
  if (!ok) return;
  try {
    const result = await Api.post(`/api/inspections/${encodeURIComponent(id)}/cancel`, {});
    UI.toast("warn", "Cancellation requested", `status: ${result.status}`);
    setTimeout(() => Router.go(), 700);
  } catch (error) { UI.error(error); }
}

async function retryJob(id, mode) {
  try {
    await Api.post(`/api/inspections/${encodeURIComponent(id)}/retry`, { mode });
    UI.toast("ok", mode === "reasoning" ? "Reasoning retry started" : "Full retry started", id);
    setTimeout(() => Router.go(), 500);
  } catch (error) { UI.error(error); }
}

async function deleteJob(id, title) {
  const ok = await UI.confirm({
    title: "Delete inspection",
    message: `Delete “${title}” with its findings, events and artifacts?`,
    label: "Delete inspection", requireText: id,
    details: "Work orders raised from it are deleted as well. Media files are purged.",
  });
  if (!ok) return;
  try {
    await Api.del(`/api/inspections/${encodeURIComponent(id)}?confirm=${encodeURIComponent(id)}`);
    UI.toast("ok", "Inspection deleted", id);
    location.hash = "#/inspections";
  } catch (error) { UI.error(error); }
}

function ordersTable(rows, options = {}) {
  if (!rows.length) return UI.empty({
    icon: "☰", title: "No work orders yet",
    text: "Work orders appear only after a run produces reasoned findings — and none is authorised until a person approves it.",
  });
  return `<table class="data"><thead><tr><th>Order</th><th>Asset</th><th>Priority</th><th>Status</th>
      <th>Team</th><th>Due</th>${options.compact ? "" : "<th>Inspection</th>"}<th></th></tr></thead>
    <tbody>${rows.map((row) => `<tr data-id="${esc(row.id)}">
      <td><b>${esc(row.title)}</b><div class="cell-sub mono">${esc(row.id)}</div></td>
      <td>${esc(row.asset_name || "—")}</td>
      <td><span class="badge badge-priority">${esc(row.priority)}</span></td>
      <td>${orderBadge(row.status)}${row.status === "pending_approval" ? '<div class="cell-sub">needs sign-off</div>' : ""}</td>
      <td class="tiny">${esc(row.assigned_team || "—")}</td>
      <td class="tiny">${esc(row.due_date || "—")}</td>
      ${options.compact ? "" : `<td class="tiny"><a href="#/inspections/${esc(row.inspection_id)}">${esc(row.inspection_id)}</a></td>`}
      <td><div class="row" style="justify-content:flex-end">
        <button class="button button-sm" type="button" data-act="open">${row.status === "pending_approval" ? "Review & approve" : "Review"}</button>
      </div></td></tr>`).join("")}</tbody></table>`;
}

/* ------------------------------------------------------------- defect list */

async function viewDefects(host, params, query) {
  const filters = {
    search: query.search || "", severity: query.severity || "", status: query.status || "",
    asset_id: query.asset || "", inspection_id: query.inspection || "",
    needs_review: query.review || "", page: Number(query.page || 1), page_size: 25,
  };
  const [page, assetPage] = await Promise.all([
    Api.get(Api.query("/api/findings", filters)),
    Api.get(Api.query("/api/assets", { status: "active", page_size: 100 })),
  ]);
  navCount("defects", filters.severity || filters.status ? null : page.total);

  host.innerHTML = `
    <div class="stack">
      <div class="card"><div class="card-body"><div class="filters">
        <div class="field search"><label for="dSearch">Search</label>
          <input id="dSearch" type="search" value="${esc(filters.search)}" placeholder="concept, cause or asset"></div>
        <div class="field"><label for="dSeverity">Severity</label><select id="dSeverity">
          <option value="">Any</option>${selectOptions(SEVERITIES, filters.severity)}</select></div>
        <div class="field"><label for="dStatus">Triage status</label><select id="dStatus">
          <option value="">Any</option>${selectOptions(["open", "triaged", "converted", "closed"], filters.status)}</select></div>
        <div class="field"><label for="dAsset">Asset</label><select id="dAsset">
          <option value="">All assets</option>${assetPage.items.map((asset) => `<option value="${esc(asset.id)}" ${asset.id === filters.asset_id ? "selected" : ""}>${esc(asset.name)}</option>`).join("")}</select></div>
        <div class="field"><label for="dReview">Review flag</label><select id="dReview">
          <option value="">Any</option><option value="1"${filters.needs_review === "1" ? " selected" : ""}>Needs human review</option>
          <option value="0"${filters.needs_review === "0" ? " selected" : ""}>Not escalated</option></select></div>
        <div class="row-end"><span class="badge">${page.total} finding(s)</span></div>
      </div></div></div>
      <div class="card card-flush"><div id="dTable">${UI.skeleton(6)}</div><div class="pagination" id="dPager"></div></div>
    </div>`;

  const map = { dSearch: "search", dSeverity: "severity", dStatus: "status", dAsset: "asset", dReview: "review" };
  Object.entries(map).forEach(([id, key]) => {
    const node = qs(`#${id}`, host);
    node.addEventListener("input" , key === "search" ? debounce((event) => readQuery({ search: event.target.value.trim(), page: "" })) : () => {});
    node.addEventListener("change", () => readQuery({ [key]: node.value, page: "" }));
  });

  const table = qs("#dTable", host);
  if (!page.items.length) {
    table.innerHTML = UI.empty({
      icon: "⚠", title: filters.search || filters.severity ? "No defect matches these filters" : "No defects recorded",
      text: "Detections land here after a run finishes the detecting stage.",
      actionHref: "#/inspections/new", actionLabel: "New inspection",
    });
    return;
  }
  table.innerHTML = `<table class="data"><thead><tr>
      <th>Concept</th><th>Asset</th><th>Severity</th><th>Agreement</th><th class="num">Confidence</th>
      <th class="num">Area</th><th>Engine</th><th>Status</th><th></th></tr></thead>
    <tbody>${page.items.map((row) => `<tr data-id="${esc(row.id)}">
      <td><b>${esc(row.concept)}</b><div class="cell-sub mono">${esc(row.id)}</div></td>
      <td class="tiny">${esc(row.asset_name || "—")}</td>
      <td>${UI.severity(row.severity)}</td>
      <td>${Fmt.pct(row.agreement)}<div class="cell-sub">${esc((row.corroborated_by || []).slice(0, 2).join(", "))}</div></td>
      <td class="num">${Fmt.pct(row.confidence)}</td>
      <td class="num">${row.area_m2 ? `${Fmt.num(row.area_m2)} m²` : "—"}</td>
      <td class="tiny">${engineBadge(row.detection_engine)}</td>
      <td><span class="badge">${esc(row.status)}</span>${row.needs_human_review ? ' <span class="badge badge-warn">review</span>' : ""}</td>
      <td style="text-align:right"><button class="button button-sm" type="button">Open</button></td>
    </tr>`).join("")}</tbody></table>`;
  UI.pager(qs("#dPager", host), page, (next) => readQuery({ page: next }));

  qsa("tbody tr", table).forEach((row) => row.addEventListener("click", () => {
    const finding = page.items.find((item) => item.id === row.dataset.id);
    findingDrawer(finding);
  }));
}

async function findingDrawer(finding) {
  const full = await Api.get(`/api/findings/${encodeURIComponent(finding.id)}`);
  const body = elFrom(`<div class="stack">
    <div class="row">${UI.severity(full.severity)}
      <span class="badge badge-priority">${esc(full.priority)}</span>
      <span class="badge">${esc(full.status)}</span>
      ${full.needs_human_review ? '<span class="badge badge-warn">needs human review</span>' : ""}</div>
    <div class="stage-view">
      <div class="media-box"><img src="${Api.mediaUrl(full.inspection_id, full.source_frame)}" alt="evidence for ${esc(full.concept)}">
      ${overlaySvg([full], full.id, full.source_size && full.source_size[0] ? full.source_size : [1, 1])}</div>
      <div class="stage-foot"><span class="legend"><i></i>returned mask</span>
        <span>frame ${full.source_frame}</span></div>
    </div>
    <div class="observed"><h4>Observed</h4><dl class="kv">
      <dt>Media</dt><dd>${esc(full.source_name || "—")}</dd>
      <dt>Prompt</dt><dd>${esc(full.prompt || "—")}</dd>
      <dt>Corroborated by</dt><dd>${esc((full.corroborated_by || []).join(", ") || "single signal")}</dd>
      <dt>Detections merged</dt><dd>${full.detections}</dd>
      <dt>Detected by</dt><dd>${engineBadge(full.detection_engine)}</dd>
      <dt>Reasoned by</dt><dd>${engineBadge(full.reasoning_engine, "not reasoned")}</dd></dl></div>
    <div class="interpreted"><h4>Interpretation</h4>
      <p>${esc(full.analysis || full.cause || "The reasoning stage produced no narrative for this finding.")}</p>
      ${(full.recommendations || []).length ? `<div class="detail-block"><h4>Recommended</h4><ul>${
        full.recommendations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
      ${(full.uncertainties || []).length ? `<div class="detail-block"><h4>Uncertainty</h4><ul>${
        full.uncertainties.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}</div>
    <div class="field"><label for="triageSelect">Triage this finding</label>
      <select id="triageSelect">${selectOptions(["open", "triaged", "closed"], full.status === "converted" ? "triaged" : full.status)}</select>
      <span class="hint">Status only changes by your action; “converted” is set by approving a work order.</span></div>
    ${actorField("triageActor", "Your name")}
    <div class="row"><button class="button button-primary" type="button" id="triageSave">Save triage</button>
      <a class="button" href="#/inspections/${esc(full.inspection_id)}">Open run</a></div>
  </div>`);

  const drawer = UI.drawer({
    title: full.concept, subtitle: `${full.asset_name || ""} · ${full.id}`, body,
  });

  qs("#triageSave", drawer.node).addEventListener("click", async (event) => {
    const name = qs("#triageActor input, #triageActorId", drawer.node);
    const actorName = (name && name.value.trim()) || "";
    if (actorName.length < 2) { UI.toast("warn", "Name required", "Triage is attributed — type your name."); return; }
    actor.set(actorName);
    event.target.disabled = true;
    try {
      await Api.patch(`/api/findings/${encodeURIComponent(full.id)}`, { status: qs("#triageSelect", drawer.node).value, actor: actorName });
      UI.toast("ok", "Finding triaged", full.id);
      drawer.close();
      Router.go();
    } catch (error) { UI.error(error); event.target.disabled = false; }
  });
}

/* -------------------------------------------------------------- work orders */

async function viewOrders(host, params, query) {
  const filters = { search: query.search || "", status: query.status || "", priority: query.priority || "", asset_id: query.asset || "", page: Number(query.page || 1), page_size: 25 };
  const [page, assetPage] = await Promise.all([
    Api.get(Api.query("/api/work-orders", filters)),
    Api.get(Api.query("/api/assets", { status: "active", page_size: 100 })),
  ]);
  navCount("orders", (await Api.get("/api/dashboard")).work_orders_awaiting || null);

  host.innerHTML = `
    <div class="stack">
      <div class="card"><div class="card-body"><div class="filters">
        <div class="field search"><label for="wSearch">Search</label>
          <input id="wSearch" type="search" value="${esc(filters.search)}" placeholder="title, description or asset"></div>
        <div class="field"><label for="wStatus">Status</label><select id="wStatus">
          <option value="">Any</option>${ORDER_STATUSES.map((status) => `<option value="${status}"${filters.status === status ? " selected" : ""}>${esc(status.replace("_", " "))}</option>`).join("")}</select></div>
        <div class="field"><label for="wPriority">Priority</label><select id="wPriority">
          <option value="">Any</option>${selectOptions(["P0", "P1", "P2", "P3"], filters.priority)}</select></div>
        <div class="field"><label for="wAsset">Asset</label><select id="wAsset">
          <option value="">All assets</option>${assetPage.items.map((asset) => `<option value="${esc(asset.id)}" ${asset.id === filters.asset_id ? "selected" : ""}>${esc(asset.name)}</option>`).join("")}</select></div>
        <div class="row-end">
          <a class="button" href="/api/work-orders/export${exportLine(filters)}">Export JSON</a>
        </div>
      </div></div></div>
      <div class="banner" data-tone="warn" style="margin:0"><span>Human approval is mandatory: nothing in this list is
        authorised work until it shows <b>approved</b> or <b>in progress</b>. AI-generated orders start as drafts.</span></div>
      <div class="card card-flush"><div id="wTable">${UI.skeleton(5)}</div><div class="pagination" id="wPager"></div></div>
    </div>`;

  const map = { wSearch: "search", wStatus: "status", wPriority: "priority", wAsset: "asset" };
  Object.entries(map).forEach(([id, key]) => {
    const node = qs(`#${id}`, host);
    node.addEventListener("input", key === "search" ? debounce((event) => readQuery({ search: event.target.value.trim(), page: "" })) : () => {});
    node.addEventListener("change", () => readQuery({ [key]: node.value, page: "" }));
  });

  const table = qs("#wTable", host);
  if (!page.items.length) {
    table.innerHTML = UI.empty({ icon: "☰", title: "No work order matches", text: "Orders are drafted by completed runs, or raised by hand from an inspection." });
    return;
  }
  table.innerHTML = ordersTable(page.items);
  UI.pager(qs("#wPager", host), page, (next) => readQuery({ page: next }));
  qsa("tbody tr", table).forEach((row) => {
    const order = page.items.find((item) => item.id === row.dataset.id);
    row.addEventListener("click", (event) => { if (event.target.closest("button")) return; orderDrawer(order.id); });
    qsa("button", row).forEach((button) => button.addEventListener("click", (event) => {
      event.stopPropagation();
      orderDrawer(order.id);
    }));
  });
}

async function orderDrawer(orderId) {
  const order = await Api.get(`/api/work-orders/${encodeURIComponent(orderId)}`);
  const allowed = (Store.config.choices.work_order_transitions[order.status] || []);
  const findings = order.finding_ids.map((id) => `<li><span class="mono tiny">${esc(id)}</span></li>`).join("");
  const body = elFrom(`<div class="stack">
    <div class="row">${orderBadge(order.status)}<span class="badge badge-priority">${esc(order.priority)}</span>
      <span class="badge">${esc(order.source_mode)} mode</span>
      ${order.created_by ? `<span class="muted tiny">raised by ${esc(order.created_by)}</span>` : ""}</div>
    <p>${esc(order.description || "No description was recorded.")}</p>
    <dl class="kv">
      <dt>Asset</dt><dd><a href="#/assets/${esc(order.asset_id)}">${esc(order.asset_name || order.asset_id)}</a></dd>
      <dt>Inspection</dt><dd><a href="#/inspections/${esc(order.inspection_id)}">${esc(order.inspection_id)}</a></dd>
      <dt>Team / assignee</dt><dd>${esc(order.assigned_team || "—")} · ${esc(order.assignee || "unassigned")}</dd>
      <dt>Due</dt><dd>${esc(order.due_date || "not set")}</dd>
      <dt>Updated</dt><dd>${esc(Fmt.date(order.updated_at))}</dd>
    </dl>
    ${order.risk_rationale ? `<div class="interpreted"><h4>Risk rationale (model interpretation)</h4><p>${esc(order.risk_rationale)}</p></div>` : ""}
    ${(order.recommended_action || []).length ? `<div class="detail-block"><h4>Recommended action</h4><ul>${
      order.recommended_action.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
    ${(order.finding_ids || []).length ? `<div class="detail-block"><h4>Findings covered</h4><ul>${findings}</ul></div>` : ""}
    ${order.evidence && Object.keys(order.evidence).length ? `<div class="detail-block"><h4>Evidence payload</h4>
      <pre class="mono tiny" style="white-space:pre-wrap">${esc(JSON.stringify(order.evidence, null, 2))}</pre></div>` : ""}
    <div class="detail-block"><h4>Decision history</h4>
      ${(order.history || []).length ? `<div class="timeline">${order.history.map((entry) => `
        <div class="timeline-item"><span class="timeline-dot"></span><div class="t-body">
          <div><b>${esc(entry.action)}</b> ${esc(entry.from_status ? `${entry.from_status} → ${entry.to_status}` : entry.to_status)}</div>
          <div class="t-when">${esc(entry.actor)} · ${esc(Fmt.date(entry.created_at))}${entry.note ? ` · ${esc(entry.note)}` : ""}</div>
        </div></div>`).join("")}</div>` : `<p class="muted tiny">No decision recorded yet.</p>`}
    </div>
    <div class="detail-block stack">
      <h4>Human decision</h4>
      ${actorField("decisionActor", "Your name (written to the audit trail)")}
      <div class="field"><label for="decisionNote">Note (optional)</label>
        <textarea id="decisionNote" maxlength="1000" placeholder="Why you are approving or rejecting this."></textarea></div>
    </div>
  </div>`);

  const foot = [{ label: "Edit details", class: "button", onClick: async () => { orderForm(null, null, order); } }];
  if (["draft", "pending_approval"].includes(order.status)) {
    foot.push({ label: "Reject", class: "button-danger", onClick: async (close) => { await decide(order.id, "rejected", body); close(); } });
    foot.push({ label: "Approve", class: "button-success", onClick: async (close) => { await decide(order.id, "approved", body); close(); } });
  }
  allowed.filter((status) => !["approved", "rejected"].includes(status)).forEach((status) => {
    foot.push({
      label: `Move to ${status.replace("_", " ")}`, class: "button",
      onClick: async (close) => { await transition(order.id, status, body); close(); },
    });
  });

  UI.drawer({ title: order.title, subtitle: order.id, body, foot });
}

async function decide(orderId, toStatus, scope) {
  const host = scope || document;
  const nameNode = qs("#decisionActorId", host) || qs("#decisionActor input", host);
  const name = (nameNode && nameNode.value.trim()) || "";
  if (name.length < 2) {
    UI.toast("warn", "Approval needs a name", "Approvals must be attributable — enter who is deciding.");
    if (nameNode) nameNode.focus();
    return false;
  }
  actor.set(name);
  const note = (qs("#decisionNote", host) || {}).value || "";
  try {
    const order = await Api.post(`/api/work-orders/${encodeURIComponent(orderId)}/${toStatus === "approved" ? "approve" : "reject"}`, { actor: name, note: note.trim() });
    UI.toast(toStatus === "approved" ? "ok" : "warn",
      toStatus === "approved" ? "Work order approved" : "Work order rejected",
      toStatus === "approved" ? `${order.id} is now authorised maintenance; its findings are marked converted.` : `${order.id} sent back; findings stay open.`);
    Router.go();
    return true;
  } catch (error) {
    if (error.code === "invalid_transition" && error.detail && error.detail.allowed) {
      UI.toast("warn", "That transition is not allowed", `allowed next: ${error.detail.allowed.join(", ") || "none"}`);
      return false;
    }
    UI.error(error);
    return false;
  }
}

async function transition(orderId, toStatus, scope) {
  const nameNode = qs("#decisionActorId", scope || document);
  const name = (nameNode && nameNode.value.trim()) || actor.get();
  if (name.length < 2) { UI.toast("warn", "Name required", "State changes are attributed."); return false; }
  actor.set(name);
  try {
    await Api.post(`/api/work-orders/${encodeURIComponent(orderId)}/transition`, { to_status: toStatus, actor: name });
    UI.toast("ok", `Moved to ${toStatus.replace("_", " ")}`, orderId);
    Router.go();
    return true;
  } catch (error) { UI.error(error); return false; }
}

async function orderForm(inspection, done, existing) {
  if (existing) {
    const body = elFrom(`<form class="form-grid" novalidate>
      <div class="field full" data-field="title"><label for="oTitle">Title</label>
        <input id="oTitle" name="title" type="text" value="${esc(existing.title)}" maxlength="140"><span class="error-text" data-error></span></div>
      <div class="field full" data-field="description"><label for="oDesc">Description</label>
        <textarea id="oDesc" name="description">${esc(existing.description)}</textarea><span class="error-text" data-error></span></div>
      <div class="field" data-field="priority"><label for="oPriority">Priority</label>
        <select id="oPriority" name="priority">${selectOptions(["P0", "P1", "P2", "P3"], existing.priority)}</select></div>
      <div class="field" data-field="due_date"><label for="oDue">Due date</label>
        <input id="oDue" name="due_date" type="date" value="${esc(existing.due_date)}"><span class="error-text" data-error></span></div>
      <div class="field" data-field="assigned_team"><label for="oTeam">Team</label>
        <input id="oTeam" name="assigned_team" type="text" value="${esc(existing.assigned_team)}"></div>
      <div class="field" data-field="assignee"><label for="oAssignee">Assignee</label>
        <input id="oAssignee" name="assignee" type="text" value="${esc(existing.assignee)}"></div>
      <div class="field full" data-field="risk_rationale"><label for="oRisk">Risk rationale</label>
        <textarea id="oRisk" name="risk_rationale" maxlength="1000">${esc(existing.risk_rationale)}</textarea>
        <span class="hint">Human note. Model-generated rationale is kept separate.</span><span class="error-text" data-error></span></div>
      ${actorField("editor", "Your name")}
    </form>`);
    UI.modal({
      title: "Edit work order", subtitle: existing.id, wide: true, body,
      actions: [
        { label: "Cancel", class: "button-ghost" },
        { label: "Save", class: "button-primary", autofocus: true, onClick: async (close) => {
          const payload = readForm(body, ["title", "description", "priority", "due_date", "assigned_team", "assignee", "risk_rationale"]);
          const name = qs("#editorId", body).value.trim();
          if (name.length < 2) { UI.toast("warn", "Name required", "Edits are attributed."); return true; }
          actor.set(name);
          try {
            await Api.patch(`/api/work-orders/${existing.id}`, { ...payload, actor: name });
            UI.toast("ok", "Work order updated", existing.id);
            close(); Router.go();
          } catch (error) { UI.fieldErrors(body, error); UI.error(error); return true; }
          return false;
        } },
      ],
    });
    return;
  }

  const inspections = (await Api.get(Api.query("/api/inspections", { page_size: 40, status: "completed" }))).items;
  if (!inspections.length) { UI.toast("warn", "No completed inspection", "Finish a run before raising work."); return; }
  let findings = [];
  const body = elFrom(`<form class="stack" novalidate>
    <div class="field" data-field="inspection_id"><label for="wInspection">Inspection</label>
      <select id="wInspection">${inspections.map((row) => `<option value="${esc(row.id)}">${esc(row.title)} · ${esc(row.id)}</option>`).join("")}</select>
      <span class="error-text" data-error></span></div>
    <div class="field" data-field="finding_ids"><label>Findings to include</label>
      <div class="stack" id="findingPick" style="max-height:220px;overflow:auto"></div>
      <span class="hint">Only findings from the selected inspection can be attached.</span><span class="error-text" data-error></span></div>
    <div class="field" data-field="title"><label for="wTitle">Title (optional)</label>
      <input id="wTitle" type="text" maxlength="140" placeholder="generated from the concept if blank"></div>
    <div class="form-grid">
      <div class="field" data-field="assigned_team"><label for="wTeam">Team</label><input id="wTeam" type="text"></div>
      <div class="field" data-field="assignee"><label for="wAssignee">Assignee</label><input id="wAssignee" type="text"></div>
      <div class="field" data-field="due_date"><label for="wDue">Due date</label><input id="wDue" type="date"></div>
    </div>
    ${actorField("creator", "Your name *")}
    <p class="tiny muted">A hand-raised order is created as <b>pending approval</b> — it is not authorised until someone approves it.</p>
  </form>`);

  const pick = qs("#findingPick", body);
  async function loadFindings() {
    const id = qs("#wInspection", body).value;
    const rows = (await Api.get(Api.query("/api/findings", { inspection_id: id, page_size: 100 }))).items;
    findings = rows;
    pick.innerHTML = rows.length ? rows.map((row) => `
      <label class="switch"><input type="checkbox" value="${esc(row.id)}" ${row.needs_human_review ? "checked" : ""}>
        <span>${esc(row.concept)} · ${esc(row.severity)} · <span class="mono tiny">${esc(row.id)}</span></span></label>`).join("")
      : `<p class="muted tiny">This inspection has no findings.</p>`;
  }
  qs("#wInspection", body).addEventListener("change", loadFindings);
  loadFindings();

  UI.modal({
    title: "Raise a work order", wide: true, body,
    actions: [
      { label: "Cancel", class: "button-ghost" },
      { label: "Create draft", class: "button-primary", onClick: async (close) => {
        const chosen = qsa("input:checked", pick).map((node) => node.value);
        if (!chosen.length) { UI.toast("warn", "Pick a finding", "A work order must cover at least one detection."); return true; }
        const name = qs("#creatorId", body).value.trim();
        if (name.length < 2) { UI.toast("warn", "Name required", "Drafts are attributed."); return true; }
        actor.set(name);
        const payload = {
          inspection_id: qs("#wInspection", body).value,
          finding_ids: chosen,
          title: qs("#wTitle", body).value.trim(),
          assigned_team: qs("#wTeam", body).value.trim(),
          assignee: qs("#wAssignee", body).value.trim(),
          due_date: qs("#wDue", body).value,
          actor: name,
        };
        try {
          const order = await Api.post("/api/work-orders", payload);
          UI.toast("ok", "Draft work order created", `${order.id} — awaiting approval`);
          close();
          if (done) done(); else orderDrawer(order.id);
        } catch (error) { UI.fieldErrors(body, error); UI.error(error); return true; }
        return false;
      } },
    ],
  });
}

/* ----------------------------------------------------------------- reports */

async function viewReports(host, params, query) {
  const inspections = await Api.get(Api.query("/api/inspections", { page_size: 50 }));
  const tab = query.tab || "audit";
  host.innerHTML = `
    <div class="stack">
      <div class="tabs" id="reportTabs">
        <button type="button" data-tab="audit"${tab === "audit" ? ' class="is-active"' : ""}>Audit trail</button>
        <button type="button" data-tab="artifacts"${tab === "artifacts" ? ' class="is-active"' : ""}>Run artifacts</button>
      </div>
      <div id="reportPane">${UI.skeleton(6)}</div>
    </div>`;

  qsa("button", qs("#reportTabs", host)).forEach((button) => button.addEventListener("click", () => {
    qsa("button", qs("#reportTabs", host)).forEach((node) => node.classList.toggle("is-active", node === button));
    render(button.dataset.tab, {});
  }));

  async function render(which, filters) {
    const pane = qs("#reportPane", host);
    pane.innerHTML = UI.skeleton(6);
    if (which === "artifacts") {
      pane.innerHTML = `<div class="card card-flush"><div class="card-head"><h2>Artifacts by inspection</h2>
        <a class="button button-sm row-end" href="/api/work-orders/export">Export work orders</a></div>
        <div class="table-wrap">${
          inspections.items.length ? `<table class="data"><thead><tr><th>Inspection</th><th>Asset</th><th>Status</th>
            <th class="num">Artifacts</th><th>Created</th><th></th></tr></thead><tbody>${inspections.items.map((row) => `
            <tr><td class="cell-title">${esc(row.title)}<div class="cell-sub mono">${esc(row.id)}</div></td>
            <td>${esc(row.asset_name)}</td><td>${UI.statusTag(row.status)}</td>
            <td class="num">${(row.artifacts && row.artifacts.length) || "—"}</td>
            <td class="muted tiny">${esc(Fmt.date(row.created_at))}</td>
            <td><a class="button button-sm" href="/api/inspections/${esc(row.id)}/report.pdf" download>PDF</a>
              <a class="button button-sm" href="#/inspections/${esc(row.id)}">Open</a></td></tr>`).join("")}</tbody></table>`
          : UI.empty({ icon: "⎙", title: "No runs yet", text: "Artifacts are written by the reporting stage." })
        }</div></div>`;
      return;
    }
    const page = await Api.get(Api.query("/api/audit", { action: filters.action || "", inspection_id: filters.inspection_id || "", page: filters.page || 1, page_size: 100 }));
    pane.innerHTML = `
      <div class="card"><div class="card-body"><div class="filters">
        <div class="field"><label for="aAction">Action contains</label><input id="aAction" type="search" value="${esc(filters.action || "")}" placeholder="job.start, sam.segment…"></div>
        <div class="field"><label for="aInspection">Inspection id</label><input id="aInspection" type="search" value="${esc(filters.inspection_id || "")}" placeholder="I-…"></div>
        <div class="row-end"><span class="badge">${page.total} event(s)</span></div></div>
        <p class="tiny muted" style="margin-top:10px">${esc(page.note)}</p>
      </div></div>
      <div class="card card-flush"><div class="table-wrap">${
        page.items.length ? `<table class="data"><thead><tr><th>When</th><th>Action</th><th>Agent</th><th>Phase</th>
          <th>Inspection</th><th class="wrap-cell">Detail</th></tr></thead><tbody>${page.items.map((row) => `
          <tr><td class="tiny">${esc(Fmt.date(row.created_at))}</td><td class="mono tiny">${esc(row.action)}</td>
          <td class="tiny">${esc(row.agent || "—")}</td><td class="tiny">${esc(row.phase || "—")}</td>
          <td class="tiny">${row.inspection_id ? `<a href="#/inspections/${esc(row.inspection_id)}">${esc(row.inspection_id)}</a>` : "—"}</td>
          <td class="tiny mono wrap-cell">${esc(shortJson(row.detail))}</td></tr>`).join("")}</tbody></table>`
        : UI.empty({ icon: "⎙", title: "No events match", text: "The audit log records backend events as they happen." })
      }</div><div class="pagination" id="aPager"></div></div>`;
    UI.pager(qs("#aPager", pane), page, (next) => render("audit", { ...filters, page: next }));
    qs("#aAction", pane).addEventListener("input", debounce((event) => render("audit", { ...filters, action: event.target.value.trim(), page: 1 }), 400));
    qs("#aInspection", pane).addEventListener("input", debounce((event) => render("audit", { ...filters, inspection_id: event.target.value.trim(), page: 1 }), 400));
  }

  render(tab, {});
}

function shortJson(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 180 ? `${text.slice(0, 180)}…` : text;
}

/* --------------------------------------------------------------- settings */

async function viewSettings(host) {
  const [config, providers] = await Promise.all([Api.get("/api/config"), Api.get("/api/providers")]);
  host.innerHTML = `
    <div class="split">
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Meta Model API</h2>
          <span class="row-end">${providers.configured ? '<span class="badge badge-ok">live</span>' : '<span class="badge badge-mock">simulation</span>'}</span></div>
          <div class="card-body stack">
            <dl class="kv">
              <dt>Base URL</dt><dd class="mono">${esc(providers.base_url)}</dd>
              <dt>Segmentation model</dt><dd class="mono">${esc(providers.models.segmentation)}</dd>
              <dt>Reasoning model</dt><dd class="mono">${esc(providers.models.reasoning)}</dd>
              <dt>Key present</dt><dd>${providers.configured ? "yes — server side" : "no — set MODEL_API_KEY in .env"}</dd>
            </dl>
            <p class="tiny muted">${esc(providers.secret_note)}</p>
            <div class="row">
              <button class="button button-primary" type="button" id="checkBtn">Run connectivity check</button>
              <span class="muted tiny">sends one <code>GET /models</code> request and stores the result</span>
            </div>
            <div id="checkResult"></div>
          </div></div>

        <div class="card card-flush"><div class="card-head"><h2>Last verified per provider</h2></div>
          <div class="table-wrap" id="checksWrap">${checksTable(providers.checks)}</div></div>

        <div class="card"><div class="card-head"><h2>Limits enforced by the server</h2></div>
          <div class="card-body"><dl class="kv">
            <dt>Max upload</dt><dd>${config.limits.max_upload_mb} MB</dd>
            <dt>Max frames per run</dt><dd>${config.limits.max_frames}</dd>
            <dt>Max prompts</dt><dd>${config.limits.max_prompts}</dd>
            <dt>Request timeout</dt><dd>${config.limits.request_timeout}s</dd>
            <dt>Media retention</dt><dd>${config.limits.retention_days} day(s)</dd>
            <dt>Accepted media</dt><dd class="mono">${esc(config.choices.accepted_suffixes.join(" "))}</dd>
          </dl></div></div>
      </div>

      <div class="stack">
        <div class="card"><div class="card-head"><h2>Honesty rules in this build</h2></div>
          <div class="card-body stack tiny">
            <p>· Simulation output is labelled <code>simulated sam-3.1</code> / <code>simulated rule engine</code> on every finding.</p>
            <p>· Overlays are drawn only from masks or boxes the detector actually returned.</p>
            <p>· A failed reasoning run keeps its detections, marks every finding for review and raises <b>no</b> work orders.</p>
            <p>· No CMMS, email, SMS or robotic-arm integration is wired up; the work order export is an import payload.</p>
            <p>· Nothing dispatches without a named human approval.</p>
          </div></div>
        <div class="card"><div class="card-head"><h2>Configure the real provider</h2></div>
          <div class="card-body stack tiny">
            <p>Copy <code>.env.example</code> to <code>.env</code> in <code>infrascan-3d/</code> and set
              <code>MODEL_API_KEY</code>, then restart the server. The key is read by the server process only.</p>
            <pre class="mono" style="white-space:pre-wrap;background:var(--surface-alt);padding:10px;border-radius:8px">MODEL_API_KEY=your-key
INFRASCAN_MODE=live</pre>
            <p class="muted">Until then the app stays in simulation mode and says so everywhere.</p>
          </div></div>
        <div class="card"><div class="card-head"><h2>Build</h2></div>
          <div class="card-body"><dl class="kv">
            <dt>Version</dt><dd class="mono">${esc(config.version)}</dd>
            <dt>Mode</dt><dd class="mono">${esc(config.mode)}</dd>
            <dt>Engine labels</dt><dd class="tiny">${esc(config.engine_labels.segmentation)} · ${esc(config.engine_labels.reasoning)}</dd>
          </dl></div></div>
      </div>
    </div>`;

  qs("#checkBtn", host).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    qs("#checkResult", host).innerHTML = UI.skeleton(2);
    try {
      const result = await Api.post("/api/providers/check", {});
      const fresh = await Api.get("/api/providers");
      qs("#checksWrap", host).innerHTML = checksTable(fresh.checks);
      qs("#checkResult", host).innerHTML = `
        <div class="risk-flag" style="background:${result.ok ? "var(--success-soft)" : "var(--warning-soft)"}">
          <span>${result.ok ? "✓" : "⚠"}</span><span>
          <b>${result.ok ? "Connectivity verified" : "Connectivity not verified"}</b> —
          ${esc(result.message || result.kind || "no message")}
          ${result.status_code ? ` · HTTP ${result.status_code}` : ""}${result.latency_ms ? ` · ${result.latency_ms} ms` : ""}<br>
          ${result.available_models && result.available_models.length
            ? `<span class="mono tiny">models: ${esc(result.available_models.join(", "))}</span>`
            : `<span class="tiny muted">No model list was returned, so nothing is claimed about availability.</span>`}
        </span></div>`;
      if (!providers.configured) {
        banner("warn", "The check ran without a server-side key. Set MODEL_API_KEY and re-run before claiming live Meta connectivity.");
      }
    } catch (error) {
      qs("#checkResult", host).innerHTML = `<div class="risk-flag"><span>✕</span><span>${esc(error.code)}: ${esc(error.message)}</span></div>`;
    } finally { button.disabled = false; }
  });
}

function checksTable(checks) {
  if (!checks.length) return UI.empty({ icon: "⚙", title: "No connectivity check recorded", text: "Run one to capture a real HTTP result — until then no claim about the provider is made." });
  return `<table class="data"><thead><tr><th>Provider</th><th>Models</th><th>Configured</th><th>Verified</th>
      <th class="num">Status</th><th class="num">Latency</th><th>When</th><th>Message</th></tr></thead>
    <tbody>${checks.map((row) => `<tr>
      <td class="mono tiny">${esc(row.provider)}</td><td class="mono tiny">${esc(row.model)}</td>
      <td>${row.configured ? '<span class="badge badge-ok">yes</span>' : '<span class="badge badge-mock">no</span>'}</td>
      <td>${row.verified_ok ? '<span class="badge badge-ok">passed</span>' : '<span class="badge badge-danger">failed</span>'}</td>
      <td class="num">${row.status_code || "—"}</td><td class="num">${row.latency_ms || 0} ms</td>
      <td class="tiny">${esc(Fmt.rel(row.created_at))}</td>
      <td class="tiny mono wrap-cell">${esc(row.message || "—")}</td></tr>`).join("")}</tbody></table>`;
}

/* ------------------------------------------------------------------- seed */

register("dashboard", viewDashboard);
register("assets", viewAssets);
register("asset", viewAsset);
register("inspections", viewInspections);
register("new", viewNewInspection);
register("inspection", viewInspection);
register("defects", viewDefects);
register("orders", viewOrders);
register("reports", viewReports);
register("settings", viewSettings);

})();
