/* InfraScan 3D — application core: API client, router, UI primitives.
   Talks only to the v2 JSON API; the provider secret is never requested or held here. */
"use strict";

/* ------------------------------------------------------------------ storage */

const Store = {
  config: null,
  assets: [],
  route: null,
};

/* --------------------------------------------------------------------- util */

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
));

const qs = (sel, root = document) => root.querySelector(sel);
const qsa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function elFrom(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

function debounce(fn, ms = 260) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

const Fmt = {
  date(value) {
    if (!value) return "—";
    const date = new Date(value * 1000);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  },
  day(value) {
    if (!value) return "—";
    return new Date(value * 1000).toLocaleDateString(undefined, { month: "short", day: "2-digit" });
  },
  rel(value) {
    if (!value) return "—";
    const diff = Date.now() / 1000 - value;
    if (diff < 45) return "just now";
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
  },
  bytes(value) {
    if (!value) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let n = value, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
  },
  pct(value) {
    if (value === null || value === undefined) return "—";
    return `${Math.round(value * 100)}%`;
  },
  num(value, digits = 2) {
    if (value === null || value === undefined) return "—";
    return Number(value).toFixed(digits);
  },
  clock(ms) {
    const total = Math.round((ms || 0) / 1000 * 10) / 10;
    return total >= 60 ? `${Math.floor(total / 60)}m ${(total % 60).toFixed(0)}s` : `${total}s`;
  },
};

/* ---------------------------------------------------------------------- api */

class ApiError extends Error {
  constructor(payload, status) {
    const err = payload && payload.error ? payload.error : {};
    super(err.message || `request failed (${status})`);
    this.code = err.code || "request_failed";
    this.status = status;
    this.detail = err.detail || null;
  }
}

async function request(path, options = {}) {
  const init = { headers: {}, ...options };
  if (init.body && !(init.body instanceof FormData)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); }
    catch { payload = { error: { code: "unexpected_response", message: text.slice(0, 200) } }; }
  }
  if (!response.ok) throw new ApiError(payload, response.status);
  return payload;
}

const Api = {
  get: (path) => request(path),
  post: (path, body) => request(path, { method: "POST", body }),
  patch: (path, body) => request(path, { method: "PATCH", body }),
  del: (path) => request(path, { method: "DELETE" }),
  mediaUrl: (id, frame, variant = "prepared") => {
    const parts = [`variant=${variant}`];
    if (frame !== undefined && frame !== null) parts.push(`frame=${frame}`);
    return `/api/inspections/${encodeURIComponent(id)}/media?${parts.join("&")}`;
  },
  query: (path, params = {}) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== "" && value !== null && value !== undefined) search.set(key, value);
    });
    const line = search.toString();
    return line ? `${path}?${line}` : path;
  },
};

/* Multipart upload with real byte progress - fetch cannot report it. */
function uploadInspection({ payload, sample = "", files = [], frameTimes = [] }, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("payload", JSON.stringify(payload));
    if (sample) form.append("sample", sample);
    if (files.length > 1) {
      files.forEach((file) => form.append("frames", file, file.name));
      form.append("frame_times", JSON.stringify(frameTimes));
    } else if (files.length === 1) {
      form.append("file", files[0], files[0].name);
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/inspections");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payloadOut = null;
      try { payloadOut = JSON.parse(xhr.responseText || "null"); } catch { payloadOut = null; }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(payloadOut); return; }
      reject(new ApiError(payloadOut, xhr.status));
    });
    xhr.addEventListener("error", () => reject(new ApiError({ error: { code: "network", message: "the request never reached the server" } }, 0)));
    xhr.addEventListener("abort", () => reject(new ApiError({ error: { code: "aborted", message: "upload cancelled" } }, 0)));
    xhr.send(form);
  });
}

/* ----------------------------------------------------------------------- ui */
const overlayClosers = [];

