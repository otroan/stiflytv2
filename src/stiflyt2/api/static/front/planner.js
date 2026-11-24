// The route planner (the feature `planner`, ADR-005): click where a walk begins and where it ends, anywhere in
// Norway, and get a way on foot between them along DNT's routes as much as you like, with the climb, the time and
// days that end at huts. The tab is only there for one who has the feature; the server refuses the rest.
// What a stretch is, is told by its colour: on the map, and in the list of what the way adds up to, which is the legend.
const PLAN_CLASSES = { dnt: ["DNT's routes", "#c8161d"], marked: ["Other marked routes", "#e8741c"], path: ["Paths", "#8a5a2b"],
                       road: ["Roads", "#5f6b73"], ferry: ["Ferries", "#1f6fc5"], field: ["Across country", "#5b8a3c"], off: ["No path, straight", "#1f2a30"] };
const PLAN_KEY = "stiflyt2.plan";
// The points in order, the name of each that was found by its name (null for one clicked), and whether a lone
// point is where the walk ends (it was given as "To" before anything was given as "From"); and for each leg between
// two points, whether it is drawn straight over the ground, with no path.
// A point dragged onto another path (`snap`) is where the way passes on it, not a spot walked out to.
const planAsk = Object.assign({ points: [], names: [], snap: [], lone: null, straight: [], prefer: 0.6, mountains: 0, across: false, ferries: true, ice: false, alpine: false, hours: 7, auto: true },
  (() => { try { return JSON.parse(localStorage.getItem(PLAN_KEY)) || {}; } catch { return {}; } })());
if (planAsk.names.length !== planAsk.points.length) planAsk.names = planAsk.points.map(() => null);
if (planAsk.snap.length !== planAsk.points.length) planAsk.snap = planAsk.points.map(() => false);
function legsFit() {         // one for each leg, whatever was done to the points
  const S = planAsk.straight, n = Math.max(0, planAsk.points.length - 1);
  while (S.length < n) S.push(false);
  S.length = n;
}
legsFit();
let plan = null, planProblem = null, planBusy = false, planHeights = null, heightsProblem = null, heightsBusy = false;
let planAsked = 0, planMarkers = [], planDay = null;
let planDrag = null, planIgnoreClick = false;       // the line taken hold of, and the click that letting go of it makes
let planEstimate = null, planStale = false, planPhase = null, planEstimating = false;     // how long, whether from before, what is under way
const PLAN_AUTO_S = 3;          // a way thought to take no longer than this is found by itself, when the person lets it
const hasPlanner = () => (me.features || []).includes("planner");
const letter = i => String.fromCharCode(65 + i);
const hoursAndMinutes = m => `${Math.floor(m / 60)} h ${String(Math.round(m) % 60).padStart(2, "0")}`;

