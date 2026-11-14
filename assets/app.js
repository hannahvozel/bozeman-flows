/* Bozeman Flow Report
 * Pulls live readings from the USGS Water Data API on every page load and
 * classifies them using config/rivers.json. If the live call fails, falls back
 * to data/latest.json, the snapshot the GitHub Action writes every hour.
 */
(() => {
  "use strict";

  const API = "https://api.waterdata.usgs.gov/ogcapi/v1";
  const SNAPSHOT_URL = "data/latest.json";
  const MAX_COMPARE = 3;
  const TZ = "America/Denver";
  const STEADY_PCT = 5;

  const state = {
    config: null,
    sites: {},          // usgs -> {site, river}
    mode: "compare",
    selected: [],
    latest: {},         // usgs -> {cfs, time, approval}
    series: {},         // usgs -> [[ms, cfs], ...]
    source: null,       // "live" | "snapshot"
    snapshot: null,
    charts: new Map(),
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const fmtCfs = (v) => (v == null ? "–" : Math.round(v).toLocaleString("en-US"));
  const fmtWhen = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", hour: "numeric", minute: "2-digit" });
  const fmtDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short" });
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const levelColor = (level) => cssVar(`--lv-${level}`);

  // ---------------------------------------------------------------- data ----
  async function fetchJSON(url, timeoutMs = 15000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(url, { signal: ctrl.signal, headers: { Accept: "application/geo+json, application/json" } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  /** Fetch every feature in an OGC API collection query, following rel=next links. */
  async function fetchItems(collection, params) {
    const qs = new URLSearchParams({ f: "json", skipGeometry: "true", ...params });
    let url = `${API}/collections/${collection}/items?${qs}`;
    const out = [];
    for (let page = 0; url && page < 20; page++) {
      const body = await fetchJSON(url);
      out.push(...(body.features || []));
      url = (body.links || []).find((l) => l.rel === "next")?.href || null;
    }
    return out;
  }

  const num = (raw) => {
    const v = parseFloat(raw);
    return Number.isFinite(v) && v >= 0 ? v : null; // negative sentinels (-999999) = no data
  };
  const siteOf = (feat) => (feat.properties.monitoring_location_id || "").replace(/^USGS-/, "");

  async function fetchLiveLatest(ids) {
    const feats = await fetchItems("latest-continuous", {
      monitoring_location_id: ids.map((i) => `USGS-${i}`).join(","),
      parameter_code: "00060",
      limit: "1000",
    });
    const out = {};
    for (const f of feats) {
      const p = f.properties, id = siteOf(f);
      const rec = (out[id] ||= { cfs: null, time: null, approval: null });
      const t = Date.parse(p.time);
      if (p.parameter_code === "00060" && (!rec.time || t > Date.parse(rec.time))) {
        Object.assign(rec, { cfs: num(p.value), time: p.time, approval: p.approval_status });
      } 
    }
    if (!Object.values(out).some((r) => r.cfs != null)) throw new Error("no discharge readings returned");
    return out;
  }

  async function fetchLiveSeries(ids) {
    const feats = await fetchItems("continuous", {
      monitoring_location_id: ids.map((i) => `USGS-${i}`).join(","),
      parameter_code: "00060",
      time: "P7D",
      properties: "monitoring_location_id,time,value",
      limit: "10000",
    });
    const out = Object.fromEntries(ids.map((i) => [i, []]));
    for (const f of feats) {
      const v = num(f.properties.value);
      if (v != null) (out[siteOf(f)] ||= []).push([Date.parse(f.properties.time), v]);
    }
    for (const pts of Object.values(out)) pts.sort((a, b) => a[0] - b[0]);
    return out;
  }

  async function loadSnapshot() {
    if (!state.snapshot) state.snapshot = await fetchJSON(`${SNAPSHOT_URL}?t=${Date.now()}`);
    return state.snapshot;
  }

  function useSnapshotLatest(snap) {
    state.latest = {};
    for (const [id, s] of Object.entries(snap.sites || {})) {
      state.latest[id] = { cfs: s.cfs, time: s.time, approval: s.approval };
    }
  }

  function snapshotSeries(snap, id) {
    return ((snap.sites || {})[id]?.series || []).map(([t, v]) => [Date.parse(t), v]);
  }

  /** Make sure 7-day series exist for the selected rivers' gauges. */
  async function ensureSeries() {
    const needed = selectedSiteIds().filter((id) => !state.series[id]);
    if (!needed.length) return;
    if (state.source === "live") {
      try {
        Object.assign(state.series, await fetchLiveSeries(needed));
        return;
      } catch (err) {
        console.warn("Live history failed, trying snapshot:", err);
      }
    }
    try {
      const snap = await loadSnapshot();
      for (const id of needed) state.series[id] = snapshotSeries(snap, id);
    } catch {
      for (const id of needed) state.series[id] = [];
    }
  }

  async function refresh() {
    const btn = $("#refresh");
    btn.disabled = true;
    setStatus("Checking the gauges…");
    state.series = {};
    state.snapshot = null;
    const ids = Object.keys(state.sites);
    try {
      state.latest = await fetchLiveLatest(ids);
      state.source = "live";
      setStatus(`Live from USGS · checked ${fmtWhen.format(new Date())} MT`);
    } catch (err) {
      console.warn("Live USGS request failed:", err);
      try {
        const snap = await loadSnapshot();
        useSnapshotLatest(snap);
        state.source = "snapshot";
        setStatus(`USGS didn't answer, showing the snapshot from ${fmtWhen.format(new Date(snap.generated_at))} MT`, true);
      } catch {
        state.latest = {};
        state.source = null;
        setStatus("Couldn't reach USGS and no snapshot is available. Try Refresh in a minute.", true);
      }
    }
    renderGlance();
    renderChips();
    await renderRivers();
    btn.disabled = false;
  }

  // ------------------------------------------------------ classification ----
  function classify(site, cfs) {
    if (cfs == null) return null;
    const i = site.bands.findIndex((b) => b.max == null || cfs < b.max);
    const band = site.bands[i];
    const lv = state.config.levels[band.level];
    // Position for the ladder pin: equal-width segments, interpolated inside the band.
    const lo = i === 0 ? 0 : site.bands[i - 1].max;
    const hi = band.max ?? lo * 1.6;
    const frac = Math.min(1, Math.max(0, (cfs - lo) / (hi - lo || 1)));
    return { ...band, index: i, label: lv.label, general: lv.general, pos: (i + frac) / site.bands.length };
  }

  function trend(points, hours = 24) {
    if (!points || points.length < 2) return null;
    const [tLast, vLast] = points[points.length - 1];
    const target = tLast - hours * 3600e3;
    let best = points[0];
    for (const p of points) if (Math.abs(p[0] - target) < Math.abs(best[0] - target)) best = p;
    if (Math.abs(best[0] - target) > 3 * 3600e3 || best[1] <= 0) return null;
    const pct = ((vLast - best[1]) / best[1]) * 100;
    const direction = Math.abs(pct) < STEADY_PCT ? "steady" : pct > 0 ? "rising" : "falling";
    return { pct, direction };
  }

  // ----------------------------------------------------------- selection ----
  const riverById = (id) => state.config.rivers.find((r) => r.id === id);
  const selectedSiteIds = () => state.selected.flatMap((id) => riverById(id).sites.map((s) => s.usgs));

  function toggleRiver(id) {
    hint("");
    if (state.mode === "one") {
      state.selected = [id];
    } else if (state.selected.includes(id)) {
      state.selected = state.selected.filter((r) => r !== id);
    } else if (state.selected.length >= MAX_COMPARE) {
      hint(`You can line up ${MAX_COMPARE} rivers at once. Drop one first.`);
      return;
    } else {
      state.selected = [...state.selected, id];
    }
    onSelectionChange();
  }

  function setMode(mode) {
    state.mode = mode;
    if (mode === "one") state.selected = state.selected.slice(0, 1);
    if (!state.selected.length) state.selected = [state.config.rivers[0].id];
    hint("");
    onSelectionChange();
  }

  function onSelectionChange() {
    const url = new URL(location.href);
    url.searchParams.set("mode", state.mode);
    url.searchParams.set("rivers", state.selected.join(","));
    history.replaceState(null, "", url);
    renderChips();
    renderGlance();
    renderRivers();
  }

  function readURL() {
    const params = new URLSearchParams(location.search);
    const valid = new Set(state.config.rivers.map((r) => r.id));
    state.mode = params.get("mode") === "one" ? "one" : "compare";
    const wanted = (params.get("rivers") || "").split(",").filter((id) => valid.has(id));
    state.selected = wanted.length ? wanted : state.config.rivers.slice(0, 3).map((r) => r.id);
    state.selected = state.selected.slice(0, state.mode === "one" ? 1 : MAX_COMPARE);
  }

  // ------------------------------------------------------------- render ----
  function setStatus(text, warn = false) {
    const el = $("#status");
    el.textContent = text;
    el.classList.toggle("warn", warn);
  }

  function hint(text) {
    const el = $("#hint");
    el.textContent = text;
    el.hidden = !text;
  }

  function primaryInfo(river) {
    const site = river.sites[0];
    const cfs = state.latest[site.usgs]?.cfs ?? null;
    return { site, cfs, c: classify(site, cfs) };
  }

  function renderChips() {
    document.querySelectorAll(".segmented button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === state.mode)));
    const wrap = $("#chips");
    wrap.replaceChildren(...state.config.rivers.map((river) => {
      const { c } = primaryInfo(river);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.setAttribute("aria-pressed", String(state.selected.includes(river.id)));
      const dot = document.createElement("span");
      dot.className = "dot";
      if (c) dot.style.background = levelColor(c.level);
      b.append(dot, river.name);
      b.addEventListener("click", () => toggleRiver(river.id));
      return b;
    }));
  }

  function renderGlance() {
    $("#glance").replaceChildren(...state.config.rivers.map((river) => {
      const { site, cfs, c } = primaryInfo(river);
      const b = document.createElement("button");
      b.type = "button";
      b.className = "glance-item";
      b.setAttribute("aria-pressed", String(state.selected.includes(river.id)));
      if (c) b.style.borderLeftColor = levelColor(c.level);
      const name = el("span", "g-name", river.name);
      const num = el("span", "g-cfs", cfs == null ? "–" : `${fmtCfs(cfs)} cfs`);
      const sub = el("span", "g-sub", c ? `${site.name} · ${c.label} · Class ${c.class}` : `${site.name} · no reading`);
      b.append(name, num, sub);
      b.addEventListener("click", () => toggleRiver(river.id));
      return b;
    }));
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  async function renderRivers() {
    const wrap = $("#rivers");
    for (const ro of state.charts.values()) ro.disconnect();
    state.charts.clear();

    if (!state.selected.length) {
      wrap.style.setProperty("--cols", 1);
      wrap.replaceChildren(el("p", "empty", "Pick a river above to see its gauges."));
      return;
    }
    wrap.classList.toggle("single", state.selected.length === 1);
    wrap.style.setProperty("--cols", state.selected.length);

    const tpl = $("#site-card");
    const cards = [];
    wrap.replaceChildren(...state.selected.map((rid) => {
      const river = riverById(rid);
      const col = el("section", "river");
      col.setAttribute("aria-label", river.name);
      const sites = el("div", "sites");
      col.append(el("h3", null, river.name), el("p", "blurb", river.blurb), sites);
      for (const site of river.sites) {
        const card = tpl.content.firstElementChild.cloneNode(true);
        fillCard(card, river, site);
        sites.append(card);
        cards.push([card, site]);
      }
      return col;
    }));

    cards.forEach(([card]) => ($(".chart-msg", card).textContent = "Loading 7-day history…"));
    await ensureSeries();
    // Selection may have changed while history loaded.
    for (const [card, site] of cards) if (card.isConnected) drawHistory(card, site);
  }

  function fillCard(card, river, site) {
    const r = state.latest[site.usgs] || {};
    const c = classify(site, r.cfs);
    $(".site-name", card).textContent = `${river.name} ${site.name}`;
    $(".site-run", card).textContent = site.run;
    $(".cfs-num", card).textContent = fmtCfs(r.cfs);
    const badge = $(".badge", card);
    if (c) {
      badge.textContent = c.label;
      badge.style.background = levelColor(c.level);
      badge.title = `General level: ${c.general}`;
      $(".class", card).textContent = c.class;
      $(".band-note", card).textContent = c.note;
    } else {
      badge.textContent = "No reading";
      $(".band-note", card).textContent = "The gauge isn't reporting right now (ice, maintenance or a seasonal outage).";
    }

    const track = $(".ladder-track", card);
    track.replaceChildren(...site.bands.map((b) => {
      const s = document.createElement("span");
      s.style.background = levelColor(b.level);
      s.title = `${state.config.levels[b.level].label}: ${bandRange(site, b)}`;
      return s;
    }));
    const pin = $(".ladder-pin", card);
    if (c) {
      pin.style.display = "block";
      pin.style.left = `${(c.pos * 100).toFixed(1)}%`;
    }

    const link = $(".gauge-link", card);
    link.href = `https://waterdata.usgs.gov/monitoring-location/USGS-${site.usgs}/`;
    link.textContent = `USGS ${site.usgs}`;
    $(".updated", card).textContent = r.time
      ? `Reading ${fmtWhen.format(new Date(r.time))} MT${r.approval === "Provisional" ? " · provisional" : ""}`
      : "";
  }

  function bandRange(site, band) {
    const i = site.bands.indexOf(band);
    const lo = i === 0 ? 0 : site.bands[i - 1].max;
    return band.max == null ? `${fmtCfs(lo)}+ cfs` : `${fmtCfs(lo)}–${fmtCfs(band.max)} cfs`;
  }

  function drawHistory(card, site) {
    const pts = state.series[site.usgs] || [];
    const msg = $(".chart-msg", card);
    const t = trend(pts) || (state.source === "snapshot" ? state.snapshot?.sites?.[site.usgs]?.trend_24h : null);
    const trendEl = $(".trend", card);
    if (t) {
      const arrow = t.direction === "rising" ? "▲" : t.direction === "falling" ? "▼" : "▬";
      trendEl.textContent = `${arrow} ${t.pct > 0 ? "+" : ""}${Math.round(t.pct)}%`;
      trendEl.className = `trend ${t.direction}`;
      trendEl.title = `${t.direction} over the last 24 hours`;
    }
    if (!pts.length) {
      msg.textContent = "No recent history available.";
      return;
    }
    msg.textContent = "";
    const box = $(".chart-box", card);
    const draw = () => renderChart(box, site, pts);
    draw();
    const ro = new ResizeObserver(() => draw());
    ro.observe(box);
    state.charts.set(site.usgs, ro);
  }

  /** Dependency-free SVG line chart with the flow bands shaded behind it. */
  const SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  }

  function niceStep(range, target = 4) {
    const raw = range / target;
    const mag = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  }

  function renderChart(box, site, pts) {
    const host = $(".chart", box);
    const W = Math.max(240, box.clientWidth), H = box.clientHeight || 170;
    if (host.dataset.w === String(W) && host.firstChild) return; // no size change
    host.dataset.w = String(W);
    const m = { l: 46, r: 10, t: 8, b: 22 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    const vals = pts.map((p) => p[1]);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.25, hi * 0.08, 20);
    const step = niceStep(hi + pad - Math.max(0, lo - pad));
    const y0 = Math.max(0, Math.floor((lo - pad) / step) * step);
    const y1 = Math.ceil((hi + pad) / step) * step;
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    const X = (t) => m.l + ((t - t0) / (t1 - t0 || 1)) * iw;
    const Y = (v) => m.t + ih - ((v - y0) / (y1 - y0)) * ih;

    const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
    const muted = cssVar("--muted"), line = cssVar("--line");

    // flow bands
    site.bands.forEach((b, i) => {
      const bl = Math.max(i === 0 ? 0 : site.bands[i - 1].max, y0);
      const bh = Math.min(b.max ?? Infinity, y1);
      if (bh <= bl) return;
      svg.append(svgEl("rect", { x: m.l, y: Y(bh), width: iw, height: Y(bl) - Y(bh), fill: levelColor(b.level), "fill-opacity": 0.17 }));
      if (b.max != null && b.max > y0 && b.max < y1) {
        svg.append(svgEl("line", { x1: m.l, x2: m.l + iw, y1: Y(b.max), y2: Y(b.max), stroke: levelColor(b.level), "stroke-dasharray": "3 3", "stroke-opacity": 0.7 }));
      }
    });

    // y grid + labels
    for (let v = y0; v <= y1 + 1e-9; v += step) {
      svg.append(svgEl("line", { x1: m.l, x2: m.l + iw, y1: Y(v), y2: Y(v), stroke: line, "stroke-width": 0.6 }));
      const label = svgEl("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", fill: muted, "font-size": 11 });
      label.textContent = Math.round(v).toLocaleString("en-US");
      svg.append(label);
    }

    // x ticks at each new local (Mountain Time) day
    let prevDay = null;
    for (const [t] of pts) {
      const day = fmtDay.format(new Date(t));
      if (prevDay !== null && day !== prevDay) {
        svg.append(svgEl("line", { x1: X(t), x2: X(t), y1: m.t, y2: m.t + ih, stroke: line, "stroke-width": 0.6 }));
        if (X(t) < m.l + iw - 14) {
          const label = svgEl("text", { x: X(t) + 3, y: H - 6, fill: muted, "font-size": 11 });
          label.textContent = day;
          svg.append(label);
        }
      }
      prevDay = day;
    }

    // flow line
    const d = pts.map(([t, v], i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join("");
    svg.append(svgEl("path", { d, fill: "none", stroke: cssVar("--river"), "stroke-width": 2.2, "stroke-linejoin": "round" }));
    const [lt, lv] = pts[pts.length - 1];
    svg.append(svgEl("circle", { cx: X(lt), cy: Y(lv), r: 3.5, fill: cssVar("--river") }));

    // hover readout
    const cursor = svgEl("g", { visibility: "hidden" });
    const vline = svgEl("line", { y1: m.t, y2: m.t + ih, stroke: cssVar("--ink"), "stroke-width": 1 });
    const dot = svgEl("circle", { r: 4, fill: cssVar("--paper"), stroke: cssVar("--ink"), "stroke-width": 2 });
    cursor.append(vline, dot);
    svg.append(cursor);
    const tip = $(".tip", box);
    const hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" });
    hit.addEventListener("pointermove", (ev) => {
      const r = svg.getBoundingClientRect();
      const tx = t0 + ((ev.clientX - r.left - m.l) / iw) * (t1 - t0);
      let lo2 = 0, hi2 = pts.length - 1;
      while (hi2 - lo2 > 1) { const mid = (lo2 + hi2) >> 1; (pts[mid][0] < tx ? (lo2 = mid) : (hi2 = mid)); }
      const p = Math.abs(pts[lo2][0] - tx) < Math.abs(pts[hi2][0] - tx) ? pts[lo2] : pts[hi2];
      const px = X(p[0]), py = Y(p[1]);
      vline.setAttribute("x1", px); vline.setAttribute("x2", px);
      dot.setAttribute("cx", px); dot.setAttribute("cy", py);
      cursor.setAttribute("visibility", "visible");
      const c = classify(site, p[1]);
      tip.textContent = `${fmtWhen.format(new Date(p[0]))} · ${fmtCfs(p[1])} cfs · ${c.label}`;
      tip.hidden = false;
      tip.style.left = `${Math.min(Math.max(px, 90), W - 90)}px`;
    });
    hit.addEventListener("pointerleave", () => { cursor.setAttribute("visibility", "hidden"); tip.hidden = true; });
    svg.append(hit);

    host.replaceChildren(svg);
  }

  function renderLegend() {
    $("#legend-levels").replaceChildren(...Object.entries(state.config.levels).map(([key, lv]) => {
      const li = document.createElement("li");
      const sw = el("span", "sw");
      sw.style.background = levelColor(key);
      li.append(sw, `${lv.label} `, el("small", null, `(${lv.general})`));
      return li;
    }));
  }

  // --------------------------------------------------------------- boot ----
  async function init() {
    try {
      state.config = await fetchJSON("config/rivers.json");
    } catch (err) {
      setStatus("Couldn't load config/rivers.json. If you opened the file directly, serve the folder instead (see README).", true);
      return;
    }
    for (const river of state.config.rivers) for (const site of river.sites) state.sites[site.usgs] = { site, river };
    readURL();
    renderLegend();
    renderChips();
    document.querySelectorAll(".segmented button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
    $("#refresh").addEventListener("click", refresh);
    matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => { renderChips(); renderGlance(); renderRivers(); renderLegend(); });
    await refresh();
  }

  init();
})();
