// The front page: where one is in it, what has been fetched, and what the side shows.
let me = { operates: false }, areas = [], area = null, found = null, shown = "all", open = null, problem = null;
let view = "routes";             // what the side shows of the area: its routes, or its signs
let route = null;                // the code of the route that is open: among the routes, or in a part that says something of it
// A route opens in the part one is in, where that part has something of its own to say of it (its reports, its issues),
// so that its list, its colours on the map and its back button stay that part's.
const ROUTE_VIEWS = ["routes", "reports", "issues", "landowners"];
let routeFrom = null;            // the issue or the report the open route was opened from: where its back button goes
let over = null;                 // the area as a whole: its routes, and what they add up to
let overProblem = null, foundProblem = null;
let lines = null;                // every stretch of the routes in use, as the map draws them
let seek = "";                   // what is typed to find a route
let shownRoute = null;           // the open route as it was fetched: { id, detail, height, problem }
let several = null;              // the box on the map that asks which of several routes at a spot
let adrift = [];                 // what was decided about a sign, a post or a site that is no longer there
let lookingAtAdrift = false;
let undoable = null;             // your latest decision about signs, which an undo would take back
let pending = { changes: [], in_the_way: null }, pendingRead = false, pendingGrew = false;     // what you have decided since you last saved
let stakes = [], placing = null, editing = null;
const chosen = new Set();        // the signs clicked, to be added to the order together
let sought = "";                 // what is typed to find a site
let hot = null;                  // the post and the way a sign points that the pointer is at: { post, bearing }
let drawn = [];                  // the lines on the map that say which way the signs of the open site point
const panel = document.getElementById("panel"), hint = document.getElementById("hint");
// Where one is in the page is in its address: the area, its routes or its signs, the route or the sign site that is open.
// So a view can be linked to, and the browser's back goes back.
function here() {
  const q = new URLSearchParams(location.search);
  return { view: ["signs", "trips", "issues", "photos", "reports", "landowners", "planner", "tracks"].includes(q.get("view")) ? q.get("view") : "routes", area: (q.get("area") || "").toLowerCase() || null, route: q.get("route"), site: q.get("site"), entry: q.get("issue"), report: q.get("report"), track: q.get("track") };
}
function addressOf(at) {
  const url = new URL(location.href);
  for (const [k, v] of Object.entries({ area: at.area, view: ["signs", "trips", "issues", "photos", "reports", "landowners", "planner", "tracks"].includes(at.view) ? at.view : null,
      route: ROUTE_VIEWS.includes(at.view) && at.area ? at.route : null, site: at.view === "signs" && at.area ? at.site : null,
      issue: at.view === "issues" && at.area ? at.entry : null, report: at.view === "reports" && at.area ? at.report : null,
      track: at.view === "tracks" && at.area ? at.track : null }))
    if (v) url.searchParams.set(k, v); else url.searchParams.delete(k);
  return url;
}
function visit(next, sure) {
  const to = { view, area, route, site: open, entry: entryOpen, report: reportOpen, track: trackOpen, ...next };
  // Leaving a sign site with decisions that are not saved: it is asked first what is to become of them.
  if (!sure && view === "signs" && open && pending.changes.length && (to.view !== "signs" || to.area !== area || (to.site || null) !== open)) {
    beforeLeavingSite(() => visit(next, true));
    return Promise.resolve();
  }
  const url = addressOf(to);
  if (url.href !== location.href) history.pushState(null, "", url);
  return arrive(to);
}
// Arriving somewhere: by a click, by the browser going back, or by the address the page was opened with. It is done
// (and `arriving` with it) when what was asked for on the way in is here and the map has been fitted to it, on this
// arrival and those before it: a lens chosen while the area is still coming waits for the area.
let arriving = Promise.resolve();
function arrive(to, fitting = true) {
  return (arriving = Promise.all([arriving, arriveAt(to, fitting)]).then(() => {}));
}
function arriveAt(to, fitting) {
  // The planner is of no area: anywhere in Norway. The area one was in is remembered, for the other parts.
  const wanted = to.view !== "planner" && areas.some(a => a.area === to.area) ? to.area : null, moved = wanted !== area;
  // Going in among the signs, or to a sign site: the signs are asked for as they are now.
  const goingIn = to.view === "signs" && wanted && (view !== "signs" || (to.site || null) !== open && Boolean(to.site));
  view = to.view; route = ROUTE_VIEWS.includes(view) && wanted ? to.route || null : null; open = view === "signs" && wanted ? to.site || null : null;
  entryOpen = view === "issues" && wanted && !route ? to.entry || null : null;
  if (view !== "issues" || entryOpen || route) entryAsk = null;
  reportOpen = view === "reports" && wanted && !route ? to.report || null : null;
  if (view !== "reports" || reportOpen || route) reportAsk = null;
  trackOpen = view === "tracks" && wanted ? to.track || null : null; trackRenaming = false;
  editing = null; problem = null;
  if (view !== "signs" || moved) lookingAtAdrift = false;
  leaveProfile(); pointRoute(null);
  if (placing) stopPlacing();
  if (several) { several.remove(); several = null; }
  if (!route) { shownRoute = null; routeFrom = null; }
  if (!moved) { render(); if (goingIn && found) freshen(); return Promise.resolve(); }
  area = wanted; over = null; overProblem = null; found = null; foundProblem = null; shown = "all"; sought = ""; seek = ""; chosen.clear();
  entries = null; entriesFor = null; entriesProblem = null; entryShown = null; photosAll = null; photosFor = null;
  reportsAll = upkeep = sinceLines = null; reportsFor = null; reportShown = null;
  tracksAll = trackLines = tracksFor = trackShown = null;
  if (area) localStorage.setItem("stiflyt2.area", area); else if (to.view !== "planner") localStorage.removeItem("stiflyt2.area");
  render();
  return (area ? Promise.all([loadArea(), load(), loadEntries(), loadPhotos(), loadReports(), loadTracks()]) : Promise.resolve()).then(() => { if (fitting && view !== "planner") fit(); });
}
async function loadArea() {
  const asked = area;
  try { const got = await get(`owned/areas/${asked}`); if (asked !== area) return; over = got; }
  catch (err) { if (asked !== area) return; overProblem = err.message; }
  render();
}
async function load() {
  if (!area) return;
  const asked = area;
  let got;
  try { got = await get("signs", { area: asked }); }
  catch (err) { if (asked === area) { foundProblem = err.message; render(); } return; }
  if (asked !== area) return;
  found = got; foundProblem = null;
  if (open && !found.sites.some(s => s.key === open)) open = null;
  render();
  lookForLoose();
}
// What was decided about a sign that is no longer there, in any area, and what an undo would take back.
// Asked for after the signs are shown: neither is needed to show them.
async function lookForLoose() {
  const asked = area;
  try {
    let [loose, mine, work] = await Promise.all([get("signs/adrift"), me.operates ? get("signs/undo") : { last: null },
      me.operates ? get("signs/work") : { changes: [], in_the_way: null }]);
    if (asked !== area) return;
    // Decided with no sign site open, and nothing else waiting (signs put on the order from the list): that is
    // no item of work to come back to, and is saved as it is made. Undo still takes it back.
    if (pendingRead && work.changes.length && !pending.changes.length && !open) {
      try { await send("POST", "signs/work/save"); work = { changes: [], in_the_way: null }; } catch { /* shown as not saved */ }
    }
    pendingGrew = pendingRead && work.changes.length > pending.changes.length;
    pendingRead = true;
    const was = adrift.length;
    adrift = loose; undoable = mine.last; pending = work;
    renderUndo();
    // The side is drawn anew only when there is something new to say in it: a field being typed in would be emptied.
    if (view === "signs" && found && !open && (adrift.length !== was || lookingAtAdrift)) {
      if (!adrift.length) lookingAtAdrift = false;
      render();
    }
  } catch {}
}
// The signs as they are now, when somebody else may have decided something since they were fetched: asked
// for when one goes in among the signs or opens a sign site, as a route is fetched when it is opened. The side
// is drawn anew only if something is different, and not under a form that is being filled in.
async function freshen() {
  const asked = area, was = JSON.stringify(found);
  let got;
  try { got = await get("signs", { area: asked }); } catch { return; }
  if (asked !== area || editing || !found || JSON.stringify(found) !== was || JSON.stringify(got) === was) return;
  found = got;
  if (open && !found.sites.some(s => s.key === open)) open = null;
  if (view === "signs") render();
  lookForLoose();
}
// A change of yours: made, and everything worked out again, so that what you see is what is kept.
async function change(request) {
  problem = null;
  try { await request(); } catch (err) { problem = err.message; }
  await load();
}
// The parts of the page: the routes, the signs and planning, at the top; under the routes the lenses one looks at
// them through (their overview, issues, reports, photos, owners), under planning what is planned. A lens or a kind
// of plan one does not have is not there, nor a row with only one in it. Each part goes back to the lens last used.
const PARTS = { routes: ["routes", "issues", "reports", "photos", "tracks", "landowners"], plan: ["planner", "trips"] };
const PART_TABS = { routes: "lens-routes", issues: "tab-issues", reports: "tab-reports", photos: "tab-photos", tracks: "tab-tracks", landowners: "tab-landowners",
                    planner: "tab-planner", trips: "tab-trips", signs: "tab-signs" };
