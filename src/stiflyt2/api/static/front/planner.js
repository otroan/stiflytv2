// The route planner (the feature `planner`, ADR-005): click where a walk begins and where it ends, anywhere in
// Norway, and get a way on foot between them along DNT's routes as much as you like, with the climb, the time and
// days that end at huts. The tab is only there for one who has the feature; the server refuses the rest.
// What a stretch is, is told by its colour: on the map, and in the list of what the way adds up to, which is the legend.
const PLAN_CLASSES = { dnt: ["DNT's routes", "#c8161d"], marked: ["Other marked routes", "#e8741c"], path: ["Paths", "#8a5a2b"],
                       road: ["Roads", "#5f6b73"], ferry: ["Ferries", "#1f6fc5"], field: ["Across country", "#5b8a3c"], off: ["No path, straight", "#1f2a30"] };
const PLAN_KEY = "stiflyt2.plan";
// The points in order, the name of each that was found by its name (null for one clicked), and whether a lone
// point is where the walk ends (it was given as "To" before anything was given as "From"); and for each leg between
// two points, how it goes: along paths and roads, across country alone (even where there are paths), or straight.
// A point dragged onto another path (`snap`) is where the way passes on it, not a spot walked out to. A point that is
// `hidden` is not one of yours: it holds the way where it was on either side of where the line was dragged, so that
// only the part between is found anew. It has no letter, and goes when the point it was made for goes.
const planAsk = Object.assign({ points: [], names: [], snap: [], hidden: [], lone: null, modes: [], prefer: 0.6, mountains: 0, across: false, wade: false, fords: true, ferries: true, ice: false, alpine: false, hours: 7, auto: true },
  (() => { try { return JSON.parse(localStorage.getItem(PLAN_KEY)) || {}; } catch { return {}; } })());
function pointsFit() {       // a name, a snap and a hidden for each point, whatever was done to them
  for (const k of ["names", "snap", "hidden"]) {
    const A = planAsk[k] || (planAsk[k] = []);
    while (A.length < planAsk.points.length) A.push(k === "names" ? null : false);
    A.length = planAsk.points.length;
  }
}
pointsFit();
const planVisible = () => planAsk.points.map((_, i) => i).filter(i => !planAsk.hidden[i]);
const letterOf = i => { const v = planVisible().indexOf(i); return v < 0 ? "" : planAsk.lone === "end" ? "B" : letter(v); };
// A walk kept from before each leg was straight or not: it is so still.
if (!planAsk.modes.length && Array.isArray(planAsk.straight)) planAsk.modes = planAsk.straight.map(s => s ? "straight" : "paths");
delete planAsk.straight;
function legsFit() {         // one for each leg, whatever was done to the points
  const S = planAsk.modes, n = Math.max(0, planAsk.points.length - 1);
  while (S.length < n) S.push("paths");
  S.length = n;
}
legsFit();
let plan = null, planProblem = null, planBusy = false, planHeights = null, heightsProblem = null, heightsBusy = false;
let planAsked = 0, planMarkers = [], planDay = null;
let planDrag = null, planIgnoreClick = false;       // the line taken hold of, and the click that letting go of it makes
let planEstimate = null, planStale = false, planPhase = null, planEstimating = false;     // how long, whether from before, what is under way
const PLAN_AUTO_S = 3;          // a way thought to take no longer than this is found by itself, when the person lets it
const PLAN_SNAP_M = 150;        // the line let go of this near a path or road goes along it; further off, exactly there
const PLAN_MOST = 100;
// The legs found so far, by what they were found for: a leg asked for again just as it was is sent back to be kept,
// not looked for again. So a change to one part of the way finds that part anew and leaves the rest as it is.
let planLegCache = new Map();
const hasPlanner = () => (me.features || []).includes("planner");
const letter = i => String.fromCharCode(65 + i);
const hoursAndMinutes = m => `${Math.floor(m / 60)} h ${String(Math.round(m) % 60).padStart(2, "0")}`;

