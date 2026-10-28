// GridPilot front page. Plain JS, no dependencies, no third-party requests.
"use strict";

const TZ = "Europe/Berlin";
const fmtDayTime = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short", hour: "2-digit", minute: "2-digit" });
const fmtTime = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const fmtHour = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", hourCycle: "h23" });
const fmtDay = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short" });
const fmtDate = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

const $ = (sel) => document.querySelector(sel);
const state = { overview: null, preview: null, options: { duration_hours: 2, within_hours: 24, objective: "carbon" }, hover: null, intro: true };
const RIBBON = { priceTop: 34, priceH: 92, gap: 10, cellH: 64 };

// ---------- colour ramp: wind teal (clean) -> straw -> lignite brown (dirty) ----------
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function hexToRgb(hex) { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
function mix(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }
// Colours are relative to the hours on screen: the cleanest hour is teal, the dirtiest brown.
// On a windless week every hour is dirty, and a fixed scale would paint them all the same brown.
// A minimum span keeps a flat day from exaggerating tiny differences.
const MIN_SPAN = 60; // g CO2/kWh
function carbonScale(pts) {
  const vals = pts.map((p) => p.g_per_kwh).filter((v) => v != null);
  if (!vals.length) return { lo: 0, span: MIN_SPAN };
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(hi - lo, MIN_SPAN);
  return { lo: lo - (span - (hi - lo)) / 2, span };
}
function carbonColour(g, scale) {
  if (g == null) return cssVar("--line");
  const lo = hexToRgb(cssVar("--wind")), mid = hexToRgb(cssVar("--straw")), hi = hexToRgb(cssVar("--lignite"));
  const t = Math.min(1, Math.max(0, (g - scale.lo) / scale.span));
  const rgb = t < 0.5 ? mix(lo, mid, t / 0.5) : mix(mid, hi, (t - 0.5) / 0.5);
  return `rgb(${rgb.join(",")})`;
}

// ---------- data ----------
async function getJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

async function loadOverview() {
  try {
    state.overview = await getJSON("/overview");
    renderStatus(); renderHealth(); renderJobs();
    if (!state.intro) renderRibbon(); // on first load, wait for the plan so the ribbon draws once, complete
  } catch (err) {
    $("#answer").textContent = "The forecast isn't available right now.";
    $("#reason").textContent = "GridPilot's data couldn't be loaded. Try again in a minute.";
    $("#status-text").textContent = "Status unknown";
  }
}

let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(loadPreview, 120); }

async function loadPreview() {
  try {
    state.preview = await getJSON("/plan/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.options),
    });
    renderAnswer(); renderRibbon();
  } catch (err) {
    state.preview = null;
    $("#answer").textContent = "That doesn't fit.";
    $("#reason").textContent = err.message.includes("longer than")
      ? "The job is longer than the time it has to finish. Pick a shorter job or a later deadline."
      : `No time window works for that job: ${err.message}.`;
    renderRibbon();
  }
}

// ---------- the answer ----------
function pct(n) { return Math.round(Math.abs(n)); }
function renderAnswer() {
  const p = state.preview, o = state.options;
  if (!p) return;
  const start = new Date(p.chosen.start);
  const len = `${o.duration_hours}-hour job`;
  const cost = p.savings.cost_pct ?? 0, co2 = p.savings.co2_pct ?? 0;

  if (p.starts_now || (pct(cost) < 1 && pct(co2) < 1)) {
    // Jobs start on the full hour, so "now" usually means the next one: say which.
    const soon = (start - Date.now()) / 60000 > 2;
    $("#answer").textContent = soon ? `Start at ${fmtTime.format(start)}.` : "Start it now.";
    $("#reason").textContent = `${soon ? "That's the next full hour. " : ""}Within the next ${o.within_hours} hours, no other time is cleaner or cheaper for a ${len}.`;
    return;
  }
  $("#answer").textContent = `Start it ${fmtDayTime.format(start)}.`;

  const parts = [];
  const co2Text = co2 >= 0 ? `emit <strong>${pct(co2)}% less CO₂</strong>` : `emit ${pct(co2)}% more CO₂`;
  const costText = cost >= 0 ? `cost <strong>${pct(cost)}% less</strong>` : `cost ${pct(cost)}% more`;
  if (o.objective === "cost") { parts.push(costText); if (pct(co2) >= 1) parts.push(co2Text); }
  else { parts.push(co2Text); if (pct(cost) >= 1) parts.push(costText); }
  const joined = parts.length === 2 ? `${parts[0]} and ${parts[1]}` : parts[0];
  $("#reason").innerHTML = `A ${len} started then should ${joined} than starting right now, and finish by ${fmtTime.format(new Date(p.chosen.end))}.`;
}

