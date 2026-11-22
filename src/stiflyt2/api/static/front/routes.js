// The front door: the areas, then the routes of one, then one route. Nothing here is changed; that is done in the route editor.
const GRADING = { G: "Green, easy", B: "Blue, medium", R: "Red, demanding", S: "Black, expert", I: "Not graded" };
const PLACES = { hut: ["hut", "huts"], parking: ["parking place", "parking places"], glacier_edge: ["glacier edge", "glacier edges"], other: ["other place", "other places"] };
const many = (n, one, more) => `${n.toLocaleString("en")} ${n === 1 ? one : more}`;
const enter = act => e => { if (e.key === "Enter") act(); };
// A grading is a colour: the square says which, and so do the words for whoever does not see it.
const grade = g => el("span", { class: `grade ${BY_NUMBERS[g] ? g : "none"}`, title: GRADING[g] || "Not graded" }, el("span", { class: "seen" }, `${GRADING[g] || "Not graded"}. `));
function routesListed() {
  const words = seek.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return over ? over.routes.filter(r => words.every(w => `${r.code} ${r.name || ""}`.toLowerCase().includes(w))) : [];
}
function pointRoute(id) { whenLoaded(() => map.setFilter("route-hot", ["in", `|${id || "none"}|`, ["get", "ids"]])); }
function showRoute(code) {
  visit({ view: "routes", route: code });
  document.getElementById("scroll").scrollTop = 0;
  const r = over && over.routes.find(x => x.code === code);
  if (r) fitTo(f => f.properties.ids.includes(`|${r.id}|`), 14);
}
function renderAreas() {
  const all = areas.reduce((sum, a) => ({ routes: sum.routes + a.routes, m: sum.m + a.route_m }), { routes: 0, m: 0 });
  panel.replaceChildren(
    el("h2", {}, "Areas"),
    areas.length ? el("div", { class: "sub" }, view === "signs" ? "Choose an area for its signs." : "Where there are routes of yours in use. Choose an area for its routes and what they add up to.")
      : el("div", { class: "sub" }, "There are no routes of yours in use yet. They are made yours in the route editor."),
    el("ul", { style: "margin-top:8px" }, ...areas.map(a => el("li", { class: "hit area", tabindex: 0, onclick: () => visit({ area: a.area }), onkeydown: enter(() => visit({ area: a.area })) },
      el("span", { class: "code grow" }, a.area), el("span", { class: "far" }, many(a.routes, "route", "routes")),
      el("span", { class: "far", style: "min-width:62px;text-align:right" }, `${whole(a.route_m / 1000)} km`)))),
    areas.length > 1 ? el("div", { class: "sub", style: "margin-top:8px" }, `${many(all.routes, "route", "routes")} and ${whole(all.m / 1000)} km in all.`) : "");
}
function renderArea() {
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null }) }, "‹ Areas"));
  if (!over) {
    panel.replaceChildren(back, el("h2", { class: "area" }, area), overProblem ? el("div", { class: "error" }, `Could not get the routes: ${overProblem}`) : el("div", { class: "sub" }, "Loading…"));
    return;
  }
  const S = over.statistics, far = m => `${whole(m / 1000)} km`, rows = routesListed(), T = found && found.totals;
  const fact = (name, ...said) => [el("dt", {}, name), el("dd", {}, ...said)];
  const onRoute = at => [`${whole(at.metres)} m, on `, el("button", { class: "link", onclick: () => showRoute(at.route) }, at.route)];
  const ungraded = (S.gradings.I || 0) + (S.gradings.none || 0), byNumbers = over.routes.filter(r => r.grading_from === "length and climb").length;
  const passed = Object.entries(S.places).map(([kind, n]) => many(n, ...(PLACES[kind] || PLACES.other)));
  panel.replaceChildren(back,
    el("h2", { class: "area" }, area),
    el("div", { class: "sub" }, `${many(S.routes, "route", "routes")} in use.`),
    S.routes ? el("dl", { class: "facts" },
      ...fact("Routes", far(S.route_m), el("span", { class: "sub" }, ", each walked for itself")),
      ...fact("Ground", far(S.ground_m), el("span", { class: "sub" }, ", a stretch that routes share counted once")),
      ...fact("Marked", far(S.marked_m), S.unmarked_m ? `, ${far(S.unmarked_m)} not marked` : "", S.unknown_m ? `, ${far(S.unknown_m)} not said` : ""),
      ...(S.glacier_m ? fact("Over glacier", far(S.glacier_m)) : []),
      ...(S.water_m ? fact("Over water", far(S.water_m), el("span", { class: "sub" }, ", not counted as walking")) : []),
      ...(S.highest ? fact("Highest", ...onRoute(S.highest)) : []),
      ...(S.lowest ? fact("Lowest", ...onRoute(S.lowest)) : []),
      ...fact("Grading", ...["G", "B", "R", "S"].filter(g => S.gradings[g]).map(g => el("span", { class: "graded" }, grade(g), `${S.gradings[g]} ${BY_NUMBERS[g]}`)),
        ungraded ? el("span", { class: "graded" }, grade(null), `${ungraded} not graded`) : "",
        byNumbers ? el("div", { class: "sub" }, `${byNumbers === S.routes ? "All" : byNumbers} by length and climb alone: nobody has set ${byNumbers === 1 ? "it" : "them"}.`) : ""),
      ...(passed.length ? fact("Places", passed.join(", ")) : []),
      ...fact("Signs", T ? el("button", { class: "link", title: "The signs of the area", onclick: () => visit({ view: "signs", route: null, site: null }) }, `${many(T.sites, "sign site", "sign sites")} with ${count(T.signs)}`)
        : el("span", { class: "sub" }, foundProblem ? "could not be worked out" : "being worked out…"))) : "",
    S.routes ? el("input", { id: "seek", type: "text", value: seek, placeholder: "Find a route: code or name", "aria-label": "Find a route",
      oninput: e => { seek = e.target.value; const at = e.target.selectionStart; renderArea(); const box = document.getElementById("seek"); box.focus(); box.setSelectionRange(at, at); } }) : "",
    el("ul", { style: "margin-top:6px" }, ...rows.map(r => el("li", { class: "hit", tabindex: 0, onclick: () => showRoute(r.code), onkeydown: enter(() => showRoute(r.code)),
        onmouseenter: () => pointRoute(r.id), onmouseleave: () => pointRoute(null), onfocus: () => pointRoute(r.id), onblur: () => pointRoute(null) },
      el("span", { class: "code" }, r.code), el("span", { class: "grow" }, r.name || el("span", { class: "sub" }, "no name"),
        ...inForce(r.id).slice(0, 1).flatMap(e => [" ", el("span", { class: "tag issue notice", title: e.text }, e.kind === "closed" ? "Closed" : "Warning")])),
      el("span", { class: "far" }, km(r.on_foot_m / 1000)), grade(r.grading)))),
    S.routes && !rows.length ? el("div", { class: "sub" }, "None.") : "");
}
function renderRoute() {
  const rows = routesListed(), r = over && over.routes.find(x => x.code === route), i = r ? rows.indexOf(r) : -1;
  const step = (to, label, title) => el("button", { class: "btn step", title, "aria-label": title, disabled: !to, onclick: () => to && showRoute(to.code) }, label);
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ route: null }) }, `‹ ${area.toUpperCase()}`), el("span", { class: "grow" }),
    step(rows[i - 1], "‹", "The route before in the list"), step(i < 0 ? null : rows[i + 1], "›", "The next route in the list"));
  if (!over) { panel.replaceChildren(back, overProblem ? el("div", { class: "error" }, `Could not get the routes: ${overProblem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  if (!r) { panel.replaceChildren(back, el("div", { class: "sub" }, `No route in use here is called ${route}.`)); return; }
  if (!shownRoute || shownRoute.id !== r.id) {
    const mine = shownRoute = { id: r.id, detail: null, height: null, problem: null };
    const again = () => { if (shownRoute === mine && view === "routes" && route === r.code) renderRoute(); };
    get(`owned/routes/${r.id}`).then(d => { mine.detail = d; again(); }).catch(err => { mine.problem = err.message; again(); });
    get(`owned/routes/${r.id}/height`).then(h => { mine.height = h; again(); }).catch(() => {});
  }
  const D = shownRoute.detail, H = shownRoute.height, profile = el("div", { class: "profile" });
  leaveProfile();
  panel.replaceChildren(back,
    el("h2", {}, el("span", { class: "code" }, r.code), r.name || ""),
    ...inForce(r.id).map(noticeBox),
    el("dl", { class: "facts" },
      el("dt", {}, "Length"), el("dd", {}, `${km(r.on_foot_m / 1000)} on foot`,
        r.glacier_m ? el("div", { class: "sub" }, `${km(r.glacier_m / 1000)} of it over glacier.`) : "",
        r.water_m ? el("div", { class: "sub" }, `And ${km(r.water_m / 1000)} over water, not counted as walking.`) : ""),
      el("dt", {}, "Climbs"), el("dd", {}, ...(D ? climbs(D, H || D.height) : [el("span", { class: "sub" }, "…")])),
      r.grading ? el("dt", {}, "Grading") : "", r.grading ? el("dd", {}, grade(r.grading), GRADING[r.grading] || r.grading,
        r.grading_from === "set" ? "" : el("div", { class: "sub" }, "By its length and climb: nobody has set a grading. The ground can make it harder.")) : ""),
    profile,
    el("h2", {}, "Description"),
    D ? (D.description ? el("div", { class: "said" }, D.description) : el("div", { class: "sub" }, "None is written yet."))
      : shownRoute.problem ? el("div", { class: "error" }, `Could not get the route: ${shownRoute.problem}`) : el("div", { class: "sub" }, "Loading…"),
    el("div", { class: "actions", style: "margin-top:12px" },
      D && D.utno_id ? el("a", { class: "btn", href: `https://ut.no/rutebeskrivelse/${D.utno_id}`, target: "_blank", rel: "noopener" }, "Its page on UT.no") : "",
      el("a", { class: "btn", href: `/ruter?yours=${encodeURIComponent(r.id)}`, title: "Where its line, its places and what is said about it are changed" }, "Open in the route editor")),
    ...(hasIssues() ? routeIssues(r) : []),
    ...(hasPhotos() ? routePhotos(r) : []));
  if (D && H) drawProfile(profile, D, H);
}
// The photos taken near a route (the feature `photos`), fetched when the route is opened, and a way to add more.
function routePhotos(r) {
  const mine = shownRoute;
  if (mine && (mine.photos === undefined || mine.photos === null)) {
    mine.photos = false;
    get("photos", { route: r.id }).then(list => { mine.photos = list; if (shownRoute === mine && view === "routes") renderRoute(); }).catch(() => {});
  }
  const list = mine && mine.photos;
  return [el("h2", { class: "next" }, "Photos"),
    list && list.length ? photoRow(list) : el("div", { class: "sub" }, list ? "None taken within 75 m of it yet." : "Loading…"),
    el("div", { class: "actions" }, addPhotosButton({ area }))];
}
// What is open on a route, and a way to report more (the feature `issues`).
function routeIssues(r) {
  const open = entriesOn(r.id).filter(e => e.open && !e.active);
  return [el("h2", { class: "next" }, "Issues"),
    open.length ? el("ul", {}, ...open.map(e => el("li", { class: "hit entry", tabindex: 0, onclick: () => openEntry(e.id), onkeydown: enter(() => openEntry(e.id)) },
      kindTag(e), el("span", { class: "grow" }, el("span", { class: "said clip" }, e.text)), el("span", { class: "far" }, e.state === "planned" ? "planned" : whenSaid(e.made_at)))))
      : el("div", { class: "sub" }, entries ? "Nothing open on this route." : "Loading…"),
    el("div", { class: "actions" }, el("button", { class: "btn", onclick: () => placeEntry(r) }, "Report an issue on this route"))];
}