function keepAsk() { localStorage.setItem(PLAN_KEY, JSON.stringify(planAsk)); }
// A click on the map: the first is where the walk begins, the next where it ends, and each one after that goes on
// further from there, so that the end before becomes a place passed on the way.
function planAt(at) {
  if (planAsk.points.length >= PLAN_MOST) { planProblem = `A walk goes through ${PLAN_MOST} points at the most.`; render(); return; }
  const p = [Number(at.lng.toFixed(6)), Number(at.lat.toFixed(6))];
  if (planAsk.lone === "end") { planAsk.points.unshift(p); planAsk.names.unshift(null); planAsk.snap.unshift(false); planAsk.hidden.unshift(false); planAsk.modes.unshift("paths"); planAsk.lone = null; }      // where it begins
  else { planAsk.points.push(p); planAsk.names.push(null); planAsk.snap.push(false); planAsk.hidden.push(false); planAsk.modes.push("paths"); }
  replan();
}
// A place found by its name, as where the walk begins or where it ends.
function planFrom(p, name) {
  const P = planAsk.points, N = planAsk.names;
  if (!P.length || planAsk.lone === "end") { P.unshift(p); N.unshift(name); planAsk.snap.unshift(false); planAsk.hidden.unshift(false); planAsk.modes.unshift("paths"); planAsk.lone = null; }
  else { P[0] = p; N[0] = name; planAsk.snap[0] = false; }
  replan();
}
function planTo(p, name) {
  const P = planAsk.points, N = planAsk.names;
  if (!P.length) { P.push(p); N.push(name); planAsk.snap.push(false); planAsk.hidden.push(false); planAsk.lone = "end"; }
  else if (P.length === 1 && planAsk.lone !== "end") { P.push(p); N.push(name); planAsk.snap.push(false); planAsk.hidden.push(false); planAsk.modes.push("paths"); }
  else { P[P.length - 1] = p; N[N.length - 1] = name; planAsk.snap[P.length - 1] = false; }
  replan();
}
// A point of yours taken away, and the hidden ones that held the way on either side of it: the legs about it become one.
function planRemove(i) {
  const P = planAsk.points, H = planAsk.hidden, S = planAsk.modes;
  if (planVisible().length === 1) { planStartAgain(); return; }
  let lo = i, hi = i;
  while (lo > 0 && H[lo - 1]) lo--;
  while (hi < P.length - 1 && H[hi + 1]) hi++;
  if (lo === 0) S.splice(0, hi + 1);
  else if (hi === P.length - 1) S.splice(lo - 1, hi - lo + 1);
  else { const about = S.slice(lo - 1, hi + 1); S.splice(lo - 1, hi - lo + 2, about.every(m => m === about[0]) ? about[0] : "paths"); }   // the legs about it become one
  for (const k of ["points", "names", "snap", "hidden"]) planAsk[k].splice(lo, hi - lo + 1);
  if (P.length === 1 && lo === 0) planAsk.lone = "end";        // what is left is where it ends
  replan();
}
function planStartAgain() { for (const k of ["points", "names", "snap", "hidden", "modes"]) planAsk[k].length = 0; planAsk.lone = null; replan(); }
// Between two points of yours, along paths, across country or straight: as one leg, the hidden points between taken away.
function planSegmentMode(a, b, mode) {
  planAsk.modes.splice(a, b - a, mode);
  for (const k of ["points", "names", "snap", "hidden"]) planAsk[k].splice(a + 1, b - a - 1);
  replan();
}
const round6 = v => Number(v.toFixed(6));
function metresOf(C) {
  let m = 0;
  for (let k = 1; k < C.length; k++) {
    const [x0, y0] = C[k - 1], [x1, y1] = C[k];
    m += 6371000 * Math.hypot((x1 - x0) * Math.PI / 180 * Math.cos((y0 + y1) / 2 * Math.PI / 180), (y1 - y0) * Math.PI / 180);
  }
  return m;
}
// What leg k is found for: its two points, how it is to be drawn, and how to go. The same again is the same leg.
const legKey = k => JSON.stringify([planAsk.points[k], planAsk.points[k + 1], planAsk.modes[k], planAsk.snap[k], planAsk.snap[k + 1],
  planAsk.prefer, planAsk.mountains, planAsk.across, planAsk.wade, planAsk.fords, planAsk.ferries, planAsk.ice, planAsk.alpine]);
