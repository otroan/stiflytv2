// The route planner (the feature `planner`, ADR-005): click where a walk begins and where it ends, anywhere in
// Norway, and get a way on foot between them along DNT's routes as much as you like, with the climb, the time and
// days that end at huts. The tab is only there for one who has the feature; the server refuses the rest.
// What a stretch is, is told by its colour: on the map, and in the list of what the way adds up to, which is the legend.
const PLAN_CLASSES = { dnt: ["DNT's routes", "#c8161d"], marked: ["Other marked routes", "#e8741c"], path: ["Paths", "#8a5a2b"],
                       road: ["Roads", "#5f6b73"], ferry: ["Ferries", "#1f6fc5"], off: ["No path or road", "#1f2a30"] };
const PLAN_KEY = "stiflyt2.plan";
const planAsk = Object.assign({ points: [], prefer: 0.6, ferries: true, ice: false, alpine: false, hours: 7 },
  (() => { try { return JSON.parse(localStorage.getItem(PLAN_KEY)) || {}; } catch { return {}; } })());
let plan = null, planProblem = null, planBusy = false, planHeights = null, heightsProblem = null, heightsBusy = false;
let planAsked = 0, planMarkers = [], planDay = null;
const hasPlanner = () => (me.features || []).includes("planner");
const letter = i => String.fromCharCode(65 + i);
const hoursAndMinutes = m => `${Math.floor(m / 60)} h ${String(Math.round(m) % 60).padStart(2, "0")}`;

