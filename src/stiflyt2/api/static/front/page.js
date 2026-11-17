// The front page: where one is in it, what has been fetched, and what the side shows.
let me = { operates: false }, areas = [], area = null, found = null, shown = "all", open = null, problem = null;
let view = "routes";             // what the side shows of the area: its routes, or its signs
let route = null;                // the code of the route that is open, among the routes
let over = null;                 // the area as a whole: its routes, and what they add up to
let overProblem = null, foundProblem = null;
let lines = null;                // every stretch of the routes in use, as the map draws them
let seek = "";                   // what is typed to find a route
let shownRoute = null;           // the open route as it was fetched: { id, detail, height, problem }
let several = null;              // the box on the map that asks which of several routes at a spot
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
  return { view: q.get("view") === "signs" ? "signs" : "routes", area: (q.get("area") || "").toLowerCase() || null, route: q.get("route"), site: q.get("site") };
}
function addressOf(at) {
  const url = new URL(location.href);
  for (const [k, v] of Object.entries({ area: at.area, view: at.view === "signs" ? "signs" : null,
      route: at.view === "routes" && at.area ? at.route : null, site: at.view === "signs" && at.area ? at.site : null }))
    if (v) url.searchParams.set(k, v); else url.searchParams.delete(k);
  return url;
}
function visit(next) {
  const to = { view, area, route, site: open, ...next };
  const url = addressOf(to);
  if (url.href !== location.href) history.pushState(null, "", url);
  return arrive(to);
}
// Arriving somewhere: by a click, by the browser going back, or by the address the page was opened with.
function arrive(to, fitting = true) {
  const wanted = areas.some(a => a.area === to.area) ? to.area : null, moved = wanted !== area;
  view = to.view; route = view === "routes" && wanted ? to.route || null : null; open = view === "signs" && wanted ? to.site || null : null;
  editing = null; problem = null;
  leaveProfile(); pointRoute(null);
  if (placing) stopPlacing();
  if (several) { several.remove(); several = null; }
  if (!route) shownRoute = null;
  if (!moved) { render(); return Promise.resolve(); }
  area = wanted; over = null; overProblem = null; found = null; foundProblem = null; shown = "all"; sought = ""; seek = ""; chosen.clear();
  if (area) localStorage.setItem("stiflyt2.area", area); else localStorage.removeItem("stiflyt2.area");
  render();
  return (area ? Promise.all([loadArea(), load()]) : Promise.resolve()).then(() => { if (fitting) fit(); });
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
}
// A change of yours: made, and everything worked out again, so that what you see is what is kept.
async function change(request) {
  problem = null;
  try { await request(); } catch (err) { problem = err.message; }
  await load();
}
function render() {
  document.body.dataset.view = view;
  for (const [id, is] of [["tab-routes", "routes"], ["tab-signs", "signs"]]) {
    const tab = document.getElementById(id);
    if (view === is) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    tab.href = addressOf({ view: is, area }).href;
  }
  if (!area) renderAreas();
  else if (view === "routes") { if (route) renderRoute(); else renderArea(); }
  else if (!found) panel.replaceChildren(foundProblem ? el("div", { class: "error" }, `Could not get the signs: ${foundProblem}`) : el("div", { class: "sub" }, "Working out the signs…"));
  else { const site = open && found.sites.find(s => s.key === open); if (site) renderSite(site); else renderList(); }
  renderChosen();
  renderBasket();
  drawMap();
  const url = addressOf({ view, area, route, site: open });       // what a change of yours led to is in the address too
  if (url.href !== location.href) history.replaceState(null, "", url);
}