// The part of the way found from one index of its line to another, as a leg to be kept: its line, its steps (cut where
// it is cut), the junctions along it, and what it is from.
function keptOf(a, b, L) {
  const coords = plan.coords.slice(a, b + 1);
  const steps = plan.steps.filter(st => st.at[1] > a && st.at[0] < b).map(st => {
    const lo = Math.max(st.at[0], a), hi = Math.min(st.at[1], b);
    return { class: st.class, name: st.name, ice: st.ice, at: [lo - a, hi - a],
             metres: lo === st.at[0] && hi === st.at[1] ? st.metres : metresOf(plan.coords.slice(lo, hi + 1)) };
  });
  return { coords, steps, joints: plan.joints.filter(j => j >= a && j <= b).map(j => j - a), origins: L.origins || [],
           crossings: (plan.crossings || []).filter(c => c.at >= a && c.at <= b).map(c => ({ ...c, at: c.at - a })),
           unfound: a === L.at[0] && b === L.at[1] ? L.unfound : null, roundabout: a === L.at[0] && b === L.at[1] && L.roundabout };
}
// The line taken hold of at index i of its line (on leg `leg`) and let go of at `at`. Only the part between the
// junctions on either side of where it was taken is found anew, and no more than PLAN_AROUND_M to either side:
// the way is held there by hidden points. Let go of near a path or road, the way goes along it; on open ground, or
// where the line went across country, the point stays exactly where it was let go of and the way goes to it across
// country, over the ground (straight, where the line was straight).
const PLAN_AROUND_M = 1500;
const PLAN_MODES = { paths: ["Along paths", "along paths and roads"], across: ["Across country", "across country, over the ground"],
                     straight: ["Straight", "straight, with no path"] };
async function planDragTo(i, leg, at) {
  const L = plan.legs[leg], J = plan.joints, C = plan.coords;
  if (!L) return;
  // How far along the leg each index is, to hold the way no further than PLAN_AROUND_M off.
  const along = [0];
  for (let k = L.at[0] + 1; k <= L.at[1]; k++) along.push(along[along.length - 1] + metresOf([C[k - 1], C[k]]));
  const from = k => along[k - L.at[0]], here = from(i);
  let j0 = Math.max(L.at[0], ...J.filter(j => j <= i && j >= L.at[0]));
  let j1 = Math.min(L.at[1], ...J.filter(j => j > i && j <= L.at[1]));
  while (j0 < i && here - from(j0) > PLAN_AROUND_M) j0++;
  while (j1 > i + 1 && from(j1) - from(i + 1) > PLAN_AROUND_M) j1--;
  const step = plan.steps.find(st => st.at[0] <= i && i < Math.max(st.at[1], st.at[0] + 1));
  const p = [round6(at.lng), round6(at.lat)];
  let mode = !step ? "paths" : step.class === "off" ? "straight" : step.class === "field" ? "across" : "paths";
  if (mode === "paths") {
    try { const n = await get("planner/near", { lon: p[0], lat: p[1] }); if (n.off_m === null || n.off_m > PLAN_SNAP_M) mode = "across"; } catch { /* along paths, then */ }
  }
  const was = planAsk.modes[leg], add = [], legs = [];
  const before = j0 > L.at[0] ? keptOf(L.at[0], j0, L) : null, after = j1 < L.at[1] ? keptOf(j1, L.at[1], L) : null;
  if (before) { add.push({ p: C[j0], hidden: true, snap: true }); legs.push({ mode: was, kept: before }); }
  add.push({ p, hidden: false, snap: mode === "paths" }); legs.push({ mode, kept: null });
  if (after) { add.push({ p: C[j1], hidden: true, snap: true }); legs.push({ mode, kept: null }); }
  legs.push({ mode: after ? was : mode, kept: after });
  planAsk.points.splice(leg + 1, 0, ...add.map(x => x.p)); planAsk.names.splice(leg + 1, 0, ...add.map(() => null));
  planAsk.snap.splice(leg + 1, 0, ...add.map(x => x.snap)); planAsk.hidden.splice(leg + 1, 0, ...add.map(x => x.hidden));
  planAsk.modes.splice(leg, 1, ...legs.map(x => x.mode));
  legs.forEach((x, k) => { if (x.kept) planLegCache.set(legKey(leg + k), x.kept); });
  replan();
}
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
const planBody = () => ({ points: planAsk.points, modes: planAsk.modes, snap: planAsk.snap, prefer: planAsk.prefer, mountains: planAsk.mountains,
                          across: planAsk.across, wade: planAsk.wade, fords: planAsk.fords, ferries: planAsk.ferries, ice: planAsk.ice, alpine: planAsk.alpine,
                          kept: planAsk.modes.map((_, k) => planLegCache.get(legKey(k)) || null) });