function trackOverlay(close) {
  let wrapper;
  wrapper = () => {
    const index = overlayClosers.indexOf(wrapper);
    if (index >= 0) overlayClosers.splice(index, 1);
    close();
  };
  overlayClosers.push(wrapper);
  return wrapper;
}

/* eslint-disable-next-line max-lines-per-function */
const UI = {
  closeOverlays() {
    overlayClosers.slice().forEach((close) => close());
  },

  toast(tone, title, body) {
    const node = elFrom(`
      <div class="toast ${tone}" role="${tone === "error" ? "alert" : "status"}">
        <div>
          <div class="toast-title">${esc(title)}</div>
          ${body ? `<div class="toast-body">${esc(body)}</div>` : ""}
        </div>
        <button class="icon-button" type="button" aria-label="Dismiss">✕</button>
      </div>`);
    const kill = () => {
      node.style.opacity = "0";
      setTimeout(() => node.remove(), 160);
    };
    qs("button", node).addEventListener("click", kill);
    qs("#toastStack").appendChild(node);
    setTimeout(kill, tone === "error" ? 9000 : 4800);
    return node;
  },

  error(error, fallback = "Request failed") {
    const tone = error instanceof ApiError ? (error.status === 409 ? "warn" : "error") : "error";
    let body = error.message || fallback;
    if (error.code === "invalid_input" && error.detail && error.detail.fields) {
      body = error.detail.fields.map((field) => `${field.field}: ${field.message}`).join(" · ");
    }
    UI.toast(tone, `${error.code || "error"}`, body);
  },

  modal({ title, subtitle, body, actions, wide = false, onMount }) {
    const root = qs("#modalRoot");
    const node = elFrom(`
      <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">
          <div>
            <h3>${esc(title)}</h3>
            ${subtitle ? `<p class="muted">${esc(subtitle)}</p>` : ""}
          </div>
          <button class="icon-button modal-x" type="button" aria-label="Close dialog" style="margin-left:auto">✕</button>
        </div>
        <div class="modal-body"></div>
        <div class="modal-foot"></div>
      </div>`);
    const backdrop = elFrom(`<div class="modal-backdrop"></div>`);
    root.innerHTML = "";
    root.hidden = false;
    root.append(backdrop, node);

    const close = trackOverlay(() => { root.hidden = true; root.innerHTML = ""; document.removeEventListener("keydown", onKey); });
    const onKey = (event) => { if (event.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", close);
    qs(".modal-x", node).addEventListener("click", close);

    const bodyHost = qs(".modal-body", node);
    if (typeof body === "string") bodyHost.innerHTML = body;
    else if (body) bodyHost.appendChild(body);

    const foot = qs(".modal-foot", node);
    (actions || []).forEach((action) => {
      const button = elFrom(`<button class="button ${action.class || ""}" type="button">${esc(action.label)}</button>`);
      button.addEventListener("click", async () => {
        button.disabled = true;
        let keep = false;
        try { if (action.onClick) keep = (await action.onClick(close)) === true; }
        finally { button.disabled = false; }
        if (!keep) close();
      });
      if (action.autofocus) button.dataset.autofocus = "1";
      foot.appendChild(button);
    });

    const focusTarget = qs("[data-autofocus]", node) || qs("input, select, textarea, button", bodyHost) || qs(".modal-x", node);
    if (focusTarget) focusTarget.focus();
    if (onMount) onMount(node, close);
    return { node, close };
  },

  confirm({ title, message, label = "Confirm", tone = "danger", requireText = "", details = "" }) {
    return new Promise((resolve) => {
      const body = elFrom(`
        <div class="stack">
          <p>${esc(message)}</p>
          ${details ? `<p class="tiny muted">${esc(details)}</p>` : ""}
          ${requireText ? `
            <div class="field">
              <label for="confirmInput">Type <code class="mono">${esc(requireText)}</code> to allow this</label>
              <input id="confirmInput" type="text" autocomplete="off" spellcheck="false">
            </div>` : ""}
        </div>`);
      const { close } = UI.modal({
        title,
        body,
        actions: [
          { label: "Cancel", class: "button-ghost", onClick: () => { resolve(false); } },
          {
            label,
            class: tone === "danger" ? "button-danger" : "button-primary",
            autofocus: true,
            onClick: () => { resolve(true); },
          },
        ],
        onMount: (node) => {
          const approve = qs(".modal-foot .button:last-child", node);
          if (requireText) {
            approve.disabled = true;
            const input = qs("#confirmInput", node);
            input.addEventListener("input", () => { approve.disabled = input.value.trim() !== requireText; });
            input.focus();
          }
        },
      });
      void close;
    });
  },

  drawer({ title, subtitle, body, foot }) {
    const scrim = elFrom(`<div class="scrim-drawer"></div>`);
    const node = elFrom(`
      <aside class="drawer" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="drawer-head">
          <div><h3>${esc(title)}</h3>${subtitle ? `<p class="muted tiny">${esc(subtitle)}</p>` : ""}</div>
          <button class="icon-button drawer-x" type="button" aria-label="Close panel" style="margin-left:auto">✕</button>
        </div>
        <div class="drawer-body"></div>
        ${foot ? `<div class="drawer-foot"></div>` : ""}
      </aside>`);
    document.body.append(scrim, node);
    const close = trackOverlay(() => { scrim.remove(); node.remove(); document.removeEventListener("keydown", onKey); });
    const onKey = (event) => { if (event.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    scrim.addEventListener("click", close);
    qs(".drawer-x", node).addEventListener("click", close);
    qs(".drawer-body", node).appendChild(typeof body === "string" ? elFrom(`<div>${body}</div>`) : body);
    if (foot) {
      const host = qs(".drawer-foot", node);
      foot.forEach((action) => {
        const button = elFrom(`<button class="button ${action.class || ""}" type="button">${esc(action.label)}</button>`);
        button.addEventListener("click", async () => {
          button.disabled = true;
          try { await action.onClick(close); } finally { button.disabled = false; }
        });
        host.appendChild(button);
      });
    }
    qs(".drawer-x", node).focus();
    return { node, close };
  },

  skeleton(rows = 4) {
    return `<div class="table-loading">${
      Array.from({ length: rows }, () => `<div class="skeleton skeleton-line" style="width:${72 + Math.round(Math.random() * 28)}%"></div>`).join("")
    }</div>`;
  },

  empty({ icon = "◍", title, text, actionHref, actionLabel }) {
    return `<div class="empty">
      <div class="empty-ico">${esc(icon)}</div>
      <h3>${esc(title)}</h3>
      <p>${esc(text)}</p>
      ${actionHref ? `<a class="button button-primary" href="${esc(actionHref)}">${esc(actionLabel || "Continue")}</a>` : ""}
    </div>`;
  },

  statusTag(status) {
    return `<span class="status-tag" data-status="${esc(status)}"><span class="dot"></span>${esc(status)}</span>`;
  },

  severity(value) {
    const level = ["Low", "Medium", "High", "Critical", "Unknown"].includes(value) ? value : "Unknown";
    return `<span class="badge badge-sev ${level}">${esc(level)}</span>`;
  },

  pager(host, meta, onChange) {
    host.innerHTML = "";
    if (!meta || meta.pages <= 1) {
      if (meta && meta.total) host.innerHTML = `<span>${esc(meta.total)} record(s)</span>`;
      return;
    }
    host.innerHTML = `
      <span>Page <b>${meta.page}</b> of ${meta.pages} · ${meta.total} record(s)</span>
      <div class="pager">
        <button class="button button-sm" type="button" data-step="-1" ${meta.page <= 1 ? "disabled" : ""}>‹ Prev</button>
        <button class="button button-sm" type="button" data-step="1" ${meta.page >= meta.pages ? "disabled" : ""}>Next ›</button>
      </div>`;
    qsa("button[data-step]", host).forEach((button) => {
      button.addEventListener("click", () => onChange(meta.page + Number(button.dataset.step)));
    });
  },

  fieldErrors(form, error) {
    qsa("[data-field]", form).forEach((node) => {
      const label = qs("[data-error]", node);
      if (label) label.textContent = "";
      const input = qs("input, select, textarea", node);
      if (input) input.removeAttribute("aria-invalid");
    });
    if (!(error && error.code === "invalid_input" && error.detail)) return;
    (error.detail.fields || []).forEach(({ field, message }) => {
      const node = qs(`[data-field="${CSS.escape(field)}"]`, form);
      if (!node) return;
      const label = qs("[data-error]", node);
      if (label) label.textContent = message;
      const input = qs("input, select, textarea", node);
      if (input) input.setAttribute("aria-invalid", "true");
    });
  },
};

/* ------------------------------------------------------------------- router */

const ROUTES = [
  { id: "dashboard", path: "#/dashboard", label: "Dashboard", icon: "▦", title: "Dashboard", subtitle: "Inspection status, defect load and approval backlog" },
  { id: "assets", path: "#/assets", label: "Assets", icon: "▤", title: "Assets", subtitle: "Every inspected asset, its history and open defects" },
  { id: "inspections", path: "#/inspections", label: "Inspections", icon: "◉", title: "Inspections", subtitle: "Processing queue and completed runs" },
  { id: "new", path: "#/inspections/new", label: "New inspection", icon: "＋", title: "New inspection", subtitle: "Attach media, choose prompts and start a job" },
  { id: "defects", path: "#/defects", label: "Defects", icon: "⚠", title: "Defects", subtitle: "Model detections with visual evidence and risk context" },
  { id: "orders", path: "#/work-orders", label: "Work orders", icon: "☰", title: "Work orders", subtitle: "Human approval before any repair is authorised" },
  { id: "reports", path: "#/reports", label: "Reports & audit", icon: "⎙", title: "Reports & audit trail", subtitle: "Generated artifacts and the append-only event log" },
  { id: "settings", path: "#/settings", label: "Providers & config", icon: "⚙", title: "Providers & configuration", subtitle: "What is connected, what is verified, what is simulated" },
];

const NAV_GROUPS = [
  { section: "Operate", ids: ["dashboard", "assets", "inspections", "new"] },
  { section: "Analyse", ids: ["defects", "orders"] },
  { section: "Prove", ids: ["reports", "settings"] },
];

const Router = {
  routes: [],
  register(id, render) { this.byId = this.byId || {}; this.byId[id] = render; },

  parse() {
    const hash = location.hash || "#/dashboard";
    const raw = hash.replace(/^#\//, "");
    const [pathPart, queryPart] = raw.split("?");
    const segments = pathPart.split("/").filter(Boolean);
    const params = {};
    new URLSearchParams(queryPart || "").forEach((value, key) => { params[key] = value; });
    return { segments, params, hash };
  },

  match({ segments }) {
    const head = segments[0] || "dashboard";
    if (head === "inspections" && segments[1] === "new") return { id: "new", params: {}, segments };
    if (head === "inspections" && segments[1]) return { id: "inspection", params: { id: segments[1] }, segments };
    if (head === "assets" && segments[1]) return { id: "asset", params: { id: segments[1] }, segments };
    const known = ROUTES.find((route) => route.id === head || route.path.slice(2) === head);
    return { id: known ? known.id : "dashboard", params: {}, segments };
  },

  async go() {
    const parsed = this.parse();
    UI.closeOverlays();
    const route = this.match(parsed);
    Store.route = route;
    const DYNAMIC = {
      asset: ["Asset", "Context, inspection history and open defects"],
      inspection: ["Inspection workspace", "Stages, evidence and artifacts for this run"],
    };
    const meta = ROUTES.find((entry) => entry.id === route.id)
      || (DYNAMIC[route.id] ? { title: DYNAMIC[route.id][0], subtitle: DYNAMIC[route.id][1] } : ROUTES[0]);
    qs("#pageTitle").textContent = meta.title;
    qs("#pageSubtitle").textContent = meta.subtitle;
    document.title = `${meta.title} · InfraScan 3D`;
    const navKey = { asset: "assets", inspection: "inspections" }[route.id] || route.id;
    qsa(".nav-item").forEach((node) => {
      const active = node.dataset.route === navKey;
      node.classList.toggle("is-active", active);
      if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
    });
    qs("#globalSearchField").classList.toggle("hide", route.id === "new");
    closeSidebar();

    const view = qs("#view");
    view.innerHTML = UI.skeleton(5);
    const render = this.byId[route.id];
    if (!render) { view.innerHTML = UI.empty({ title: "Not wired up", text: `No view is registered for ${route.id}` }); return; }
    try { await render(view, route.params, parsed.params); }
    catch (error) {
      if (error instanceof ApiError) {
        view.innerHTML = `<div class="card"><div class="card-body">${
          UI.empty({ icon: "⚠", title: `Could not load this view (${error.code})`, text: error.message })
        }</div></div>`;
      } else {
        view.innerHTML = `<div class="card"><div class="card-body stack">
          <h2>Unexpected client error</h2><pre class="mono">${esc(String(error && error.stack || error))}</pre></div></div>`;
      }
      UI.error(error, "Could not load this view");
    }
    view.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  },
};

/* ------------------------------------------------------------------ sidebar */

function buildNav() {
  const host = qs("#nav");
  host.innerHTML = "";
  NAV_GROUPS.forEach((group) => {
    host.appendChild(elFrom(`<div class="nav-section">${esc(group.section)}</div>`));
    group.ids.forEach((id) => {
      const meta = ROUTES.find((route) => route.id === id);
      const node = elFrom(`<a class="nav-item" href="${meta.path}" data-route="${meta.id}">
        <span class="nav-ico" aria-hidden="true">${meta.icon}</span><span>${esc(meta.label)}</span>
        <span class="nav-count" data-count="${meta.id}" hidden></span></a>`);
      host.appendChild(node);
    });
  });
}

function navCount(id, value) {
  const node = qs(`[data-count="${id}"]`);
  if (!node) return;
  if (value === null || value === undefined) { node.hidden = true; return; }
  node.hidden = false;
  node.textContent = value;
}

function openSidebar() {
  qs("#sidebar").classList.add("is-open");
  qs("#scrim").hidden = false;
  qs("#menuButton").setAttribute("aria-expanded", "true");
}
function closeSidebar() {
  qs("#sidebar").classList.remove("is-open");
  qs("#scrim").hidden = true;
  qs("#menuButton").setAttribute("aria-expanded", "false");
}

function banner(tone, text) {
  const node = qs("#banner");
  if (!text) { node.hidden = true; node.innerHTML = ""; return; }
  node.hidden = false;
  node.dataset.tone = tone;
  node.innerHTML = `<span>${esc(text)}</span><button class="icon-button banner-close" type="button" aria-label="Dismiss">✕</button>`;
  qs(".banner-close", node).addEventListener("click", () => banner(null, ""));
}

async function loadConfig() {
  Store.config = await Api.get("/api/config");
  const config = Store.config;
  const pill = qs("#modePill");
  const state = config.live ? "live" : (config.provider_configured ? "mock" : "off");
  pill.dataset.state = state;
  qs("#modeLabel").textContent = config.live
    ? `Live · ${config.sam_model}`
    : (config.provider_configured ? `Simulated (${config.mode})` : `Simulated · no key`);
  qs("#sidebarNote").textContent = config.live
    ? "Requests go to the Meta Model API. Segments are real model output."
    : "No provider key on the server: detections are locally simulated and always labelled as such.";
  if (!config.live) {
    banner("warn", "Simulation mode: SAM segmentation and reasoning are generated locally, not by Meta. "
      + "Every finding below is labelled with the engine that produced it.");
  }
  return config;
}

/* ---------------------------------------------------------------- server-sent */

function followJob(inspectionId, handlers) {
  const source = new EventSource(`/api/inspections/${encodeURIComponent(inspectionId)}/events?after_seq=0`);
  let lastSeq = 0;
  source.addEventListener("message", (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    if (payload.seq && payload.seq <= lastSeq) return;
    lastSeq = payload.seq || lastSeq;
    if (handlers.each) handlers.each(payload);
    if (payload.type === "stage" && handlers.stage) handlers.stage(payload);
    if (payload.type === "frame" && handlers.frame) handlers.frame(payload);
    if (payload.type === "log" && handlers.log) handlers.log(payload);
    if (payload.type === "findings" && handlers.findings) handlers.findings(payload);
    if (payload.type === "done") {
      source.close();
      if (handlers.done) handlers.done(payload);
    }
  });
  source.onerror = () => { source.close(); if (handlers.error) handlers.error(); };
  if (handlers.opened) source.onopen = handlers.opened;
  return () => source.close();
}

/* ------------------------------------------------------------- evidence view */

/* Draws only what the detector returned: a box when a box exists, a mask path
   when a mask path exists. Nothing is invented client-side. */
function overlaySvg(findings, selectedId, sourceSize) {
  const [width, height] = sourceSize && sourceSize[0] ? sourceSize : [1, 1];
  const shapes = findings.map((finding) => {
    const [left, top, right, bottom] = finding.box || [0, 0, 0, 0];
    const selected = finding.id === selectedId;
    const mask = finding.mask || {};
    const origin = mask.origin || [left, top];
    const path = mask.svg ? `<path class="mask-shape ${selected ? "is-selected" : ""}" transform="translate(${origin[0]} ${origin[1]})" d="${esc(mask.svg)}"/>` : "";
    const label = `${finding.concept} · ${Fmt.pct(finding.confidence)}`;
    return `<g>
      ${path}
      <rect class="box-shape ${selected ? "is-selected" : ""}" x="${left}" y="${top}" width="${Math.max(0, right - left)}" height="${Math.max(0, bottom - top)}" rx="2"/>
      <g transform="translate(${left} ${Math.max(0, top - 17)})">
        <rect class="tag-bg" x="0" y="0" width="${Math.min(width, 7 + label.length * 6.1)}" height="16" rx="3"/>
        <text class="tag" x="5" y="11.5">${esc(label)}</text>
      </g>
    </g>`;
  }).join("");
  return `<svg class="overlay" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">${shapes}</svg>`;
}

/* --------------------------------------------------------------- boot */

async function boot() {
  buildNav();
  qs("#menuButton").addEventListener("click", openSidebar);
  qs("#sidebarClose").addEventListener("click", closeSidebar);
  qs("#scrim").addEventListener("click", closeSidebar);
  window.addEventListener("hashchange", () => Router.go());

  try {
    await loadConfig();
  } catch (error) {
    banner("error", `Server configuration could not be read: ${error.message}`);
  }

  qs("#globalSearch").addEventListener("input", debounce((event) => {
    const term = event.target.value.trim();
    if (!term) return;
    const route = Router.match(Router.parse());
    const target = { assets: "#/assets", new: "#/defects", inspection: "#/defects", asset: "#/defects" };
    location.hash = `${target[route.id] || "#/defects"}?search=${encodeURIComponent(term)}`;
  }, 420));

  await Router.go();
  setInterval(() => { if (!document.hidden) loadConfig().catch(() => {}); }, 60000);
}

document.addEventListener("DOMContentLoaded", () => {
  if (!location.hash) location.hash = "#/dashboard";
  boot();
});

window.Infrascan = { Api, UI, Router, Store, Fmt, esc, elFrom, qs, qsa, followJob, overlaySvg, banner, navCount, uploadInspection, ApiError, debounce };