function keepAsk() { localStorage.setItem(PLAN_KEY, JSON.stringify(planAsk)); }
// A click on the map: the first is where the walk begins, the next where it ends, and each one after that goes on
// further from there, so that the end before becomes a place passed on the way.
function planAt(at) {
  if (planAsk.points.length >= 12) { planProblem = "A walk goes through 12 points at the most."; render(); return; }
  const p = [Number(at.lng.toFixed(6)), Number(at.lat.toFixed(6))];
  if (planAsk.lone === "end") { planAsk.points.unshift(p); planAsk.names.unshift(null); planAsk.snap.unshift(false); planAsk.straight.unshift(false); planAsk.lone = null; }      // where it begins
  else { planAsk.points.push(p); planAsk.names.push(null); planAsk.snap.push(false); planAsk.straight.push(false); }
  replan();
}
// A place found by its name, as where the walk begins or where it ends.
function planFrom(p, name) {
  const P = planAsk.points, N = planAsk.names;
  if (!P.length || planAsk.lone === "end") { P.unshift(p); N.unshift(name); planAsk.snap.unshift(false); planAsk.straight.unshift(false); planAsk.lone = null; }
  else { P[0] = p; N[0] = name; planAsk.snap[0] = false; }
  replan();
}
function planTo(p, name) {
  const P = planAsk.points, N = planAsk.names;
  if (!P.length) { P.push(p); N.push(name); planAsk.snap.push(false); planAsk.lone = "end"; }
  else if (P.length === 1 && planAsk.lone !== "end") { P.push(p); N.push(name); planAsk.snap.push(false); planAsk.straight.push(false); }
  else { P[P.length - 1] = p; N[N.length - 1] = name; planAsk.snap[P.length - 1] = false; }
  replan();
}
function planRemove(i) {
  const P = planAsk.points;
  if (P.length === 1) { planStartAgain(); return; }
  const S = planAsk.straight;
  if (i === 0) S.shift(); else if (i === P.length - 1) S.pop(); else S.splice(i - 1, 2, S[i - 1] && S[i]);    // two legs become one
  P.splice(i, 1); planAsk.names.splice(i, 1); planAsk.snap.splice(i, 1);
  if (P.length === 1 && i === 0) planAsk.lone = "end";        // what is left is where it ends
  replan();
}
function planStartAgain() { planAsk.points.length = 0; planAsk.names.length = 0; planAsk.snap.length = 0; planAsk.straight.length = 0; planAsk.lone = null; replan(); }
// The line dragged somewhere else: a point there, between the two the leg runs between, that the way passes on the
// nearest path or road. The leg becomes two, each drawn as the one was.
function planInsert(i, p) {
  const S = planAsk.straight;
  planAsk.points.splice(i, 0, p); planAsk.names.splice(i, 0, null); planAsk.snap.splice(i, 0, true);
  S.splice(i - 1, 1, S[i - 1] || false, S[i - 1] || false);
  replan();
}
function planStraight(leg, straight) { planAsk.straight[leg] = straight; replan(); }
// What is typed in the From and To fields, and what was found for it.
const planSeek = { from: { text: null, found: [] }, to: { text: null, found: [] } };
const typeOf = t => (t || "").replace(/([A-ZÆØÅ])/g, " $1").toLowerCase();
function placeField(which, label, point, name, coords) {
  const seek = planSeek[which];
  const found = el("ul", { class: "plan-found", role: "listbox", "data-plan-found": which });
  const choose = r => { seek.text = null; seek.found = []; (which === "from" ? planFrom : planTo)([r.lon, r.lat], r.name); };
  // Into the list that is shown: the side may have been drawn anew while the answer was on its way.
  const list = () => (document.querySelector(`[data-plan-found="${which}"]`) || found).replaceChildren(...seek.found.map(r => el("li", {}, el("button", { type: "button", class: "link", onclick: () => choose(r) },
    r.name, el("span", { class: "sub" }, ` ${[typeOf(r.type), r.municipality].filter(Boolean).join(", ")}`)))));
  let timer = null;
  const input = el("input", { type: "search", "data-plan-field": which, value: seek.text ?? name ?? "", "aria-label": label,
    placeholder: point ? `Clicked at ${coords}` : which === "from" ? "Search for a place, or click the map" : "Search for a place",
    oninput: e => {
      seek.text = e.target.value; clearTimeout(timer);
      const q = e.target.value.trim();
      if (q.length < 2) { seek.found = []; list(); return; }
      timer = setTimeout(async () => {
        const c = map.getCenter();
        try { const got = await get("placenames", { q, lon: c.lng.toFixed(5), lat: c.lat.toFixed(5), limit: 8 }); if ((seek.text || "").trim() === q) { seek.found = got; list(); } } catch {}
      }, 250);
    },
    onkeydown: e => {
      if (e.key === "Enter" && seek.found.length) { e.preventDefault(); choose(seek.found[0]); }
      else if (e.key === "Escape") { e.stopPropagation(); seek.text = null; seek.found = []; e.target.value = name || ""; list(); }
    } });
  found.replaceChildren();
  const field = el("div", { class: "plan-field" },
    el("label", {}, el("span", { class: "plan-letter" + (point ? "" : " none") }, point ? point : ""), el("span", { class: "plan-label" }, label), input,
      point ? el("button", { class: "link", type: "button", title: `Take ${label.toLowerCase()} away`, "aria-label": `Take ${label.toLowerCase()} away`,
        onclick: () => planRemove(which === "from" ? 0 : planAsk.points.length - 1) }, "✕") : ""),
    found);
  setTimeout(list);         // once the field is in the page
  return field;
}
// What is asked of the server: the points and how to go.
const planBody = () => ({ points: planAsk.points, straight: planAsk.straight, snap: planAsk.snap, prefer: planAsk.prefer, mountains: planAsk.mountains,
                          across: planAsk.across, ferries: planAsk.ferries, ice: planAsk.ice, alpine: planAsk.alpine });