function keepAsk() { localStorage.setItem(PLAN_KEY, JSON.stringify(planAsk)); }
// A click on the map: the first is where the walk begins, the next where it ends, and each one after that goes on
// further from there, so that the end before becomes a place passed on the way.
function planAt(at) {
  if (planAsk.points.length >= 12) { planProblem = "A walk goes through 12 points at the most."; render(); return; }
  planAsk.points.push([Number(at.lng.toFixed(6)), Number(at.lat.toFixed(6))]);
  replan();
}
function replan() {
  keepAsk();
  plan = planHeights = null; planProblem = heightsProblem = null; planDay = null;
  if (planAsk.points.length < 2) { render(); return; }
  askPlan();
}
let planTimer = null;
function replanSoon() { clearTimeout(planTimer); planTimer = setTimeout(replan, 400); }
async function askPlan() {
  const asked = ++planAsked;
  planBusy = true; render();
  try {
    const got = await send("POST", "planner/plan", { points: planAsk.points, prefer: planAsk.prefer, ferries: planAsk.ferries, ice: planAsk.ice, alpine: planAsk.alpine });
    if (asked !== planAsked) return;
    plan = got;
  } catch (err) { if (asked === planAsked) planProblem = err.message; }
  if (asked !== planAsked) return;
  planBusy = false; render();
  if (plan) askHeights(asked);
}
async function askHeights(asked) {
  heightsBusy = true; render();
  try {
    const parts = plan.steps.map(s => ({ coords: plan.coords.slice(s.at[0], s.at[1] + 1), ferry: s.class === "ferry" }));
    const got = await send("POST", "planner/heights", { parts, hours: planAsk.hours });
    if (asked !== planAsked) return;
    planHeights = got;
  } catch (err) { if (asked === planAsked) heightsProblem = err.message; }
  if (asked !== planAsked) return;
  heightsBusy = false; render();
}
function showPlan() {
  if (!plan) return;
  const lon = plan.coords.map(c => c[0]), lat = plan.coords.map(c => c[1]);
  map.fitBounds([[Math.min(...lon), Math.min(...lat)], [Math.max(...lon), Math.max(...lat)]], { padding: 60, maxZoom: 14, animate: !calm });
}
function planGpx() {
  const safe = s => String(s).replace(/[<>&"]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const credits = [...plan.credits, ...(planHeights ? planHeights.credits : [])];
  const huts = planHeights ? planHeights.days.filter(d => d.hut) : [];
  const text = `<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Stiflyt" xmlns="http://www.topografix.com/GPX/1/1">\n`
    + `<metadata><copyright author="${safe(credits.join(". "))}"/></metadata>\n`
    + huts.map(d => `<wpt lat="${d.hut.lat}" lon="${d.hut.lon}"><name>${safe(`Day ${d.day}: ${d.hut.name || "a hut"}`)}</name></wpt>`).join("\n")
    + `\n<trk><name>${safe(planName())}</name><desc>${safe(plan.steps.map(s => `${s.says}, ${km(s.metres / 1000)}`).join(". "))}.</desc><trkseg>\n`
    + plan.coords.map(c => `<trkpt lat="${c[1]}" lon="${c[0]}"/>`).join("\n") + "\n</trkseg></trk>\n</gpx>\n";
  const link = el("a", { href: URL.createObjectURL(new Blob([text], { type: "application/gpx+xml" })), download: "walk.gpx" });
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 5000);
}
function planName() {
  const days = planHeights ? planHeights.days : [];
  const ends = days.filter(d => d.hut).map(d => d.hut.name).filter(Boolean);
  return ends.length ? `Walk by ${ends.join(", ")}` : "A walk";
}
// How high the way goes, drawn small: the days are marked where they end.
function profileOf(H) {
  const at = H.profile.at_m, z = H.profile.z, W = 320, Ht = 90, pad = 4;
  const zs = z.filter(v => v !== null);
  if (zs.length < 2 || !at.length) return "";
  const lo = Math.min(...zs), hi = Math.max(...zs), span = Math.max(hi - lo, 50), far = at[at.length - 1] || 1;
  const X = a => pad + (W - 2 * pad) * a / far, Y = v => Ht - pad - (Ht - 2 * pad) * (v - lo) / span;
  const pts = at.map((a, i) => z[i] === null ? null : `${X(a).toFixed(1)},${Y(z[i]).toFixed(1)}`).filter(Boolean).join(" ");
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${Ht}`); svg.setAttribute("class", "plan-profile"); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Height along the way: from ${Math.round(lo)} to ${Math.round(hi)} m`);
  const add = (tag, attrs) => { const e = document.createElementNS(ns, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); svg.append(e); return e; };
  add("polygon", { points: `${X(0)},${Ht - pad} ${pts} ${X(far)},${Ht - pad}`, fill: "#e9e4dc" });
  add("polyline", { points: pts, fill: "none", stroke: "#5f6b73", "stroke-width": 1.5 });
  for (const d of H.days.slice(0, -1)) add("line", { x1: X(d.at_m), x2: X(d.at_m), y1: pad, y2: Ht - pad, stroke: "#c8161d", "stroke-dasharray": "3 3" });
  add("text", { x: pad + 2, y: 12, "font-size": 10, fill: "#5f6b73" }).textContent = `${Math.round(hi)} m`;
  add("text", { x: pad + 2, y: Ht - 6, "font-size": 10, fill: "#5f6b73" }).textContent = `${Math.round(lo)} m`;
  return svg;
}
function renderPlanner() {
  const P = planAsk.points;
  const pointList = el("ol", { class: "plan-points" }, ...P.map((p, i) => el("li", {},
    el("span", { class: "plan-letter" }, letter(i)),
    el("span", { class: "grow" }, i === 0 ? "Begins" : i === P.length - 1 ? "Ends" : "Passes", el("span", { class: "sub" }, ` ${p[1].toFixed(4)} N ${p[0].toFixed(4)} E`)),
    el("button", { class: "link", type: "button", title: `Take point ${letter(i)} away`, "aria-label": `Take point ${letter(i)} away`, onclick: () => { P.splice(i, 1); replan(); } }, "✕"))));
  const slider = el("input", { type: "range", min: 0, max: 100, step: 5, value: Math.round(planAsk.prefer * 100), "aria-label": "How much DNT's routes are preferred",
    oninput: e => { planAsk.prefer = Number(e.target.value) / 100; replanSoon(); } });
  const form = el("div", { class: "trip-ask" },
    el("label", { class: "plan-prefer" }, el("span", {}, "Shortest"), slider, el("span", {}, "Along DNT's routes")),
    ...[["ferries", "Ferries may be taken"], ["ice", "Over glaciers too"], ["alpine", "Demanding alpine paths and via ferratas too"]].map(([key, says]) =>
      el("label", {}, el("input", { type: "checkbox", checked: planAsk[key] ? "" : false, onchange: e => { planAsk[key] = e.target.checked; replan(); } }), ` ${says}`)),
    el("label", {}, "A day is ", el("input", { type: "number", min: 3, max: 14, step: 0.5, value: planAsk.hours, style: "width:5em",
      onchange: e => { planAsk.hours = Math.min(14, Math.max(3, Number(e.target.value) || 7)); keepAsk(); if (plan) { planHeights = null; askHeights(planAsked); } } }), " hours of walking, with no rests"));
  const told = [];
  if (planBusy) told.push(el("div", { class: "sub" }, "Finding the way…"));
  if (planProblem) told.push(el("div", { class: "error", role: "alert" }, `No way was found: ${planProblem}`));
  if (plan && !planBusy) {
    const T = plan.total, H = planHeights;
    told.push(el("h2", { class: "next" }, `${km(T.metres / 1000)}` + (H ? `, ${H.days.length} ${H.days.length === 1 ? "day" : "days"}` : "")),
      el("div", { class: "sub" }, H ? `${hoursAndMinutes(H.minutes)} of walking by Naismith's rule, with no rests; ${H.up_m.toLocaleString("en")} m up and ${H.down_m.toLocaleString("en")} m down, `
          + `from ${H.lowest_m} to ${H.highest_m} m above the sea.` + (H.ferries.length ? ` Ferries besides: about ${hoursAndMinutes(H.ferries.reduce((t, f) => t + f.minutes, 0))} on board.` : "")
        : heightsBusy ? "Asking Kartverket how high it goes…" : heightsProblem ? `The heights could not be had: ${heightsProblem}` : ""),
      H ? profileOf(H) : "",
      el("ul", { class: "plan-by" }, ...Object.entries(PLAN_CLASSES).filter(([k]) => T.by[k]).map(([k, [says, colour]]) =>
        el("li", {}, el("span", { class: "plan-swatch", style: `background:${colour}` }), `${says}: ${km(T.by[k] / 1000)}`))),
      ...plan.off.map(o => el("div", { class: "sub" }, `${letter(o.point)} is ${o.off_m.toLocaleString("en")} m from the nearest path or road: that bit is drawn straight.`)));
    if (H && H.days.length > 1) told.push(el("ol", { class: "trip-days" }, ...H.days.map(d => el("li", { class: planDay === d.day ? "hit current" : "hit" },
      el("span", { class: "swatch", style: "background:#1f2a30" }, String(d.day)),
      el("div", { class: "grow" },
        el("div", {}, d.last ? "To the end" : d.hut ? `To ${d.hut.name || "a hut"}` : "To where the hours run out"),
        el("div", { class: "sub" }, `${km(d.metres / 1000)}, ${hoursAndMinutes(d.minutes)}`
          + (d.hut ? `${d.hut.kind === "wilderness_hut" ? ", an open shelter" : ""}${d.hut.operator ? `, ${d.hut.operator}` : ""}` : d.last ? "" : ": no hut near the way, a night in a tent"))),
      ))));
    told.push(el("details", { class: "how" }, el("summary", {}, `The way, in ${plan.steps.length} ${plan.steps.length === 1 ? "step" : "steps"}`),
        el("ol", { class: "trip-steps" }, ...plan.steps.map(s => el("li", {}, el("span", { class: "plan-swatch", style: `background:${(PLAN_CLASSES[s.class] || PLAN_CLASSES.path)[1]}` }),
          `${s.says}, ${km(s.metres / 1000)}`)))),
      el("div", { class: "actions" }, el("button", { class: "btn", onclick: planGpx }, "As a GPX file"), el("button", { class: "btn", onclick: showPlan }, "Show all of it")),
      el("div", { class: "sub" }, `Whether a hut is open, a bridge is up or a ferry runs is not known here. ${[...plan.credits, ...(H ? H.credits : [])].join(". ")}.`));
  }
  panel.replaceChildren(
    el("h2", { class: "area" }, "Plan a walk"),
    el("div", { class: "sub", style: "margin-bottom:8px" }, P.length ? "Click the map to go on further from the last point. Drag a point to move it."
      : "Click the map where the walk begins, anywhere in Norway, then where it ends. Click again to go on further; drag a point to move it."),
    P.length ? pointList : "",
    P.length ? el("div", { class: "actions" },
      P.length > 1 ? el("button", { class: "btn", onclick: () => { P.reverse(); replan(); } }, "The other way") : "",
      el("button", { class: "btn", onclick: () => { P.length = 0; replan(); } }, "Start again")) : "",
    form, ...told);
}
// The way on the map, in the colours of what it is; the points as letters that can be dragged; where each day ends.
let planLayers = false;
function drawPlan() {
  whenLoaded(() => {
    const on = view === "planner";
    if (!planLayers) {
      if (!on) return;
      map.addSource("plan", { type: "geojson", data: empty });
      map.addSource("plan-days", { type: "geojson", data: empty });
      map.addLayer({ id: "plan-casing", type: "line", source: "plan", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": 8 } });
      map.addLayer({ id: "plan", type: "line", source: "plan", filter: ["!", ["get", "dashed"]], layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "colour"], "line-width": 4.5 } });
      map.addLayer({ id: "plan-dashed", type: "line", source: "plan", filter: ["get", "dashed"], layout: { "line-join": "round" }, paint: { "line-color": ["get", "colour"], "line-width": 4, "line-dasharray": [1.2, 1.2] } });
      map.addLayer({ id: "plan-days", type: "circle", source: "plan-days", paint: { "circle-radius": 9, "circle-color": "#1f2a30", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
      map.addLayer({ id: "plan-day-numbers", type: "symbol", source: "plan-days", layout: { "text-field": ["get", "day"], "text-size": 11, "text-font": ["Open Sans Semibold"], "text-allow-overlap": true }, paint: { "text-color": "#fff" } });
      planLayers = true;
    }
    const steps = on && plan ? plan.steps : [];
    map.getSource("plan").setData({ type: "FeatureCollection", features: steps.map(s => ({ type: "Feature",
      properties: { colour: s.ice ? GLACIER : (PLAN_CLASSES[s.class] || PLAN_CLASSES.path)[1], dashed: s.class === "ferry" || s.class === "off" || s.ice },
      geometry: { type: "LineString", coordinates: plan.coords.slice(s.at[0], s.at[1] + 1) } })) });
    const days = on && plan && planHeights ? planHeights.days.filter(d => !d.last) : [];
    map.getSource("plan-days").setData({ type: "FeatureCollection", features: days.map(d => ({ type: "Feature", properties: { day: String(d.day) },
      geometry: { type: "Point", coordinates: d.hut ? [d.hut.lon, d.hut.lat] : pointAlong(d.at_m) } })).filter(f => f.geometry.coordinates) });
    if (on && plan) for (const id of ["routes", "routes-ice"]) if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", 0.3);
    // The points: markers that can be dragged, made anew when they are not the ones shown.
    for (const m of planMarkers) m.remove();
    planMarkers = on ? planAsk.points.map((p, i) => {
      const dot = el("div", { class: "plan-marker", title: `${letter(i)}: drag to move` }, letter(i));
      const m = new maplibregl.Marker({ element: dot, draggable: true }).setLngLat(p).addTo(map);
      m.on("dragend", () => { const at = m.getLngLat(); planAsk.points[i] = [Number(at.lng.toFixed(6)), Number(at.lat.toFixed(6))]; replan(); });
      dot.addEventListener("click", e => e.stopPropagation());
      return m;
    }) : [];
  });
}
// Where on the way a day ends that ends at no hut: the point that far along it.
function pointAlong(metres) {
  if (!plan) return null;
  const C = plan.coords;
  let so = 0;
  for (let i = 1; i < C.length; i++) {
    const [x0, y0] = C[i - 1], [x1, y1] = C[i];
    const d = 6371000 * Math.hypot((x1 - x0) * Math.PI / 180 * Math.cos((y0 + y1) / 2 * Math.PI / 180), (y1 - y0) * Math.PI / 180);
    if (so + d >= metres) return [x0 + (x1 - x0) * (metres - so) / (d || 1), y0 + (y1 - y0) * (metres - so) / (d || 1)];
    so += d;
  }
  return C[C.length - 1];
}
