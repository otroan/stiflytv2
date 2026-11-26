// The search at the top: a route of ours by its code, its name or a place it passes (in any area), an area by its
// letters, and a place in the whole country by its name. A route opens in the lens one is in (the routes' overview
// from elsewhere), an area opens, and the map goes to a place; in the planner a place can be where the walk begins or
// ends. "/" goes to the search from anywhere but a field being typed in; Escape leaves it; Enter takes the first.
const searchBox = document.getElementById("search"), searchFound = document.getElementById("search-found");
let searchAsked = 0, searchHits = [], searchAt = -1, searchPin = null;

function searchClose() { searchFound.hidden = true; searchHits = []; searchAt = -1; searchBox.setAttribute("aria-expanded", "false"); }
function searchShow(groups) {
  searchHits = groups.flatMap(g => g.hits);
  searchAt = searchHits.length ? 0 : -1;
  searchFound.replaceChildren(...(searchHits.length ? groups.filter(g => g.hits.length).map(g => el("li", { class: "group" },
    el("div", { class: "sub" }, g.title),
    el("ul", {}, ...g.hits.map(h => el("li", { role: "option", class: "hit", "data-hit": searchHits.indexOf(h), onmousedown: e => { e.preventDefault(); searchTake(h); } },
      el("span", { class: h.code ? "code" : "" }, h.title), h.sub ? el("span", { class: "sub" }, ` ${h.sub}`) : "")))))
    : [el("li", { class: "sub none" }, "Nothing found")]));
  searchMark();
  searchFound.hidden = false;
  searchBox.setAttribute("aria-expanded", "true");
}
function searchMark() {
  for (const li of searchFound.querySelectorAll("[data-hit]")) li.classList.toggle("current", Number(li.dataset.hit) === searchAt);
}
async function searchFor(q) {
  const asked = ++searchAsked;
  if (q.trim().length < 2) { searchClose(); return; }
  const c = map.getCenter(), low = q.trim().toLowerCase();
  const [found, named] = await Promise.all([
    get("search/routes", { q, limit: 8, season }).catch(() => []),
    get("placenames", { q, lon: c.lng.toFixed(5), lat: c.lat.toFixed(5), limit: 6 }).catch(() => [])]);
  if (asked !== searchAsked) return;
  const typeOf = t => (t || "").replace(/([A-ZÆØÅ])/g, " $1").toLowerCase();
  searchShow([
    { title: "Routes", hits: found.map(r => ({ code: r.code, title: r.code, sub: [r.name, r.passes && !(r.name || "").includes(r.passes) ? `passes ${r.passes}` : null].filter(Boolean).join(", "),
                                               take: () => searchRoute(r) })) },
    { title: "Areas", hits: areas.filter(a => a.area.startsWith(low)).slice(0, 4).map(a => ({ code: a.area, title: a.area, sub: `${a.routes} routes`,
                                                                                               take: () => visit({ ...placeFor(PARTS.routes.includes(view) ? view : lastOf("routes")), area: a.area, route: null }) })) },
    { title: "Places", hits: named.map(p => ({ title: p.name, sub: [typeOf(p.type), p.municipality].filter(Boolean).join(", "), take: () => searchPlace(p) })) }]);
}
function searchTake(hit) { searchClose(); searchBox.value = ""; searchBox.blur(); hit.take(); }
// A route, in the lens one is in: in its area, opened, and the map on it.
function searchRoute(r) {
  const lens = ROUTE_VIEWS.includes(view) ? view : ROUTE_VIEWS.includes(lastOf("routes")) ? lastOf("routes") : "routes";
  visit({ view: lens, area: r.area, route: r.code, site: null, entry: null, report: null })
    .then(() => fitTo(f => f.properties.ids.includes(`|${r.id}|`), 14));
}
// A place: the map goes there, and says what it is; in the planner, where the walk may begin or end.
function searchPlace(p) {
  if (searchPin) searchPin.remove();
  map.flyTo({ center: [p.lon, p.lat], zoom: Math.max(map.getZoom(), 13), essential: true, animate: !calm });
  const body = el("div", {}, el("strong", {}, p.name), el("div", { class: "sub" }, [p.type && p.type.replace(/([A-ZÆØÅ])/g, " $1").toLowerCase(), p.municipality].filter(Boolean).join(", ")));
  if (view === "planner" && hasPlanner()) body.append(el("div", { class: "actions" },
    el("button", { class: "btn", onclick: () => { searchPin.remove(); planFrom([p.lon, p.lat], p.name); } }, "Begin here"),
    el("button", { class: "btn", onclick: () => { searchPin.remove(); planTo([p.lon, p.lat], p.name); } }, "End here")));
  searchPin = new maplibregl.Popup({ maxWidth: "260px" }).setLngLat([p.lon, p.lat]).setDOMContent(body).addTo(map);
}
let searchTimer = null;
searchBox.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => searchFor(searchBox.value), 200); });
searchBox.addEventListener("keydown", e => {
  if (e.key === "Escape") { e.stopPropagation(); searchBox.value = ""; searchClose(); searchBox.blur(); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!searchHits.length) return;
    e.preventDefault();
    searchAt = (searchAt + (e.key === "ArrowDown" ? 1 : searchHits.length - 1)) % searchHits.length;
    searchMark();
  } else if (e.key === "Enter" && searchHits.length) { e.preventDefault(); searchTake(searchHits[Math.max(0, searchAt)]); }
});
searchBox.addEventListener("blur", () => setTimeout(searchClose, 150));
searchBox.addEventListener("focus", () => { if (searchBox.value.trim().length >= 2) searchFor(searchBox.value); });
document.addEventListener("keydown", e => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName) || e.target.isContentEditable) return;
  e.preventDefault();
  searchBox.focus();
});
