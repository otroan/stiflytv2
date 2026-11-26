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
  if (!route) routeFrom = { entry: entryOpen, report: reportOpen };
  visit({ view: ROUTE_VIEWS.includes(view) ? view : "routes", route: code, entry: null, report: null, site: null });
  document.getElementById("scroll").scrollTop = 0;
  const r = over && over.routes.find(x => x.code === code);
  if (r) fitTo(f => f.properties.ids.includes(`|${r.id}|`), 14);
}
// The areas: yours first (your profile says which, in the order you chose them), then the others. "Your areas…" says
// which are yours; the first is the one the front page opens with.
let choosingAreas = null;           // the areas being chosen as yours, in order, while the list is a form
function renderAreas() {
  const all = areas.reduce((sum, a) => ({ routes: sum.routes + a.routes, m: sum.m + a.route_m }), { routes: 0, m: 0 });
  const mine = (me.profile && me.profile.areas) || [];
  const row = a => choosingAreas
    ? el("li", { class: "hit area" }, el("label", { class: "grow", style: "display:flex;gap:8px;align-items:baseline;cursor:pointer" },
        el("input", { type: "checkbox", checked: choosingAreas.includes(a.area) ? "" : false,
          onchange: e => { choosingAreas = e.target.checked ? [...choosingAreas, a.area] : choosingAreas.filter(x => x !== a.area); renderAreas(); } }),
        el("span", { class: "code" }, a.area), choosingAreas.indexOf(a.area) === 0 ? el("span", { class: "sub" }, "opens first") : ""),
        el("span", { class: "far" }, many(a.routes, "route", "routes")))
    : el("li", { class: "hit area", tabindex: 0, onclick: () => visit({ area: a.area }), onkeydown: enter(() => visit({ area: a.area })) },
        el("span", { class: "code grow" }, a.area), el("span", { class: "far" }, many(a.routes, "route", "routes")),
        el("span", { class: "far", style: "min-width:62px;text-align:right" }, `${whole(a.route_m / 1000)} km`));
  const yours = mine.map(m => areas.find(a => a.area === m)).filter(Boolean), others = areas.filter(a => !mine.includes(a.area));
  const save = async () => {
    try { me.profile = await send("PUT", "profile", { areas: choosingAreas, maintainer: (me.profile && me.profile.maintainer) || null }); choosingAreas = null; problem = null; }
    catch (err) { problem = err.message; }
    renderAreas();
  };
  panel.replaceChildren(
    el("h2", {}, season === "winter" ? "Areas, in the winter" : "Areas"),
    areas.length ? el("div", { class: "sub" }, view === "signs" ? "Choose an area for its signs." : `Where there are ${season === "winter" ? "winter routes" : "routes"} of yours in use. Choose an area for its routes and what they add up to.`)
      : el("div", { class: "sub" }, season === "winter"
          ? "No winter route is yours yet. In the route editor, with Winter chosen, DNT's winter routes in Turrutebasen are made yours as the summer's are."
          : "There are no routes of yours in use yet. They are made yours in the route editor."),
    problem ? el("div", { class: "error" }, problem) : "",
    me.name ? el("div", { class: "actions" }, ...(choosingAreas
      ? [el("button", { class: "btn main", onclick: save }, "Save your areas"), el("button", { class: "btn", onclick: () => { choosingAreas = null; renderAreas(); } }, "Cancel"),
         el("span", { class: "sub" }, "Tick the areas you work with. The first you tick opens first.")]
      : [el("button", { class: "btn", onclick: () => { choosingAreas = [...mine]; renderAreas(); } }, mine.length ? "Your areas…" : "Choose your areas…")])) : "",
    !choosingAreas && yours.length ? el("h2", { class: "next" }, "Your areas") : "",
    !choosingAreas && yours.length ? el("ul", {}, ...yours.map(row)) : "",
    !choosingAreas && yours.length && others.length ? el("h2", { class: "next" }, "Other areas") : "",
    el("ul", { style: "margin-top:8px" }, ...(choosingAreas ? areas : others).map(row)),
    areas.length > 1 ? el("div", { class: "sub", style: "margin-top:8px" }, `${many(all.routes, "route", "routes")} and ${whole(all.m / 1000)} km in all.`) : "");
}
// The routes added to this area from another (sun27 in Breheimen), and, for the route responsible, a way to add more.
function addedRoutes() {
  const here = areas.find(a => a.area === area), added = here ? here.added || [] : [];
  const can = (me.features || []).includes("routes");
  if (!added.length && !can) return "";
  const code = el("input", { type: "text", placeholder: "Its code, as sun27", maxlength: 20, style: "width:9em", "aria-label": "The code of a route of another area" });
  const change = async request => {
    // What the area's routes are changes its lists: its routes, and its issues, reports and photos, are asked for again.
    try { await request(); problem = null; areas = await get("owned/areas", { season }); if (rawLines) lines = inUse(rawLines); whenLoaded(() => map.getSource("routes").setData(lines));
          await Promise.all([loadArea(), loadEntries(), loadPhotos(), loadReports()]); }
    catch (err) { problem = err.message; render(); }
  };
  return el("details", { class: "how", style: "margin-top:10px", open: added.length ? "" : false },
    el("summary", {}, added.length ? `Routes from other areas here (${added.length})` : "Routes from other areas…"),
    added.length ? el("ul", {}, ...added.map(id => {
      const r = over && over.routes.find(x => x.id === id);
      return el("li", {}, el("button", { class: "link", onclick: () => r && showRoute(r.code) }, r ? r.code : id), r && r.name ? ` ${r.name} ` : " ",
        can ? el("button", { class: "link", title: "It is no longer counted to this area. Undo puts it back.", onclick: () => change(() => send("DELETE", `areas/${area}/added/${id}`)) }, "take out") : "");
    })) : el("div", { class: "sub" }, "None. A route of another area that is looked after with these can be counted here too."),
    can ? el("form", { class: "actions", onsubmit: e => { e.preventDefault(); if (code.value.trim()) change(() => send("POST", `areas/${area}/added`, { code: code.value.trim() })); } },
      code, el("button", { class: "btn" }, "Add it here")) : "");
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
        r.code.slice(0, 3).toLowerCase() !== area ? el("span", { class: "tag", title: "A route of another area, counted here too" }, ` from ${r.code.slice(0, 3).toLowerCase()}`) : "",
        ...inForce(r.id).slice(0, 1).flatMap(e => [" ", el("span", { class: "tag issue notice", title: e.text }, e.kind === "closed" ? "Closed" : "Warning")])),
      el("span", { class: "far" }, km(r.on_foot_m / 1000)), grade(r.grading)))),
    S.routes && !rows.length ? el("div", { class: "sub" }, "None.") : "",
    problem ? el("div", { class: "error" }, problem) : "",
    addedRoutes());
}
// Back from a route: to the issue or the report it was opened from, else to the list of the part one is in.
function leaveRoute() { visit({ route: null, entry: routeFrom && routeFrom.entry || null, report: routeFrom && routeFrom.report || null }); }
function backFromRoute() {
  const AREA = area.toUpperCase();
  return routeFrom && routeFrom.report ? "‹ The report" : routeFrom && routeFrom.entry ? "‹ The issue"
    : view === "reports" ? `‹ Reports in ${AREA}` : view === "issues" ? `‹ Issues in ${AREA}` : view === "landowners" ? `‹ Landowners in ${AREA}` : `‹ ${AREA}`;
}
// The routes ‹ and › go through: the list of the part one is in, in its order. Issues have no list of routes.
function routeSteps() {
  if (view === "reports") { const { rows, others } = upkeepListed(); return others.some(x => x.code === route) ? others : rows; }
  if (view === "landowners") return over ? over.routes.slice().sort((a, b) => a.code.localeCompare(b.code, "en", { numeric: true })) : [];
  return view === "routes" ? routesListed() : [];
}
function renderRoute() {
  const rows = routeSteps(), r = over && over.routes.find(x => x.code === route), i = rows.findIndex(x => x.code === route);
  const step = (to, label, title) => el("button", { class: "btn step", title, "aria-label": title, disabled: !to, onclick: () => to && showRoute(to.code) }, label);
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: leaveRoute }, backFromRoute()), el("span", { class: "grow" }),
    ...(rows.length ? [step(rows[i - 1], "‹", "The route before in the list"), step(i < 0 ? null : rows[i + 1], "›", "The next route in the list")] : []));
  if (!over) { panel.replaceChildren(back, overProblem ? el("div", { class: "error" }, `Could not get the routes: ${overProblem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  if (!r) { panel.replaceChildren(back, el("div", { class: "sub" }, `No route in use here is called ${route}.`)); return; }
  if (!shownRoute || shownRoute.id !== r.id) {
    const mine = shownRoute = { id: r.id, detail: null, height: null, problem: null };
    const again = () => { if (shownRoute === mine && route === r.code) renderRoute(); };
    get(`owned/routes/${r.id}`).then(d => { mine.detail = d; again(); }).catch(err => { mine.problem = err.message; again(); });
    get(`owned/routes/${r.id}/height`).then(h => { mine.height = h; again(); }).catch(() => {});
  }
  const D = shownRoute.detail, H = shownRoute.height, profile = el("div", { class: "profile" });
  // What the part one is in says of the route comes first: its reports among the reports, its issues among the issues.
  const own = { issues: hasIssues() ? routeIssues(r) : [], reports: hasReports() ? routeUpkeep(r) : [],
                landowners: view === "landowners" && hasLandowners() ? routeLandowners(r) : [] };
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
        r.grading_from === "set" ? "" : el("div", { class: "sub" }, "By its length and climb: nobody has set a grading. The ground can make it harder.")) : "",
      // The route of the other season with its number: jot11v goes with jot11.
      ...(D && D.twin ? [el("dt", {}, D.twin.season === "winter" ? "In the winter" : "In the summer"),
        el("dd", {}, el("button", { class: "link", title: "The same number in the other season", onclick: () => visit({ season: D.twin.season, route: D.twin.code, site: null, entry: null, report: null, track: null }) }, D.twin.code),
          D.twin.places.every(Boolean) ? el("span", { class: "sub" }, ` ${[...new Set(D.twin.places)].join(" – ")}`) : "")] : [])),
    ...(own[view] || []),
    profile,
    el("h2", {}, "Description"),
    D ? (D.description || D.description_back ? describedBoth(D) : el("div", { class: "sub" }, "None is written yet. ",
        D.utno_id ? "" : el("a", { href: utnoSearch(r.name, r.code), target: "_blank", rel: "noopener", title: "A web search for the route's page on UT.no, in a tab of its own" }, "Search for it on UT.no"),
        D.utno_id ? "" : ". In the route editor its page there is noted once, and its description taken."))
      : shownRoute.problem ? el("div", { class: "error" }, `Could not get the route: ${shownRoute.problem}`) : el("div", { class: "sub" }, "Loading…"),
    el("div", { class: "actions", style: "margin-top:12px" },
      D && D.utno_id ? el("a", { class: "btn", href: `https://ut.no/rutebeskrivelse/${D.utno_id}`, target: "_blank", rel: "noopener" }, "Its page on UT.no") : "",
      el("a", { class: "btn", href: `/ruter?yours=${encodeURIComponent(r.id)}`, title: "Where its line, its places and what is said about it are changed" }, "Open in the route editor"),
      el("button", { class: "btn", "aria-expanded": String(cardAsk === r.id), title: "The route on one page, to print: its facts, a map, the profile and what you choose",
        onclick: () => { cardAsk = cardAsk === r.id ? null : r.id; renderRoute(); } }, "Route card…")),
    cardAsk === r.id ? cardForm(r) : "",
    ...(view === "issues" ? [] : own.issues),
    ...(view === "reports" ? [] : own.reports),
    ...(hasPhotos() ? routePhotos(r) : []));
  if (D && H) drawProfile(profile, D, H);
}
// The route card: which parts are on it, chosen here and remembered in the browser, then made in a tab of its own to
// print. A part that is a feature of its own is offered only to one who has that feature, as the card has it.
const CARD_PARTS = [["map", "Map", true, null], ["profile", "Height profile", true, null], ["description", "Description", true, null],
  ["walk", "The route section by section", true, null], ["heritage", "Heritage sites near it", true, null],
  ["issues", "Open issues and notices", true, "issues"], ["upkeep", "Inspections and work parties", true, "reports"],
  ["photos", "Photos near it", false, "photos"], ["signs", "Signs along it", false, null],
  ["owners", "Who owns the ground (asked of the register now)", false, "landowners"]];