const partOf = v => PARTS.routes.includes(v) ? "routes" : PARTS.plan.includes(v) ? "plan" : "signs";
const partShown = id => !document.getElementById(id).hidden;
function lastOf(part) {
  const kept = localStorage.getItem(`stiflyt2.lens.${part}`), have = PARTS[part].filter(v => partShown(PART_TABS[v]));
  return have.includes(kept) ? kept : have[0] || PARTS[part][0];
}
// Where a part or a lens leads: the area one is in (none for the planner, which is of no area), and the route one has
// open where the lens has something to say of it.
const placeFor = v => ({ view: v, area: v === "planner" ? null : area || localStorage.getItem("stiflyt2.area"),
                         route: ROUTE_VIEWS.includes(v) && ROUTE_VIEWS.includes(view) ? route : null });
function renderParts() {
  const part = partOf(view);
  if (part !== "signs") localStorage.setItem(`stiflyt2.lens.${part}`, view);
  for (const [id, p, to] of [["tab-routes", "routes", lastOf("routes")], ["tab-signs", "signs", "signs"], ["tab-plan", "plan", lastOf("plan")]]) {
    const tab = document.getElementById(id);
    if (part === p) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    tab.href = addressOf({ view: to, area: to === "planner" ? null : area || localStorage.getItem("stiflyt2.area") }).href;
  }
  for (const [v, id] of Object.entries(PART_TABS)) {
    if (v === "signs") continue;
    const tab = document.getElementById(id);
    if (view === v) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    tab.href = addressOf(placeFor(v)).href;
  }
  document.getElementById("lenses").hidden = part !== "routes" || PARTS.routes.filter(v => partShown(PART_TABS[v])).length < 2;
  document.getElementById("plans").hidden = part !== "plan" || PARTS.plan.filter(v => partShown(PART_TABS[v])).length < 2;
}
function render() {
  document.body.dataset.view = view;
  renderParts();
  if (view === "planner") renderPlanner();
  else if (!area) renderAreas();
  else if (route) renderRoute();
  else if (view === "routes") renderArea();
  else if (view === "trips") renderTrips();
  else if (view === "issues") renderIssues();
  else if (view === "photos") renderPhotos();
  else if (view === "tracks") renderTracks();
  else if (view === "reports") renderReports();
  else if (view === "landowners") renderLandowners();
  else if (!found) panel.replaceChildren(foundProblem ? el("div", { class: "error" }, `Could not get the signs: ${foundProblem}`) : el("div", { class: "sub" }, "Working out the signs…"));
  else if (lookingAtAdrift && !open) renderAdrift();
  else { const site = open && found.sites.find(s => s.key === open); if (site) renderSite(site); else renderList(); }
  renderUndo();
  renderChosen();
  renderBasket();
  drawMap();
  drawTrip();
  drawPlan();
  drawIssues();
  drawPhotos();
  drawTracks();
  drawReports();
  drawLandowners();
  const url = addressOf({ view, area, route, site: open, entry: entryOpen, report: reportOpen, track: trackOpen });       // what a change of yours led to is in the address too
  if (url.href !== location.href) history.replaceState(null, "", url);
}
