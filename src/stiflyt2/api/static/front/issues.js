// Issues and notices on the routes (the feature `issues`, ADR-004, ADR-005): what is wrong or to be done at a
// spot, along parts of a route or on the whole route, and notices to walkers (closed, a warning). The tab, the marks on
// the map and the notices in a route are only there for one who has the feature; the server refuses the rest.
const ISSUE_KINDS = { clearing: "Clearing", marking: "Marking", sign: "Sign or post", bridge: "Bridge", wet: "Wet ground", other: "Other", closed: "Closed", warning: "Warning" };
const ISSUE_STATES = { reported: "Reported", planned: "Planned", done: "Done", dismissed: "Dismissed" };
const isNotice = kind => kind === "closed" || kind === "warning";
let entries = null, entriesFor = null, entriesProblem = null;   // the issues of the area, as fetched
let entryOpen = null;            // the issue that is open: its id, in the address as `issue`
let entryShown = null;           // that one with its history: { id, told, problem }
let entryAsk = null;             // a new issue, or one being changed: { route, code, lon, lat } or { id }; with `draft`, what the form says
let entryShow = "open";          // which the list has: the open ones, those done or dismissed, or all
let entryUndo = null;            // your latest decision in operations, which an undo would take back
const hasIssues = () => (me.features || []).includes("issues");
const dayOf = d => d ? new Date(`${d}T12:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "";
const whenSaid = t => new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const entriesOn = id => (entries || []).filter(e => e.routes.some(r => r.id === id));
const inForce = id => entriesOn(id).filter(e => e.active);
const kindTag = e => el("span", { class: `tag issue ${isNotice(e.kind) ? "notice" : ""} ${e.open ? "" : "shut"}` }, ISSUE_KINDS[e.kind] || e.kind);
const kmOf = m => (m / 1000).toLocaleString("en", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const partsSaid = parts => parts.map(p => `${kmOf(p.from_m)}–${kmOf(p.to_m)} km`).join(", ");
const metresIn = parts => parts.reduce((n, p) => n + p.metres, 0);
// Where on its route an issue is, in words: at 4.1 km; 3.2–3.5 km; the whole route.
const placeSaid = e => !e.route ? "on no route in use" : e.about === "part" ? `${e.route.code} ${partsSaid(e.parts)}`
  : e.about === "spot" && e.at_m !== null ? `${e.route.code} at ${kmOf(e.at_m)} km` : e.route.code;
// The states an issue can be in, as its kind says them: a notice is up, or lifted.
const statesOf = e => isNotice(e.kind) ? { reported: "Up", done: "Lifted", dismissed: "Dismissed" } : ISSUE_STATES;
const span = e => e.valid_from || e.valid_until ? `${e.valid_from ? `from ${dayOf(e.valid_from)}` : "now"} ${e.valid_until ? `until ${dayOf(e.valid_until)}` : "until further notice"}` : "until further notice";
// A notice in force, as a route shows it at the top.
const noticeBox = e => el("div", { class: `notice-box ${e.kind}`, role: "note", title: "Open the notice", style: "cursor:pointer", onclick: () => openEntry(e.id) },
  el("strong", {}, e.kind === "closed" ? `Closed ${span(e)}` : `Warning, ${span(e)}`),
  e.about === "route" ? "" : el("span", { class: "sub" }, e.about === "spot" ? ` (at one spot${e.at_m !== null ? `, ${kmOf(e.at_m)} km` : ""})` : ` (along ${partsSaid(e.parts)})`),
  el("div", { class: "said" }, e.text));

async function loadEntries() {
  if (!hasIssues() || !area) { entries = null; entriesFor = null; return; }
  const asked = area;
  try { const got = await get("issues", { area: asked }); if (asked !== area) return; entries = got; entriesFor = asked; entriesProblem = null; }
  catch (err) { if (asked !== area) return; entriesProblem = err.message; }
  try { entryUndo = (await get("issues/undo")).last; } catch { entryUndo = null; }
  if (entryOpen && entryShown && entryShown.id === entryOpen && (entries || []).some(e => e.id === entryOpen)) fetchEntry(entryOpen);
  render();
}
function fetchEntry(id) {
  const mine = entryShown = { id, told: entryShown && entryShown.id === id ? entryShown.told : null, problem: null };
  get(`issues/${id}`).then(t => { mine.told = t; }).catch(err => { mine.problem = err.message; }).then(() => { if (entryShown === mine && view === "issues") render(); });
}
// A change of yours to an issue: made, and the issues fetched again.
async function entryChange(request) {
  problem = null;
  try { await request(); } catch (err) { problem = err.message; }
  await loadEntries();
  return !problem;
}
function undoEntry() {
  if (!entryUndo) return;
  const txid = entryUndo.txid;
  entryUndo = null;
  entryChange(() => send("POST", "issues/undo", { txid }));
}
function openEntry(id) {
  entryAsk = null;
  visit({ view: "issues", entry: id, route: null, site: null });
  const e = (entries || []).find(x => x.id === id);
  if (e && e.lon !== null) map.easeTo({ center: [e.lon, e.lat], zoom: Math.max(map.getZoom(), 14), animate: !calm });
  document.getElementById("scroll").scrollTop = 0;
}

// A new issue: the next click on a route is where it is. With a route given, on that route.
function placeEntry(route) {
  placing = { issue: true, route: route || null };
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren(route ? `Click where on ${route.code} it is.` : "Click the route where it is.", el("button", { class: "btn", onclick: stopPlacing }, "Cancel"));
}
function moveEntry(id, about) {
  placing = { issue: true, move: id, about };
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren("Click the route where it is now.", el("button", { class: "btn", onclick: stopPlacing }, "Cancel"));
}
// A part of the route, chosen on the map: where it begins, then where it ends.
function pickPart(from = null) {
  entryAsk.picking = { from };
  placing = { issue: true, pick: true };
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren(entryAsk.picking.from ? "Now click where it ends." : "Click on the route where the part begins.",
    el("button", { class: "btn", onclick: () => { if (entryAsk) entryAsk.picking = null; stopPlacing(); render(); } }, "Cancel"));
}
// The route an issue being said is on: the one clicked, or the one it is measured along.
function entryRoute() {
  if (entryAsk.route) return entryAsk.route;
  const was = (entryShown && entryShown.told) || (entries || []).find(x => x.id === entryAsk.id);
  return was && was.route ? was.route.id : null;
}
// Parts added to what the form says: drawn and measured by the service, which keeps nothing of them yet.
async function addParts(asked) {
  const d = entryAsk.draft;
  try {
    const got = await send("POST", "issues/parts", { route: entryRoute(), parts: [...d.parts.map(p => ({ from_m: p.from_m, to_m: p.to_m })), ...asked] });
    d.parts = got.parts; d.lines = got.lines; d.changed = true; problem = null;
  } catch (err) { problem = err.message; }
  render();
}
function dropPart(i) {
  const d = entryAsk.draft;
  d.parts.splice(i, 1); d.changed = true;
  if (d.parts.length) addParts([]); else { d.lines = []; render(); }
}
// The click: on which route, and where. Where several routes run together, the one asked for, else it asks.
function placedEntry(e) {
  const how = placing;
  stopPlacing();
  if (several) { several.remove(); several = null; }
  if (how.pick && entryAsk && entryAsk.picking) {
    const at = [e.lngLat.lng, e.lngLat.lat];
    if (!entryAsk.picking.from) { pickPart(at); render(); return; }
    const from = entryAsk.picking.from;
    entryAsk.picking = null;
    addParts([{ from_point: from, to_point: at }]);
    return;
  }
  if (how.move) { entryChange(() => send("PATCH", `issues/${how.move}`, { spot: { point: [e.lngLat.lng, e.lngLat.lat] }, ...(how.about ? { about: how.about } : {}) })); return; }
  const at = routesAt(e.point), asked = how.route && at.find(r => r.id === how.route.id);
  const start = r => { if (several) { several.remove(); several = null; } entryAsk = { route: r.id, code: r.code, lon: e.lngLat.lng, lat: e.lngLat.lat }; visit({ view: "issues", entry: null, route: null }); };
  if (asked || at.length === 1) start(asked || at[0]);
  else if (!at.length) { problem = "That is not on a route of the area: click on its line."; render(); }
  else several = new maplibregl.Popup({ closeButton: false, maxWidth: "300px" }).setLngLat(e.lngLat).setDOMContent(el("div", {},
    el("div", { class: "sub" }, `${at.length} routes go here. Which is it on?`),
    el("ul", {}, ...at.map(r => el("li", {}, el("button", { class: "link", onclick: () => start(r) }, r.code), r.name ? ` ${r.name}` : ""))))).addTo(map);
}

function renderIssues() {
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null, entry: null }) }, "‹ Areas"));
  const told = problem ? el("div", { class: "error", role: "alert" }, problem) : "";
  if (entryAsk) { panel.replaceChildren(back, told, entryForm()); return; }
  if (entryOpen) { renderEntry(told); return; }
  if (!entries) { panel.replaceChildren(back, el("h2", { class: "area" }, `Issues in ${area}`), entriesProblem ? el("div", { class: "error" }, `Could not get the issues: ${entriesProblem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  const counts = { open: entries.filter(e => e.open).length, shut: entries.filter(e => !e.open).length };
  const listed = entries.filter(e => entryShow === "all" || (entryShow === "open") === e.open);
  const chip = (key, label) => el("button", { "aria-pressed": String(entryShow === key), onclick: () => { entryShow = key; render(); } }, label);
  const adrift = entries.filter(e => e.adrift).length;
  panel.replaceChildren(back,
    el("h2", { class: "area" }, `Issues in ${area}`),
    el("div", { class: "sub" }, "What is wrong or to be done on the routes, and notices to walkers: a route closed, or a warning."),
    told,
    el("div", { class: "actions" }, el("button", { class: "btn main", onclick: () => placeEntry(null) }, "Report an issue")),
    el("div", { class: "filter", role: "group", "aria-label": "Which issues are listed" }, chip("open", `Open (${counts.open})`), chip("shut", `Done or dismissed (${counts.shut})`), chip("all", "All")),
    adrift ? el("div", { class: "sub" }, `${adrift} no longer lie on a route in use: open each to move it onto a route, or take it away.`) : "",
    el("ul", { style: "margin-top:6px" }, ...listed.map(e => el("li", { class: "hit entry", tabindex: 0, onclick: () => openEntry(e.id), onkeydown: enter(() => openEntry(e.id)) },
      kindTag(e),
      el("span", { class: "grow" }, el("span", { class: "said clip" }, e.text),
        el("span", { class: "sub" }, ` ${placeSaid(e)}${e.active ? ", in force" : e.notice && e.lapsed ? ", lapsed" : ""}${e.adrift ? ", adrift" : ""}`)),
      el("span", { class: "far" }, e.open ? (e.state === "planned" ? "planned" : whenSaid(e.made_at)) : (statesOf(e)[e.state] || e.state).toLowerCase())))),
    listed.length ? "" : el("div", { class: "sub" }, entryShow === "open" ? "Nothing open. Report one with the button, or by clicking a route after it." : "None."));
}
function renderEntry(told) {
  const listedE = (entries || []).find(x => x.id === entryOpen);
  if (!entryShown || entryShown.id !== entryOpen) fetchEntry(entryOpen);
  const e = (entryShown && entryShown.told) || listedE;
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ entry: null }) }, `‹ Issues in ${area.toUpperCase()}`));
  if (!e) { panel.replaceChildren(back, entryShown && entryShown.problem ? el("div", { class: "error" }, `Could not get the issue: ${entryShown.problem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  const to = state => entryChange(() => send("PATCH", `issues/${e.id}`, { state }));
  const where = e.about === "route" ? "The whole route" : e.about === "part" ? "Along" : "At";
  const others = e.routes.filter(r => !e.route || r.id !== e.route.id);
  // Its state: one row, the one it is in pressed. A click on another puts it there.
  const states = el("div", { class: "filter states", role: "group", "aria-label": "State" }, ...Object.entries(statesOf(e)).map(([k, says]) =>
    el("button", { "aria-pressed": String(e.state === k), onclick: () => { if (e.state !== k) to(k); } }, says)));
  const fact = (name, ...said) => [el("dt", {}, name), el("dd", {}, ...said)];
  const history = (entryShown && entryShown.told && entryShown.told.history) || [];
  panel.replaceChildren(back,
    el("h2", {}, kindTag(e), e.notice ? ` ${e.active ? "In force" : e.lapsed ? "Lapsed" : e.open ? "Not yet in force" : "Lifted"}` : ""),
    told,
    e.adrift ? el("div", { class: "error" }, `Adrift: ${e.adrift}. Move it onto a route, or take it away.`) : "",
    el("div", { class: "said", style: "margin:6px 0 4px" }, e.text),
    el("dl", { class: "facts" },
      ...fact(where, e.route ? el("button", { class: "link", onclick: () => showRoute(e.route.code) }, e.route.code) : "no route in use",
        e.about === "part" ? ` ${partsSaid(e.parts)} (${metresIn(e.parts).toLocaleString("en")} m)` : e.about === "spot" && e.at_m !== null ? ` at ${kmOf(e.at_m)} km` : "",
        others.length ? el("div", { class: "sub" }, `Also on ${others.map(r => r.code).join(", ")}`) : ""),
      ...(e.notice ? fact("When", span(e)) : []),
      ...fact("State", states),
      ...fact("Reported", `${whenSaid(e.made_at)} by ${e.made_by}`),
      ...(e.updated_at !== e.made_at ? fact("Last changed", `${whenSaid(e.updated_at)} by ${e.updated_by}`) : [])),
    el("div", { class: "actions" },
      el("button", { class: "btn", title: e.about === "part" ? "What it is, its parts, or its dates" : "What it is, or its dates", onclick: () => { entryAsk = { id: e.id }; render(); } }, "Change it…"),
      e.about === "spot" ? el("button", { class: "btn", title: "Click the route where it is", onclick: () => moveEntry(e.id) }, "Move it") : "",
      el("button", { class: "btn quiet", title: "It is taken away. Undo brings it back.", onclick: () => { if (confirm("Take this issue away? Undo brings it back.")) entryChange(() => send("DELETE", `issues/${e.id}`)).then(ok => { if (ok) visit({ entry: null }); }); } }, "Take it away")),
    ...(hasPhotos() ? entryPhotos(e) : []),
    history.length ? el("h2", { class: "next" }, "What has happened") : "",
    history.length ? el("ol", { class: "history" }, ...history.map(h => el("li", {}, el("span", { class: "far" }, whenSaid(h.at)), " ", h.who, ": ", entrySaid(h)))) : "");
}
// The photos of an issue, and a way to add more: placed where their cameras say, else at the issue.
function entryPhotos(e) {
  const mine = entryShown;
  if (mine && (mine.photos === undefined || mine.photos === null)) {
    mine.photos = false;
    get("photos", { entry: e.id }).then(list => { mine.photos = list; if (entryShown === mine && view === "issues") render(); }).catch(() => {});
  }
  const list = mine && mine.photos;
  return [list && list.length ? photoRow(list) : "",
    el("div", { class: "actions" }, addPhotosButton({ entry: e.id, area, lon: e.lon, lat: e.lat }, list && list.length ? "Add more photos" : "Add photos of it"))];
}
// An event of an issue in words.
function entrySaid(h) {
  const p = h.payload || {};
  if (h.type === "entry.reported") return `reported it${p.route ? ` on ${p.route}` : ""}`;
  if (h.type === "entry.taken_away") return "took it away";
  const parts = [];
  if (p.state) parts.push(`said it is ${(ISSUE_STATES[p.state[1]] || p.state[1]).toLowerCase()}`);
  if (p.kind) parts.push(`made it ${(ISSUE_KINDS[p.kind[1]] || p.kind[1]).toLowerCase()}`);
  if (p.text) parts.push("changed the text");
  if (p.about) parts.push(`made it about ${p.about[1] === "route" ? "the whole route" : p.about[1] === "part" ? "parts of the route" : "a spot"}`);
  if (p.valid_from || p.valid_until) parts.push("changed the dates");
  if (p.moved) parts.push("moved it");
  return parts.length ? `${parts.join(", ")}` : "changed it";
}
// Saying what it is: a new one at the spot clicked, or one that is there. What the form says is kept in
// `entryAsk.draft`, so that it is drawn again as it was when parts are added on the map.
function entryForm() {
  const was = entryAsk.id ? (entryShown && entryShown.told) || (entries || []).find(x => x.id === entryAsk.id) : null;
  const today = new Date().toISOString().slice(0, 10);
  const d = entryAsk.draft || (entryAsk.draft = was
    ? { kind: was.kind, text: was.text, about: was.about, from: was.valid_from || "", until: was.valid_until || "", parts: [...was.parts], lines: [...was.lines], changed: false }
    : { kind: "clearing", text: "", about: "spot", from: today, until: "", parts: [], lines: [], changed: false });
  const kind = el("select", { "aria-label": "What it is", onchange: e => { d.kind = e.target.value; dates.hidden = !isNotice(d.kind); } },
    ...Object.entries(ISSUE_KINDS).map(([k, says]) => el("option", { value: k, selected: d.kind === k }, says)));
  const text = el("textarea", { rows: 3, maxlength: 2000, required: "", placeholder: "What it is: two birches across the path below the bridge",
    oninput: e => { d.text = e.target.value; } }, d.text);
  const abouts = [["spot", was && was.about !== "spot" ? "At a spot: click it on the map after saving" : "At this spot"],
                  ["part", "Along part of the route, from where to where you say: one part or more"], ["route", "The whole route"]];
  const about = el("div", { class: "field" }, ...abouts.map(([k, says]) => el("label", { style: "display:block" },
    el("input", { type: "radio", name: "about", value: k, checked: d.about === k ? "" : false,
      onchange: () => { d.about = k; if (k === "part" && !d.parts.length && entryRoute()) pickPart(); render(); } }), ` ${says}`)));
  const fromKm = el("input", { type: "number", min: 0, step: 0.1, style: "width:5em", "aria-label": "From, km from the start" });
  const toKm = el("input", { type: "number", min: 0, step: 0.1, style: "width:5em", "aria-label": "To, km from the start" });
  const typed = () => { if (fromKm.value !== "" && toKm.value !== "") addParts([{ from_m: Number(fromKm.value) * 1000, to_m: Number(toKm.value) * 1000 }]); };
  const parts = d.about !== "part" ? "" : el("div", { class: "field parts" },
    d.parts.length ? el("ol", {}, ...d.parts.map((p, i) => el("li", {}, `${kmOf(p.from_m)}–${kmOf(p.to_m)} km, ${p.metres.toLocaleString("en")} m `,
      el("button", { class: "link", type: "button", onclick: () => dropPart(i) }, "take out"))))
      : el("div", { class: "sub" }, entryAsk.picking ? "Click on the route where the part begins, then where it ends." : "No part chosen yet."),
    el("div", { class: "actions" }, el("button", { class: "btn", type: "button", onclick: () => { pickPart(); render(); } }, d.parts.length ? "Add a part on the map" : "Choose it on the map")),
    el("div", {}, "Or from ", fromKm, " to ", toKm, " km from the start ", el("button", { class: "btn", type: "button", onclick: typed }, "Add")));
  const from = el("input", { type: "date", value: d.from, "aria-label": "From", oninput: e => { d.from = e.target.value; } });
  const until = el("input", { type: "date", value: d.until, "aria-label": "Until", oninput: e => { d.until = e.target.value; } });
  const dates = el("div", { class: "field", hidden: !isNotice(d.kind) }, "For walkers: from ", from, " until ", until, el("div", { class: "sub" }, "Until left empty: until further notice."));
  const done = async e => {
    e.preventDefault();
    const body = { kind: d.kind, text: d.text.trim(), about: d.about,
                   valid_from: isNotice(d.kind) ? d.from || null : null, valid_until: isNotice(d.kind) ? d.until || null : null };
    if (!body.text) { text.focus(); return; }
    if (d.about === "part" && !d.parts.length) { problem = "Choose the part: where it begins and where it ends."; render(); return; }
    const ask = entryAsk;
    if (d.about === "part" && (!was || d.changed || was.about !== "part")) body.parts = d.parts.map(p => ({ from_m: p.from_m, to_m: p.to_m }));
    if (!was && d.about === "spot") body.spot = { point: [ask.lon, ask.lat] };
    // To a spot from something else: where it is, is the next click.
    const spotNext = was && was.about !== "spot" && d.about === "spot";
    if (spotNext) delete body.about;
    let made = null;
    const ok = await entryChange(async () => {
      made = ask.id ? await send("PATCH", `issues/${ask.id}`, body) : await send("POST", "issues", { ...body, route: ask.route });
    });
    if (!ok) { render(); return; }
    entryAsk = null;
    if (made) { entryShown = null; visit({ view: "issues", entry: made.id }); }
    if (spotNext) moveEntry(made.id, "spot");
  };
  return el("form", { class: "entry-form", onsubmit: done },
    el("h2", {}, was ? "Change the issue" : `A new issue on ${entryAsk.code}`),
    el("label", { class: "field" }, "What it is", kind),
    el("label", { class: "field" }, "Say what it is", text),
    about, parts,
    dates,
    el("div", { class: "actions" }, el("button", { class: "btn main" }, was ? "Save" : "Report it"),
      el("button", { class: "btn", type: "button", onclick: () => { entryAsk = null; stopPlacing(); render(); } }, "Cancel")));
}

// The issues on the map: spots as points, stretches as a band under the route. Among the routes only the open
// ones; among the issues those the list has, with the one that is open ringed.
function drawIssues() {
  whenLoaded(() => {
    const on = hasIssues() && entries && entriesFor === area && (view === "routes" || view === "issues");
    for (const id of ["entries", "entry-lines"]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    const editing = entryAsk && entryAsk.id;
    const shown = !on ? [] : entries.filter(e => e.id !== editing && (view === "issues" ? entryShow === "all" || (entryShow === "open") === e.open || e.id === entryOpen : e.open));
    map.getSource("entries").setData({ type: "FeatureCollection", features: shown.filter(e => e.lon !== null && e.about !== "part").map(e => ({ type: "Feature",
      properties: { id: e.id, notice: e.notice, open: e.open, chosen: e.id === entryOpen }, geometry: { type: "Point", coordinates: [e.lon, e.lat] } })) });
    map.getSource("entry-lines").setData({ type: "FeatureCollection", features: shown.flatMap(e => e.lines.map(coords => ({ type: "Feature",
      properties: { id: e.id, notice: e.notice, open: e.open, chosen: e.id === entryOpen }, geometry: { type: "LineString", coordinates: coords } }))) });
    // What the form says, while it is said: the parts chosen, where a part begins, or the spot clicked.
    const d = view === "issues" && entryAsk && entryAsk.draft, picked = [];
    if (d && d.about === "part") picked.push(...d.lines.map(coords => ({ type: "LineString", coordinates: coords })));
    if (d && entryAsk.picking && entryAsk.picking.from) picked.push({ type: "Point", coordinates: entryAsk.picking.from });
    if (d && d.about === "spot" && !entryAsk.id) picked.push({ type: "Point", coordinates: [entryAsk.lon, entryAsk.lat] });
    map.getSource("entry-pick").setData({ type: "FeatureCollection", features: picked.map(geometry => ({ type: "Feature", properties: {}, geometry })) });
  });
}