let cardAsk = null;              // the route whose card is being chosen
function cardChosen() {
  try { const kept = JSON.parse(localStorage.getItem("stiflyt2.card")); if (Array.isArray(kept)) return new Set(kept); } catch { /* not kept */ }
  return new Set(CARD_PARTS.filter(p => p[2]).map(p => p[0]));
}
function cardForm(r) {
  const chosen = cardChosen(), parts = CARD_PARTS.filter(p => !p[3] || (me.features || []).includes(p[3]));
  const make = () => {
    const want = parts.map(p => p[0]).filter(k => chosen.has(k));
    localStorage.setItem("stiflyt2.card", JSON.stringify(want));
    window.open(new URL(`routecard/${encodeURIComponent(r.id)}?with=${want.join(",")}`, API).href, "_blank", "noopener");
    cardAsk = null; renderRoute();
  };
  return el("div", { class: "card-ask" },
    el("div", { class: "sub" }, "On the card besides its facts, in a tab of its own to print or save as PDF:"),
    ...parts.map(([k, label]) => el("label", {}, el("input", { type: "checkbox", checked: chosen.has(k) ? "" : false,
      onchange: e => { if (e.target.checked) chosen.add(k); else chosen.delete(k); } }), ` ${label}`)),
    el("div", { class: "actions" }, el("button", { class: "btn main", onclick: make }, "Make the card"),
      el("button", { class: "btn", onclick: () => { cardAsk = null; renderRoute(); } }, "Cancel")));
}
// The photos taken near a route (the feature `photos`), fetched when the route is opened, and a way to add more.
function routePhotos(r) {
  const mine = shownRoute;
  if (mine && (mine.photos === undefined || mine.photos === null)) {
    mine.photos = false;
    get("photos", { route: r.id }).then(list => { mine.photos = list; if (shownRoute === mine && route) renderRoute(); }).catch(() => {});
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
    mayChangeIssues() ? el("div", { class: "actions" }, el("button", { class: "btn", onclick: () => placeEntry(r) }, "Report an issue on this route")) : ""];
}