// ---------- the ribbon ----------
function renderRibbon() {
  const ov = state.overview;
  if (!ov) return;
  const pts = ov.curve;
  const holder = $("#ribbon");
  const narrow = holder.clientWidth < 700;
  // Draw at the real pixel width: text stays readable on a phone instead of shrinking with the chart.
  const W = Math.max(Math.round(holder.clientWidth), 300);
  const { priceTop, priceH, gap, cellH } = RIBBON;
  const padL = 0, padR = 0, cellTop = priceTop + priceH + gap, axisTop = cellTop + cellH;
  const H = axisTop + 46;
  const n = pts.length, cw = (W - padL - padR) / n;
  const x = (i) => padL + i * cw;

  const deadline = state.preview ? new Date(state.preview.deadline) : null;
  const prices = pts.map((p) => p.eur_mwh).filter((v) => v != null);
  const rawMin = Math.min(...prices), rawMax = Math.max(...prices), pad = (rawMax - rawMin || 10) * 0.12;
  const pMin = rawMin - pad, pMax = rawMax + pad;
  const py = (v) => priceTop + priceH - ((v - pMin) / (pMax - pMin)) * priceH;

  // The fill-in animation plays once, on the first draw only (not on every hover redraw).
  let s = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"${state.intro && state.preview ? ' class="intro"' : ""}>`;

  // price gridlines
  const ticks = niceTicks(pMin, pMax, 3);
  for (const t of ticks) {
    s += `<line class="grid" x1="0" x2="${W}" y1="${py(t)}" y2="${py(t)}"/><text class="plabel" x="${W - 2}" y="${py(t) - 4}" text-anchor="end">€${t}/MWh</text>`;
  }

  // carbon cells
  const scale = carbonScale(pts);
  pts.forEach((p, i) => {
    const late = deadline && new Date(p.ts) >= deadline ? " late" : "";
    s += `<rect class="cell${late}" style="--i:${i}" x="${x(i) + 0.5}" y="${cellTop}" width="${Math.max(cw - 1, 1)}" height="${cellH}" fill="${carbonColour(p.g_per_kwh, scale)}"/>`;
  });

  // price line: published solid, forecast dashed
  const seg = (kind) => {
    let d = "", pen = false;
    pts.forEach((p, i) => {
      const ok = p.eur_mwh != null && (kind === "published" ? p.price_kind === "published" : p.price_kind !== "published" || (pts[i + 1] && pts[i + 1].price_kind !== "published"));
      if (ok) { d += `${pen ? "L" : "M"}${x(i) + cw / 2},${py(p.eur_mwh)}`; pen = true; } else pen = false;
    });
    return d;
  };
  s += `<path class="price" d="${seg("published")}"/><path class="price fc" d="${seg("forecast")}"/>`;

  // deadline marker
  if (deadline) {
    const di = pts.findIndex((p) => new Date(p.ts) >= deadline);
    if (di > 0) s += `<line class="deadline" x1="${x(di)}" x2="${x(di)}" y1="${priceTop - 8}" y2="${axisTop + 6}"/>`;
  }

  // the chosen window
  if (state.preview) {
    const a = pts.findIndex((p) => p.ts === state.preview.chosen.start);
    const b = pts.findIndex((p) => p.ts === state.preview.chosen.end);
    const end = b === -1 ? n : b;
    if (a !== -1) {
      const jx = x(a), jw = x(end) - jx;
      s += `<rect class="job" x="${jx + 1.5}" y="${priceTop - 6}" width="${jw - 3}" height="${axisTop - priceTop + 6}" rx="3"/>`;
      const anchor = jx + jw / 2 > W - 60 ? "end" : jx + jw / 2 < 60 ? "start" : "middle";
      const lx = anchor === "end" ? jx + jw : anchor === "start" ? jx : jx + jw / 2;
      s += `<text class="joblabel" x="${lx}" y="${priceTop - 14}" text-anchor="${anchor}">Your job</text>`;
    }
  }

  // hover outline
  if (state.hover != null) s += `<rect class="hover" x="${x(state.hover) + 0.5}" y="${cellTop}" width="${cw - 1}" height="${cellH}"/>`;

  // axis: hours, and day names at local midnight
  s += `<g class="axis">`;
  const every = narrow ? 6 : 3;
  pts.forEach((p, i) => {
    const d = new Date(p.ts), h = Number(fmtHour.format(d));
    if (i === 0) { s += `<text class="now" x="${x(0)}" y="${axisTop + 18}">now</text>`; return; }
    if (h % every === 0 && x(i) - x(0) > 30) s += `<line x1="${x(i)}" x2="${x(i)}" y1="${axisTop}" y2="${axisTop + 5}"/><text x="${x(i)}" y="${axisTop + 18}" text-anchor="middle">${String(h).padStart(2, "0")}</text>`;
    if (h === 0) s += `<text class="day" x="${x(i) + 3}" y="${axisTop + 40}">${fmtDay.format(d)}</text>`;
  });
  s += `</g></svg>`;
  holder.innerHTML = s;
  if (state.preview) state.intro = false;

  // accessible summary
  const cleanest = pts.reduce((m, p) => (p.g_per_kwh != null && (m == null || p.g_per_kwh < m.g_per_kwh) ? p : m), null);
  holder.setAttribute("aria-label", `Forecast for the next ${n} hours. Cleanest hour: ${cleanest ? fmtDayTime.format(new Date(cleanest.ts)) + ", " + Math.round(cleanest.g_per_kwh) + " grams of CO2 per kilowatt hour" : "unknown"}. Use the left and right arrow keys to read each hour.`);
}