// Something was changed: how long the way would take to find is asked for at once (a millisecond), and the way itself
// only when that is quick (and it is to be found by itself), or when "Find the way" is pressed. Until then the way
// found before stays, faded, and says it is from before.
function replan() {
  pointsFit();
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
    // A point of yours moved onto the path it is passed on: its letter goes there too. (A hidden one stays where it
    // was put, on the way as it was: so the legs it holds are the same legs.)
    for (const [i, at] of Object.entries(got.snapped || {})) if (!planAsk.hidden[Number(i)]) planAsk.points[Number(i)] = at;
    planLegCache = new Map(got.legs.map((L, k) => [legKey(k), keptOf(L.at[0], L.at[1], L)]));
    keepAsk();
    fitWhenNew();
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
// When the way is found from somewhere else or to somewhere else than before (or first), the map goes to it, unless
// it is all in sight already. Not when a point between is moved or the line dragged: one is at work where one is.
let planFitFor = null;
function fitWhenNew() {
  const P = planAsk.points, ends = JSON.stringify([P[0], P[P.length - 1]]);
  if (ends === planFitFor || !plan || !plan.coords.length) return;
  planFitFor = ends;
  const b = map.getBounds(), C = plan.coords;
  if (C.every(([x, y]) => b.contains([x, y]))) return;
  showPlan();
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
// Where the way crosses a river, and how: by a bridge, by a ford on a path, or wading across country; at how far.
const CROSSING = { bridge: "Bridge over", ford: "Ford over", wade: "Wades" };
function planCrossings() {
  const C = plan.crossings || [];
  if (!C.length) return "";
  const along = [0];
  for (let k = 1; k < plan.coords.length; k++) along.push(along[k - 1] + metresOf([plan.coords[k - 1], plan.coords[k]]));
  return el("div", { class: "plan-crossings" }, el("strong", {}, "Over rivers: "),
    ...C.map((c, k) => el("span", { class: `crossing ${c.how}` }, `${k ? "; " : ""}${CROSSING[c.how]} ${c.river || (c.how === "bridge" ? "a river" : "a stream")}`,
      el("span", { class: "sub" }, ` at ${km(along[c.at] / 1000)}`))),
    !planAsk.fords && C.some(c => c.how === "ford") ? el("div", { class: "sub" }, "A ford here could not be gone round: there is no bridge within some 10 km.") : "",
    C.some(c => c.how !== "bridge") ? el("div", { class: "sub" }, "Whether a ford or a river can be waded today, and whether a summer bridge is out, is not known here.") : "");
}
// The way between each two points of yours: its legs together (a dragged line cuts one into several).
function planSegments() {
  const V = planVisible(), P = planAsk.points;
  return V.slice(0, -1).map((a, n) => {
    const b = V[n + 1], legs = plan.legs.slice(a, b), metres = legs.reduce((m, l) => m + l.metres, 0), crow = metresOf([P[a], P[b]]);
    return { a, b, metres, crow_m: crow, mode: legs.length === 1 ? legs[0].mode || (legs[0].straight ? "straight" : "paths") : null, manual: legs.length > 1,
             unfound: (legs.find(l => l.unfound) || {}).unfound || null,
             roundabout: legs.length === 1 && legs[0].roundabout };
  });
}
function renderPlanner() {
  const P = planAsk.points;
  // Opened with a walk from before (kept in the browser): how long its way takes is asked for, and it is found by
  // itself if that is quick.
  if (P.length >= 2 && !plan && !planBusy && !planProblem && !planEstimate && !planEstimating && hasPlanner()) {
    planEstimating = true;
    askEstimate(planAsked).finally(() => { planEstimating = false; });
  }
  const at = p => `${p[1].toFixed(4)} N ${p[0].toFixed(4)} E`, N = planAsk.names, V = planVisible();
  const lone = planAsk.lone === "end", last = P.length - 1;
  const start = P.length && !lone ? 0 : null, end = P.length > 1 ? last : lone ? 0 : null;
  // Where it begins and where it ends, each found by its name or clicked on the map; the places passed between.
  const ends = el("div", { class: "plan-ends" },
    placeField("from", "From", start === null ? null : letterOf(0), start === null ? null : N[0], start === null ? "" : at(P[0])),
    ...V.slice(1, -1).map(i => el("div", { class: "plan-field" }, el("label", {},
      el("span", { class: "plan-letter" }, letterOf(i)), el("span", { class: "plan-label" }, "Via"),
      el("span", { class: "grow" }, N[i] || el("span", { class: "sub" }, at(P[i]))),
      el("button", { class: "link", type: "button", title: `Take point ${letterOf(i)} away`, "aria-label": `Take point ${letterOf(i)} away`, onclick: () => planRemove(i) }, "✕")))),
    placeField("to", "To", end === null ? null : letterOf(end), end === null ? null : N[end], end === null ? "" : at(P[end])));
  const slider = el("input", { type: "range", min: 0, max: 100, step: 5, value: Math.round(planAsk.prefer * 100), "aria-label": "How much DNT's routes are preferred",
    oninput: e => { planAsk.prefer = Number(e.target.value) / 100; replanSoon(); } });
  const heights = el("input", { type: "range", min: -100, max: 100, step: 10, value: Math.round(planAsk.mountains * 100), "aria-label": "Whether it keeps to the valleys or up in the mountains",
    oninput: e => { planAsk.mountains = Number(e.target.value) / 100; replanSoon(); } });
  const form = el("div", { class: "trip-ask" },
    el("label", { class: "plan-prefer" }, el("span", {}, "Shortest"), slider, el("span", {}, "Along DNT's routes")),
    el("label", { class: "plan-prefer" }, el("span", {}, "Valleys"), heights, el("span", {}, "Mountains")),
    ...[["across", "Across country where there is no path (a sketch)"], ["wade", "Across country, wade rivers (each counts as 1 km more)"],
        ["dry", "Go round fords on paths by a bridge, where there is one"], ["ferries", "Ferries may be taken"], ["ice", "Over glaciers too"],
        ["alpine", "Demanding alpine paths and via ferratas too"]].map(([key, says]) =>
      // (Going round fords is fords not taken as they come.)
      el("label", {}, el("input", { type: "checkbox", checked: (key === "dry" ? !planAsk.fords : planAsk[key]) ? "" : false,
        onchange: e => { if (key === "dry") planAsk.fords = !e.target.checked; else planAsk[key] = e.target.checked; replan(); } }), ` ${says}`)),
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
      planCrossings(),
      ...plan.off.filter(o => !planAsk.hidden[o.point]).map(o => el("div", { class: "sub" }, `${letterOf(o.point)} is ${o.off_m.toLocaleString("en")} m from where the way passes: that bit is drawn straight.`)),
      // Each leg, between two points of yours: along paths and roads, across country alone (even where there are paths),
      // or straight. One with no way found is drawn straight and says why; one that goes far round says so. One the
      // line was dragged on is as you laid it, until it is given one of the three again.
      planStale ? "" : el("ol", { class: "plan-legs" }, ...planSegments().map(l => el("li", { class: l.unfound ? "unfound" : l.roundabout ? "roundabout" : "" },
        el("div", { class: "grow" },
          el("div", {}, `${letterOf(l.a)} → ${letterOf(l.b)}: ${km(l.metres / 1000)} `, el("span", { class: "sub" },
            l.manual ? "as you have laid it" : l.unfound ? "drawn straight" : PLAN_MODES[l.mode][1])),
          l.unfound ? el("div", { class: "sub" }, `No way on foot was found: ${l.unfound}. It is drawn straight over whatever lies there: add points to go round, or allow ferries or glaciers.`) : "",
          l.roundabout ? el("div", { class: "sub" }, `${km(l.metres / 1000)} for ${km(l.crow_m / 1000)} as the crow flies: if the ground allows, it may be better across country, or straight.`) : "",
          el("div", { class: "plan-modes", role: "group", "aria-label": `How ${letterOf(l.a)} → ${letterOf(l.b)} goes` },
            ...Object.entries(PLAN_MODES).map(([mode, [says]]) => el("button", { class: "link", type: "button", "aria-pressed": String(!l.manual && l.mode === mode),
              title: l.manual ? "As one leg again, found anew this way" : "", onclick: () => planSegmentMode(l.a, l.b, mode) }, says))))))));
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
      P.length > 1 ? el("button", { class: "btn", onclick: () => { for (const k of ["points", "names", "snap", "hidden", "modes"]) planAsk[k].reverse(); replan(); } }, "The other way") : "",
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
      map.addSource("plan-crossings", { type: "geojson", data: empty });
      for (const how of ["bridge", "ford"]) if (!map.hasImage(`plan-${how}`)) map.addImage(`plan-${how}`, crossingIcon(how), { pixelRatio: 2 });
      map.addLayer({ id: "plan-crossings", type: "symbol", source: "plan-crossings",
        layout: { "icon-image": ["match", ["get", "how"], "bridge", "plan-bridge", "plan-ford"], "icon-allow-overlap": true, "icon-ignore-placement": true } });
      map.addLayer({ id: "plan-days", type: "circle", source: "plan-days", paint: { "circle-radius": 9, "circle-color": "#1f2a30", "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
      map.addLayer({ id: "plan-day-numbers", type: "symbol", source: "plan-days", layout: { "text-field": ["get", "day"], "text-size": 11, "text-font": ["Open Sans Semibold"], "text-allow-overlap": true }, paint: { "text-color": "#fff" } });
      // The line can be taken hold of and dragged to another path: where it is let go is a point it passes.
      for (const id of ["plan", "plan-dashed"]) map.on("mousedown", id, e => {
        if (e.originalEvent.button !== 0 || !plan || planBusy) return;
        e.preventDefault();
        const leg = e.features[0].properties.leg, L = plan.legs[leg];
        planDrag = { leg, index: L ? nearestIndex(e.lngLat, L.at[0], L.at[1]) : -1, from: e.point, moved: false, ghost: null };
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
        if (d.moved && d.leg >= 0 && d.index >= 0) planDragTo(d.index, d.leg, e.lngLat);
      });
      planLayers = true;
    }
    const steps = on && plan ? plan.steps : [];
    const legOf = s => plan.legs.findIndex(l => s.at[0] >= l.at[0] && s.at[0] < Math.max(l.at[1], l.at[0] + 1));
    map.getSource("plan").setData({ type: "FeatureCollection", features: steps.map(s => ({ type: "Feature",
      properties: { colour: s.ice ? GLACIER : (PLAN_CLASSES[s.class] || PLAN_CLASSES.path)[1], dashed: s.class === "ferry" || s.class === "off" || s.class === "field" || s.ice, leg: legOf(s) },
      geometry: { type: "LineString", coordinates: plan.coords.slice(s.at[0], s.at[1] + 1) } })) });
    map.getSource("plan-crossings").setData({ type: "FeatureCollection", features: (on && plan && !planStale ? plan.crossings || [] : []).map(c => ({ type: "Feature",
      properties: { how: c.how }, geometry: { type: "Point", coordinates: c.point || plan.coords[c.at] } })) });
    const days = on && plan && planHeights ? planHeights.days.filter(d => !d.last) : [];
    map.getSource("plan-days").setData({ type: "FeatureCollection", features: days.map(d => ({ type: "Feature", properties: { day: String(d.day) },
      geometry: { type: "Point", coordinates: d.hut ? [d.hut.lon, d.hut.lat] : pointAlong(d.at_m) } })).filter(f => f.geometry.coordinates) });
    if (on && plan) for (const id of ["routes", "routes-ice"]) if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", 0.3);
    for (const id of ["plan", "plan-dashed"]) map.setPaintProperty(id, "line-opacity", planStale ? 0.4 : 1);
    // The points: markers that can be dragged, made anew when they are not the ones shown.
    for (const m of planMarkers) m.remove();
    planMarkers = on ? planVisible().map(i => {
      const p = planAsk.points[i], mark = letterOf(i);
      const dot = el("div", { class: "plan-marker", title: `${planAsk.names[i] ? `${planAsk.names[i]}. ` : ""}${mark}: drag to move` }, mark);
      const m = new maplibregl.Marker({ element: dot, draggable: true }).setLngLat(p).addTo(map);
      m.on("dragend", () => {
        const at = m.getLngLat(), last = planAsk.points.length - 1;
        planAsk.points[i] = [Number(at.lng.toFixed(6)), Number(at.lat.toFixed(6))]; planAsk.names[i] = null;
        planAsk.snap[i] = i > 0 && i < last;          // a point passed on the way goes onto the path it is dropped by
        planAsk.hidden[i] = false;
        replan();
      });
      dot.addEventListener("click", e => e.stopPropagation());
      return m;
    }) : [];
  });
}
// Which piece of the line (from index k to k + 1, between a and b) is nearest to where it was taken hold of.
function nearestIndex(at, a, b) {
  const C = plan.coords, f = Math.cos(at.lat * Math.PI / 180);
  let best = a, bestD = Infinity;
  for (let k = a; k < b; k++) {
    const ax = (C[k][0] - at.lng) * f, ay = C[k][1] - at.lat, bx = (C[k + 1][0] - at.lng) * f, by = C[k + 1][1] - at.lat;
    const dx = bx - ax, dy = by - ay, t = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy || 1)));
    const d = Math.hypot(ax + t * dx, ay + t * dy);
    if (d < bestD) { bestD = d; best = k; }
  }
  return best;
}
// The marks of where the way crosses a river, drawn here: a bridge (an arch under its deck, over the water) on white,
// and a ford, or a river waded, as waves on the blue of water. Twice the size, for screens that have the pixels.
function crossingIcon(how) {
  const S = 44, c = document.createElement("canvas");
  c.width = c.height = S;
  const g = c.getContext("2d"), blue = "#1f6fc5";
  g.beginPath(); g.arc(S / 2, S / 2, S / 2 - 3, 0, 2 * Math.PI);
  g.fillStyle = how === "bridge" ? "#fff" : blue; g.fill();
  g.lineWidth = 3; g.strokeStyle = how === "bridge" ? blue : "#fff"; g.stroke();
  g.lineCap = "round"; g.lineJoin = "round";
  const wave = (y, colour) => {
    g.beginPath(); g.moveTo(10, y);
    for (let x = 10; x < 34; x += 6) g.quadraticCurveTo(x + 1.5, y - 3, x + 3, y), g.quadraticCurveTo(x + 4.5, y + 3, x + 6, y);
    g.strokeStyle = colour; g.lineWidth = 2.6; g.stroke();
  };
  if (how === "bridge") {
    g.fillStyle = "#1f2a30"; g.fillRect(9, 15, 26, 4);                       // the deck
    g.beginPath(); g.arc(22, 29, 9, Math.PI, 0);                              // the arch under it
    g.moveTo(13, 19); g.lineTo(13, 29); g.moveTo(31, 19); g.lineTo(31, 29);   // and where it stands
    g.strokeStyle = "#1f2a30"; g.lineWidth = 3; g.stroke();
    wave(33, blue);
  } else {
    wave(18, "#fff"); wave(27, "#fff");
  }
  return g.getImageData(0, 0, S, S);
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