// Something was changed: how long the way would take to find is asked for at once (a millisecond), and the way itself
// only when that is quick (and it is to be found by itself), or when "Find the way" is pressed. Until then the way
// found before stays, faded, and says it is from before.
function replan() {
  legsFit();
  keepAsk();
  const asked = ++planAsked;            // what is on its way back from before is for what was asked before
  planProblem = heightsProblem = null; planDay = null; planBusy = heightsBusy = false; planEstimate = null;
  if (planAsk.points.length < 2) { plan = planHeights = null; planStale = false; render(); return; }
  planStale = Boolean(plan);
  render();
  askEstimate(asked);
}
let planTimer = null;
function replanSoon() { clearTimeout(planTimer); planTimer = setTimeout(replan, 400); }
async function askEstimate(asked) {
  try {
    const got = await send("POST", "planner/estimate", planBody());
    if (asked !== planAsked) return;
    planEstimate = got;
  } catch { if (asked !== planAsked) return; planEstimate = { seconds: null, heights_seconds: null }; }
  if (planAsk.auto && planEstimate.seconds !== null && planEstimate.seconds <= PLAN_AUTO_S) askPlan(asked);
  else render();
}
async function askPlan(asked = ++planAsked) {
  clearTimeout(planTimer);          // a slider's change still waiting is in what is asked now
  planBusy = true; planProblem = null;
  planPhase = { finding: "way", began: Date.now(), about: planEstimate && planEstimate.seconds };
  render(); planTick();
  try {
    const got = await send("POST", "planner/plan", planBody());
    if (asked !== planAsked) return;
    plan = got; planStale = false; planHeights = null; heightsProblem = null;
    // A point moved onto the path it is passed on: its letter goes there too.
    for (const [i, at] of Object.entries(got.snapped || {})) planAsk.points[Number(i)] = at;
    keepAsk();
  } catch (err) { if (asked === planAsked) planProblem = err.message; }
  if (asked !== planAsked) return;
  planBusy = false; render();
  if (plan && !planProblem) askHeights(asked);
}
async function askHeights(asked) {
  heightsBusy = true;
  planPhase = { finding: "heights", began: Date.now(), about: planEstimate && planEstimate.heights_seconds };
  render(); planTick();
  try {
    const parts = plan.steps.map(s => ({ coords: plan.coords.slice(s.at[0], s.at[1] + 1), ferry: s.class === "ferry", rough: s.class === "off" || s.class === "field", ice: s.ice }));
    const got = await send("POST", "planner/heights", { parts, hours: planAsk.hours });
    if (asked !== planAsked) return;
    planHeights = got;
  } catch (err) { if (asked === planAsked) heightsProblem = err.message; }
  if (asked !== planAsked) return;
  heightsBusy = false; render();
}
// How far it has come, as the time it was thought to take goes: a bar that fills, and stops short until it is done.
const aboutTime = t => t >= 60 ? `${Math.floor(t / 60)} min ${String(Math.round(t % 60)).padStart(2, "0")} s` : t >= 10 ? `${Math.round(t)} s` : `${t.toFixed(1)} s`;
function planProgress() {
  const P = planPhase, gone = (Date.now() - P.began) / 1000;
  const what = P.finding === "way" ? "Finding the way" : "Reading how high it goes";
  if (!P.about) return [null, `${what}… ${aboutTime(gone)}`];
  const share = Math.min(0.95, gone / Math.max(P.about, 0.1));
  return [share, gone > P.about * 1.2 ? `${what}… ${aboutTime(gone)}: longer than the ${aboutTime(P.about)} it was thought to take`
    : `${what}… ${Math.round(share * 100)} %, about ${aboutTime(Math.max(0, P.about - gone))} left`];
}
function progressBar() {
  const [share, says] = planProgress();
  return el("div", { class: "plan-progress", role: "status" },
    el("div", { class: "plan-bar" + (share === null ? " unknown" : "") }, el("span", { style: `width:${share === null ? 100 : Math.round(share * 100)}%` })),
    el("div", { class: "sub" }, says));
}
let planTicker = null;
function planTick() {
  clearInterval(planTicker);
  planTicker = setInterval(() => {
    if (!planBusy && !heightsBusy) { clearInterval(planTicker); return; }
    const bar = document.querySelector("#panel .plan-bar span"), said = document.querySelector("#panel .plan-progress .sub");
    if (!bar || !said || !planPhase) return;
    const [share, says] = planProgress();
    if (share !== null) bar.style.width = `${Math.round(share * 100)}%`;
    said.textContent = says;
  }, 200);
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
  const N = planAsk.names;
  if (N.length > 1 && N[0] && N[N.length - 1]) return `${N[0]} – ${N[N.length - 1]}`;
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
  if (H.days.length <= 30) for (const d of H.days.slice(0, -1)) add("line", { x1: X(d.at_m), x2: X(d.at_m), y1: pad, y2: Ht - pad, stroke: "#c8161d", "stroke-dasharray": "3 3" });
  add("text", { x: pad + 2, y: 12, "font-size": 10, fill: "#5f6b73" }).textContent = `${Math.round(hi)} m`;
  add("text", { x: pad + 2, y: Ht - 6, "font-size": 10, fill: "#5f6b73" }).textContent = `${Math.round(lo)} m`;
  return svg;
}
function renderPlanner() {
  const P = planAsk.points;
  // Opened with a walk from before (kept in the browser): how long its way takes is asked for, and it is found by
  // itself if that is quick.
  if (P.length >= 2 && !plan && !planBusy && !planProblem && !planEstimate && !planEstimating && hasPlanner()) {
    planEstimating = true;
    askEstimate(planAsked).finally(() => { planEstimating = false; });
  }
  const at = p => `${p[1].toFixed(4)} N ${p[0].toFixed(4)} E`, N = planAsk.names;
  const lone = planAsk.lone === "end", last = P.length - 1;
  const start = P.length && !lone ? 0 : null, end = P.length > 1 ? last : lone ? 0 : null;
  // Where it begins and where it ends, each found by its name or clicked on the map; the places passed between.
  const ends = el("div", { class: "plan-ends" },
    placeField("from", "From", start === null ? null : letter(0), start === null ? null : N[0], start === null ? "" : at(P[0])),
    ...P.slice(1, -1).map((p, k) => el("div", { class: "plan-field" }, el("label", {},
      el("span", { class: "plan-letter" }, letter(k + 1)), el("span", { class: "plan-label" }, "Via"),
      el("span", { class: "grow" }, N[k + 1] || el("span", { class: "sub" }, at(p))),
      el("button", { class: "link", type: "button", title: `Take point ${letter(k + 1)} away`, "aria-label": `Take point ${letter(k + 1)} away`, onclick: () => planRemove(k + 1) }, "✕")))),
    placeField("to", "To", end === null ? null : lone ? "B" : letter(last), end === null ? null : N[end], end === null ? "" : at(P[end])));
  const slider = el("input", { type: "range", min: 0, max: 100, step: 5, value: Math.round(planAsk.prefer * 100), "aria-label": "How much DNT's routes are preferred",
    oninput: e => { planAsk.prefer = Number(e.target.value) / 100; replanSoon(); } });
  const heights = el("input", { type: "range", min: -100, max: 100, step: 10, value: Math.round(planAsk.mountains * 100), "aria-label": "Whether it keeps to the valleys or up in the mountains",
    oninput: e => { planAsk.mountains = Number(e.target.value) / 100; replanSoon(); } });
  const form = el("div", { class: "trip-ask" },
    el("label", { class: "plan-prefer" }, el("span", {}, "Shortest"), slider, el("span", {}, "Along DNT's routes")),
    el("label", { class: "plan-prefer" }, el("span", {}, "Valleys"), heights, el("span", {}, "Mountains")),
    ...[["across", "Across country where there is no path (a sketch)"], ["ferries", "Ferries may be taken"], ["ice", "Over glaciers too"], ["alpine", "Demanding alpine paths and via ferratas too"]].map(([key, says]) =>
      el("label", {}, el("input", { type: "checkbox", checked: planAsk[key] ? "" : false, onchange: e => { planAsk[key] = e.target.checked; replan(); } }), ` ${says}`)),
    el("label", {}, "A day is ", el("input", { type: "number", min: 3, max: 14, step: 0.5, value: planAsk.hours, style: "width:5em",
      onchange: e => { planAsk.hours = Math.min(14, Math.max(3, Number(e.target.value) || 7)); keepAsk(); if (plan) { planHeights = null; askHeights(planAsked); } } }), " hours of walking, with no rests"));
  const told = [];
  // Find the way: by itself when it is quick, else when asked; with how long it is thought to take.
  const about = planEstimate && planEstimate.seconds !== null && planEstimate.seconds !== undefined ? ` (about ${aboutTime(planEstimate.seconds)})` : "";
  told.push(el("div", { class: "actions plan-find" },
    el("button", { class: "btn main", type: "button", disabled: P.length < 2 || planBusy ? "" : false, onclick: () => askPlan() },
      planBusy ? "Finding the way…" : `${plan && !planStale ? "Find it again" : "Find the way"}${about}`),
    el("label", { class: "plan-auto" }, el("input", { type: "checkbox", checked: planAsk.auto ? "" : false, onchange: e => { planAsk.auto = e.target.checked; keepAsk(); } }),
      ` By itself when it takes under ${PLAN_AUTO_S} s`)));
  if (planBusy || heightsBusy) told.push(progressBar());
  if (plan && planStale && !planBusy) told.push(el("div", { class: "sub stale" }, "Below is the way as it was before what you changed: find it again for the new one."));
  if (planProblem) told.push(el("div", { class: "error", role: "alert" }, `No way was found: ${planProblem}`));
  if (plan && !planBusy) {
    const T = plan.total, H = planHeights;
    told.push(el("h2", { class: "next" }, `${km(T.metres / 1000)}` + (H ? `, ${H.days.length} ${H.days.length === 1 ? "day" : "days"}` : "")),
      el("div", { class: "sub" }, H ? `${hoursAndMinutes(H.minutes)} of walking by Naismith's rule, with no rests; ${H.up_m.toLocaleString("en")} m up and ${H.down_m.toLocaleString("en")} m down, `
          + `from ${H.lowest_m} to ${H.highest_m} m above the sea.` + (H.rough_m >= 100 ? ` ${km(H.rough_m / 1000)} of it with no path, timed half as slow again.` : "")
          + (H.ferries.length ? ` Ferries besides: about ${hoursAndMinutes(H.ferries.reduce((t, f) => t + f.minutes, 0))} on board.` : "")
        : heightsBusy ? "" : heightsProblem ? `The heights could not be had: ${heightsProblem}` : ""),
      H ? profileOf(H) : "",
      // A few metres from a click to the nearest way are said below, not as a kind of their own.
      el("ul", { class: "plan-by" }, ...Object.entries(PLAN_CLASSES).filter(([k]) => T.by[k] >= (k === "off" ? 100 : 1)).map(([k, [says, colour]]) =>
        el("li", {}, el("span", { class: "plan-swatch", style: `background:${colour}` }), `${says}: ${km(T.by[k] / 1000)}`))),
      ...plan.off.map(o => el("div", { class: "sub" }, `${letter(o.point)} is ${o.off_m.toLocaleString("en")} m from where the way passes: that bit is drawn straight.`)),
      // Each leg: along paths, or straight over the ground where nothing is mapped. One with no way found is drawn
      // straight and says why; one that goes far round says so, for it to be drawn straight if that is better.
      el("ol", { class: "plan-legs" }, ...plan.legs.map(l => el("li", { class: l.unfound ? "unfound" : l.roundabout ? "roundabout" : "" },
        el("div", { class: "grow" },
          el("div", {}, `${letter(l.from)} → ${letter(l.to)}: ${km(l.metres / 1000)} `, el("span", { class: "sub" },
            l.straight ? "straight, with no path" : l.unfound ? "drawn straight" : "along paths and roads")),
          l.unfound ? el("div", { class: "sub" }, `No way on foot was found: ${l.unfound}. It is drawn straight over whatever lies there: add points to go round, or allow ferries or glaciers.`) : "",
          l.roundabout ? el("div", { class: "sub" }, `${km(l.metres / 1000)} for ${km(l.crow_m / 1000)} as the crow flies: if the ground allows, it may be better straight.`) : ""),
        el("button", { class: "link", type: "button", onclick: () => planStraight(l.from, !l.straight) },
          l.straight ? "Along paths" : l.unfound ? "Keep it straight" : "Straight instead")))));
    if (H && H.days.length > 1) told.push(el("ol", { class: "trip-days" }, ...H.days.map(d => el("li", { class: planDay === d.day ? "hit current" : "hit" },
      el("span", { class: "swatch", style: "background:#1f2a30" }, String(d.day)),
      el("div", { class: "grow" },
        el("div", {}, d.last ? "To the end" : d.hut ? `To ${d.hut.name || "a hut"}` : "To where the hours run out"),
        el("div", { class: "sub" }, `${km(d.metres / 1000)}, ${hoursAndMinutes(d.minutes)}`
          + (d.hut ? (d.hut.operator ? `, ${d.hut.operator}` : "") : d.last ? "" : ": no hut near the way, a night in a tent"))),
      ))));
    told.push(el("details", { class: "how" }, el("summary", {}, `The way, in ${plan.steps.length} ${plan.steps.length === 1 ? "step" : "steps"}`),
        el("ol", { class: "trip-steps" }, ...plan.steps.map(s => el("li", {}, el("span", { class: "plan-swatch", style: `background:${(PLAN_CLASSES[s.class] || PLAN_CLASSES.path)[1]}` }),
          `${s.says}, ${km(s.metres / 1000)}`)))),
      el("div", { class: "actions" }, el("button", { class: "btn", onclick: planGpx }, "As a GPX file"), el("button", { class: "btn", onclick: showPlan }, "Show all of it")),
      el("div", { class: "sub" }, `Whether a hut is open, a bridge is up or a ferry runs is not known here. ${[...plan.credits, ...(H ? H.credits : [])].join(". ")}.`));
  }
  // A field being typed in stays so when the side is drawn anew (the way, or its heights, came meanwhile).
  const typing = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.planField;
  const caret = typing ? document.activeElement.selectionStart : 0;
  panel.replaceChildren(
    el("h2", { class: "area" }, "Plan a walk"),
    el("div", { class: "sub", style: "margin-bottom:8px" }, P.length > 1 ? "Click the map to go on further from the last point. Drag a point to move it, or the line to another path to go that way."
      : "Search for where the walk begins and ends, anywhere in Norway, or click the map. Click again to go on further; drag a point to move it."),
    ends,
    P.length ? el("div", { class: "actions" },
      P.length > 1 ? el("button", { class: "btn", onclick: () => { P.reverse(); planAsk.names.reverse(); planAsk.snap.reverse(); planAsk.straight.reverse(); replan(); } }, "The other way") : "",
      el("button", { class: "btn", onclick: planStartAgain }, "Start again")) : "",
    form, ...told);
  if (typing) { const field = panel.querySelector(`[data-plan-field="${typing}"]`); if (field) { field.focus(); field.setSelectionRange(caret, caret); } }
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
      // The line can be taken hold of and dragged to another path: where it is let go is a point it passes.
      for (const id of ["plan", "plan-dashed"]) map.on("mousedown", id, e => {
        if (e.originalEvent.button !== 0 || !plan || planBusy) return;
        e.preventDefault();
        planDrag = { leg: e.features[0].properties.leg, from: e.point, moved: false, ghost: null };
        planIgnoreClick = true;
      });
      map.on("mousemove", e => {
        if (!planDrag) return;
        if (!planDrag.moved && Math.hypot(e.point.x - planDrag.from.x, e.point.y - planDrag.from.y) < 4) return;
        planDrag.moved = true;
        if (!planDrag.ghost) planDrag.ghost = new maplibregl.Marker({ element: el("div", { class: "plan-marker ghost" }, "+") }).setLngLat(e.lngLat).addTo(map);
        else planDrag.ghost.setLngLat(e.lngLat);
      });
      map.on("mouseup", e => {
        if (!planDrag) return;
        const d = planDrag;
        planDrag = null;
        if (d.ghost) d.ghost.remove();
        setTimeout(() => { planIgnoreClick = false; });       // after the click the browser makes of it, if it makes one
        if (d.moved && d.leg >= 0) planInsert(d.leg + 1, [Number(e.lngLat.lng.toFixed(6)), Number(e.lngLat.lat.toFixed(6))]);
      });
      planLayers = true;
    }
    const steps = on && plan ? plan.steps : [];
    const legOf = s => plan.legs.findIndex(l => s.at[0] >= l.at[0] && s.at[0] < Math.max(l.at[1], l.at[0] + 1));
    map.getSource("plan").setData({ type: "FeatureCollection", features: steps.map(s => ({ type: "Feature",
      properties: { colour: s.ice ? GLACIER : (PLAN_CLASSES[s.class] || PLAN_CLASSES.path)[1], dashed: s.class === "ferry" || s.class === "off" || s.class === "field" || s.ice, leg: legOf(s) },
      geometry: { type: "LineString", coordinates: plan.coords.slice(s.at[0], s.at[1] + 1) } })) });
    const days = on && plan && planHeights ? planHeights.days.filter(d => !d.last) : [];
    map.getSource("plan-days").setData({ type: "FeatureCollection", features: days.map(d => ({ type: "Feature", properties: { day: String(d.day) },
      geometry: { type: "Point", coordinates: d.hut ? [d.hut.lon, d.hut.lat] : pointAlong(d.at_m) } })).filter(f => f.geometry.coordinates) });
    if (on && plan) for (const id of ["routes", "routes-ice"]) if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", 0.3);
    for (const id of ["plan", "plan-dashed"]) map.setPaintProperty(id, "line-opacity", planStale ? 0.4 : 1);
    // The points: markers that can be dragged, made anew when they are not the ones shown.
    for (const m of planMarkers) m.remove();
    planMarkers = on ? planAsk.points.map((p, i) => {
      const mark = planAsk.lone === "end" ? "B" : letter(i);
      const dot = el("div", { class: "plan-marker", title: `${planAsk.names[i] ? `${planAsk.names[i]}. ` : ""}${mark}: drag to move` }, mark);
      const m = new maplibregl.Marker({ element: dot, draggable: true }).setLngLat(p).addTo(map);
      m.on("dragend", () => {
        const at = m.getLngLat(), last = planAsk.points.length - 1;
        planAsk.points[i] = [Number(at.lng.toFixed(6)), Number(at.lat.toFixed(6))]; planAsk.names[i] = null;
        planAsk.snap[i] = i > 0 && i < last;          // a point passed on the way goes onto the path it is dropped by
        replan();
      });
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