function niceTicks(min, max, count) {
  const span = max - min || 1, step0 = span / count, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  const out = [];
  for (let t = Math.ceil(min / step) * step; t <= max; t += step) out.push(Math.round(t));
  return out;
}

// ---------- tooltip (pointer and keyboard) ----------
function showTip(i) {
  const pts = state.overview && state.overview.curve;
  if (!pts || i == null) { $("#tip").hidden = true; state.hover = null; renderRibbon(); return; }
  state.hover = Math.max(0, Math.min(pts.length - 1, i));
  const p = pts[state.hover];
  const price = p.eur_mwh == null ? "no price" : `€${p.eur_mwh.toFixed(0)}/MWh`;
  const carbon = p.g_per_kwh == null ? "no data" : `${Math.round(p.g_per_kwh)} g CO₂/kWh`;
  const tip = $("#tip");
  tip.innerHTML = `<b>${fmtDayTime.format(new Date(p.ts))}</b><br>${carbon} <span class="k">(${p.carbon_kind})</span><br>${price} <span class="k">(${p.price_kind})</span>`;
  tip.hidden = false;
  renderRibbon();
  // Sit just above the hovered hour, over the price chart (never over the controls).
  const box = $("#ribbon").getBoundingClientRect();
  const cellTopPx = (RIBBON.priceTop + RIBBON.priceH + RIBBON.gap) * box.width / $("#ribbon svg").viewBox.baseVal.width;
  const cx = ((state.hover + 0.5) / pts.length) * box.width;
  const left = Math.min(Math.max(cx - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth);
  tip.style.left = `${left}px`;
  tip.style.top = `${Math.max(cellTopPx - tip.offsetHeight - 8, 0)}px`;
}
function indexFromEvent(e) {
  const box = $("#ribbon").getBoundingClientRect();
  return Math.floor(((e.clientX - box.left) / box.width) * state.overview.curve.length);
}

// ---------- jobs ----------
function renderJobs() {
  const jobs = state.overview.jobs;
  const el = $("#jobs");
  if (!jobs.length) {
    el.innerHTML = `<p>No jobs yet. Submit one with <code>POST /jobs</code> and your API key, and it will appear here with its plan.</p>`;
    return;
  }
  el.innerHTML = `<ul class="jobs">${jobs.map((j) => {
    const when = j.actual_start ? `Ran ${fmtDate.format(new Date(j.actual_start))}` : j.planned_start ? `Starts ${fmtDate.format(new Date(j.planned_start))}` : "Not planned";
    return `<li><button class="row" type="button" aria-expanded="false" data-id="${j.id}">
      <span class="name">${escapeHtml(j.name)}<span class="badge" data-s="${j.status}">${j.status}</span></span>
      <span class="saving" data-saving="${j.id}"></span>
      <span class="when">${when}, ${j.duration_hours} h, optimised for ${j.objective === "carbon" ? "CO₂" : j.objective === "cost" ? "cost" : "both"}</span>
    </button><div class="detail" id="detail-${j.id}" hidden></div></li>`;
  }).join("")}</ul>`;
  jobs.forEach(fillSaving);
}
async function fillSaving(job) {
  try {
    const ex = await getJSON(`/jobs/${job.id}/explain`);
    const key = job.objective === "cost" ? "cost_pct" : "co2_pct";
    const v = ex.savings_vs_asap[key];
    const cell = document.querySelector(`[data-saving="${job.id}"]`);
    if (cell && v != null) cell.innerHTML = `${Math.round(v)}%<small>${job.objective === "cost" ? "cheaper" : "less CO₂"}</small>`;
    const detail = $(`#detail-${job.id}`);
    if (detail) detail.textContent = ex.summary;
  } catch (_) { /* the row still shows without a saving */ }
}
function toggleJob(btn) {
  const open = btn.getAttribute("aria-expanded") === "true";
  btn.setAttribute("aria-expanded", String(!open));
  $(`#detail-${btn.dataset.id}`).hidden = open;
}

// ---------- health ----------
function ago(iso) {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (mins < 2) return "just now";
  if (mins < 90) return `${mins} min ago`;
  const h = Math.round(mins / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
}
function worstSeverity() {
  const open = state.overview.health.open;
  if (!open.length) return "ok";
  return open.some((i) => i.severity === "critical") ? "critical" : "warning";
}
function renderStatus() {
  const sev = worstSeverity(), n = state.overview.health.open.length;
  const status = $("#status");
  status.dataset.state = sev;
  $("#status-text").textContent = sev === "ok" ? "All systems normal" : `${n} open issue${n > 1 ? "s" : ""}`;
}
function renderHealth() {
  const h = state.overview.health, sev = worstSeverity();
  const colour = { ok: "var(--ok)", warning: "var(--warn)", critical: "var(--crit)" }[sev];
  let html = `<p class="health-line"><span class="dot" style="background:${colour}" aria-hidden="true"></span>${
    sev === "ok" ? "Everything is running normally." : `${h.open.length} open issue${h.open.length > 1 ? "s" : ""}.`}</p>`;
  if (h.open.length) {
    html += `<ul class="incidents">${h.open.map((i) => `<li><span class="sev ${i.severity}">${i.severity === "critical" ? "Critical" : "Warning"}:</span> ${escapeHtml(i.title)} <span class="past">since ${fmtDate.format(new Date(i.first_seen))}</span></li>`).join("")}</ul>`;
  }
  html += `<h3>Data last updated</h3><dl class="feeds">${h.feeds.map((f) => `<dt>${escapeHtml(f.label)}</dt><dd>${ago(f.last_success)}</dd>`).join("")}</dl>`;
  const models = state.overview.models.filter((m) => m.trained_at);
  if (models.length) {
    const m = models[0];
    html += `<h3>Forecast models</h3><p>Retrained ${fmtDate.format(new Date(m.trained_at))} on ${m.train_days} days of data${m.uses_weather ? ", with weather forecasts" : ""}. GridPilot retrains them every weekend in its own cleanest window.</p>`;
  }
  if (h.recently_resolved.length) {
    html += `<h3>Recently fixed</h3><ul class="incidents">${h.recently_resolved.map((i) => `<li>${escapeHtml(i.title)} <span class="past">resolved ${fmtDate.format(new Date(i.resolved_at))}</span></li>`).join("")}</ul>`;
  }
  $("#health-body").innerHTML = html;
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ---------- wiring ----------
document.querySelectorAll(".segmented").forEach((group) => {
  group.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    const v = btn.dataset.value;
    state.options[group.dataset.name] = isNaN(Number(v)) ? v : Number(v);
    schedulePreview();
  });
});
$("#jobs").addEventListener("click", (e) => { const btn = e.target.closest("button.row"); if (btn) toggleJob(btn); });

const ribbon = $("#ribbon");
ribbon.addEventListener("pointermove", (e) => state.overview && showTip(indexFromEvent(e)));
ribbon.addEventListener("pointerleave", () => showTip(null));
ribbon.addEventListener("keydown", (e) => {
  if (!state.overview) return;
  if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    showTip((state.hover ?? -1) + (e.key === "ArrowRight" ? 1 : -1));
  } else if (e.key === "Escape") showTip(null);
});
ribbon.addEventListener("blur", () => showTip(null));
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderRibbon, 150); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderRibbon);

loadOverview().then(loadPreview);
setInterval(() => loadOverview().then(loadPreview), 5 * 60 * 1000);
