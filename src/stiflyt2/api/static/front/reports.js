// Reports of inspections and work parties (the feature `reports`): when each route was last inspected and last had a
// work party, as a list with those longest since first and as colours on the map, stretch by stretch, as skisporet.no
// shows when a track was prepared; and the reports themselves. Reporting needs the editor or operations role as well.
const REPORT_KINDS = { inspection: "Inspection", work: "Work party", note: "Diary note" };
const DONE_WORDS = { clearing: "Clearing", marking: "Marking", signs: "Signs", bridges: "Bridges", boards: "Boards", drainage: "Drainage", other: "Other" };
const CONDITION_WORDS = { good: "Good", fair: "Fair", poor: "Poor" };
const DUE = { inspection: 3, work: 10 };          // years: decided with the route responsible
const AGE_COLOURS = { recent: "#2e7d32", ageing: "#f9a825", due: "#c62828", never: "#5f6b73" };
let reportsAll = null, reportsFor = null, upkeep = null, sinceLines = null, reportUndo = null;
let sinceKind = "inspection";       // what the list and the map say how long since: inspections, or work parties
let reportOpen = null;              // the report that is open: its id, in the address as `report`
let reportShown = null;             // that one as fetched: { id, told }
let reportAsk = null;               // a report being said: what the form says (`id` when it is one already kept)
const hasReports = () => (me.features || []).includes("reports");
const mayReport = () => hasReports() && (me.features || []).some(f => f === "routes" || f === "signs");
const ageOf = (last, kind) => !last ? "never" : last.due ? "due" : last.years > DUE[kind] / 2 ? "ageing" : "recent";
const dayIs = d => d ? new Date(`${d}T12:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "";
const whenDone = r => r.year_only ? String(r.done_on).slice(0, 4) : dayIs(r.done_on);     // only the year, where only it is known
const reportTag = r => el("span", { class: `tag report ${r.kind}` }, REPORT_KINDS[r.kind] || r.kind);
const coversSaid = r => r.covers.map(c => !c.route ? "a route that is gone" : c.whole ? c.route.code
  : `${c.route.code} ${c.parts.map(p => `${kmOf(p.from_m)}–${kmOf(p.to_m)} km`).join(", ")}`).join("; ");

async function loadReports() {
  if (!hasReports() || !area) { reportsAll = upkeep = sinceLines = null; reportsFor = null; return; }
  const asked = area;
  try {
    const [all, status, lines] = await Promise.all([get("reports", { area: asked }), get("reports/status", { area: asked }), get("reports/coverage", { area: asked, kind: sinceKind })]);
    if (asked !== area) return;
    reportsAll = all; upkeep = status; sinceLines = lines; reportsFor = asked;
  } catch { /* the part says it could not */ }
  try { reportUndo = (await get("reports/undo")).last; } catch { reportUndo = null; }
  if (reportOpen && (reportsAll || []).some(r => r.id === reportOpen)) fetchReport(reportOpen);
  render();
}
function fetchReport(id) {
  const mine = reportShown = { id, told: reportShown && reportShown.id === id ? reportShown.told : null, problem: null };
  get(`reports/${id}`).then(t => { mine.told = t; }).catch(err => { mine.problem = err.message; }).then(() => { if (reportShown === mine && view === "reports") render(); });
}
async function reportChange(request) {
  problem = null;
  try { await request(); } catch (err) { problem = err.message; }
  await Promise.all([loadReports(), loadEntries()]);
  return !problem;
}
function openReport(id) {
  reportAsk = null;
  visit({ view: "reports", report: id, route: null, site: null, entry: null });
  document.getElementById("scroll").scrollTop = 0;
}
function undoReport() {
  if (!reportUndo) return;
  const txid = reportUndo.txid;
  reportUndo = null;
  reportChange(() => send("POST", "reports/undo", { txid }));
}
// A new report: of a route, when one is given, else of none yet.
function newReport(route) {
  reportAsk = { kind: "inspection", done_on: localDay(), year_only: false, covers: route ? [{ route: route.id, code: route.code, name: route.name, whole: true, parts: [], lines: [] }] : [],
                text: "", condition: null, persons: "", hours: "", done: [], link: "", closes: [] };
  visit({ view: "reports", report: null, route: null, site: null, entry: null });
}

// A part of one route of the report, chosen on the map: where it begins, then where it ends.
function pickReportPart(row, from = null) {
  placing = { reportPart: row, from };
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren(from ? "Now click where it ends." : `Click on ${reportAsk.covers[row].code} where the part begins.`, el("button", { class: "btn", onclick: () => { stopPlacing(); render(); } }, "Cancel"));
  drawReports();
}
async function placedReportPart(e) {
  const how = placing, at = [e.lngLat.lng, e.lngLat.lat];
  stopPlacing();
  if (!reportAsk || !reportAsk.covers[how.reportPart]) return;
  if (!how.from) { pickReportPart(how.reportPart, at); return; }
  await setReportParts(how.reportPart, [{ from_point: how.from, to_point: at }]);
}
// The parts of one route of the report, measured and drawn by the service; with km, or with points clicked.
async function setReportParts(row, asked) {
  const c = reportAsk.covers[row];
  try {
    const got = await send("POST", "reports/parts", { route: c.route, parts: [...c.parts.map(p => ({ from_m: p.from_m, to_m: p.to_m })), ...asked] });
    c.parts = got.parts; c.lines = got.lines; c.whole = false; problem = null;
  } catch (err) { problem = err.message; }
  render();
}

function renderReports() {
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null, entry: null, report: null }) }, "‹ Areas"));
  const told = problem ? el("div", { class: "error", role: "alert" }, problem) : "";
  if (reportAsk) { panel.replaceChildren(back, told, reportForm()); return; }
  if (reportOpen) { renderReport(told); return; }
  if (!upkeep) { panel.replaceChildren(back, el("h2", { class: "area" }, `Reports in ${area}`), el("div", { class: "sub" }, reportsFor === null ? "Loading…" : "Could not get the reports.")); return; }
  const chip = (key, label) => el("button", { "aria-pressed": String(sinceKind === key), onclick: () => { sinceKind = key; sinceLines = null; render(); get("reports/coverage", { area, kind: key }).then(l => { sinceLines = l; render(); }).catch(() => {}); } }, label);
  const half = DUE[sinceKind] / 2;
  const legend = el("div", { class: "since-legend" }, ...[["recent", `under ${half} years`], ["ageing", `${half}–${DUE[sinceKind]} years`], ["due", `over ${DUE[sinceKind]} years: due`], ["never", "never, or not reported"]]
    .map(([age, says]) => el("span", {}, el("i", { style: `background:${AGE_COLOURS[age]}` }), says)));
  const rows = upkeep.slice().sort((a, b) => (a[sinceKind] ? a[sinceKind].done_on : "") < (b[sinceKind] ? b[sinceKind].done_on : "") ? -1 : 1);
  const due = rows.filter(r => !r[sinceKind] || r[sinceKind].due).length;
  panel.replaceChildren(back,
    el("h2", { class: "area" }, `Reports in ${area}`),
    el("div", { class: "sub" }, "When each route was last inspected (every 3 years) and last had a work party (every 10 years). Between, the issues reported on it say what is to be done."),
    told,
    mayReport() ? el("div", { class: "actions" }, el("button", { class: "btn main", onclick: () => newReport(null) }, "Report")) : "",
    el("div", { class: "filter", role: "group", "aria-label": "Since what" }, chip("inspection", "Since the last inspection"), chip("work", "Since the last work party")),
    legend,
    el("div", { class: "sub", style: "margin:6px 0" }, `${due} of ${rows.length} routes are due or have none reported.`),
    el("ul", {}, ...rows.map(r => {
      const last = r[sinceKind], age = ageOf(last, sinceKind);
      return el("li", { class: "hit", tabindex: 0, onclick: () => showRoute(r.code), onkeydown: enter(() => showRoute(r.code)), onmouseenter: () => pointRoute(r.id), onmouseleave: () => pointRoute(null) },
        el("span", { class: "code" }, r.code), el("span", { class: "grow" }, (over && over.routes.find(x => x.id === r.id) || {}).name || r.name || ""),
        el("span", { class: "far" }, last ? `${new Date(`${last.done_on}T12:00:00`).getFullYear()}${last.all_of_it ? "" : ", part"}` : "never"),
        el("i", { class: "age-dot", style: `background:${AGE_COLOURS[age]}`, title: age }));
    })),
    reportsAll && reportsAll.length ? el("h2", { class: "next" }, "Reports, the latest first") : "",
    reportsAll && reportsAll.length ? el("ul", {}, ...reportsAll.slice(0, 60).map(r => el("li", { class: "hit entry", tabindex: 0, onclick: () => openReport(r.id), onkeydown: enter(() => openReport(r.id)) },
      reportTag(r), el("span", { class: "grow" }, el("span", {}, whenDone(r)), el("span", { class: "sub" }, ` ${coversSaid(r)}`), r.text ? el("div", { class: "said clip" }, r.text) : "")))) : "");
}
function renderReport(told) {
  if (!reportShown || reportShown.id !== reportOpen) fetchReport(reportOpen);
  const r = (reportShown && reportShown.told) || (reportsAll || []).find(x => x.id === reportOpen);
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ report: null }) }, `‹ Reports in ${area.toUpperCase()}`));
  if (!r) { panel.replaceChildren(back, reportShown && reportShown.problem ? el("div", { class: "error" }, `Could not get the report: ${reportShown.problem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  const fact = (name, ...said) => [el("dt", {}, name), el("dd", {}, ...said)];
  const mine = reportShown && reportShown.photos;
  if (reportShown && mine === undefined) { reportShown.photos = null; if (hasPhotos()) get("photos", { report: r.id }).then(l => { if (reportShown) { reportShown.photos = l; render(); } }).catch(() => {}); }
  panel.replaceChildren(back,
    el("h2", {}, reportTag(r), ` ${whenDone(r)}`),
    told,
    el("dl", { class: "facts" },
      ...fact("Where", ...r.covers.flatMap((c, i) => [i ? "; " : "", c.route ? el("button", { class: "link", onclick: () => showRoute(c.route.code) }, c.route.code) : "a route that is gone",
        c.route && !c.whole ? ` ${c.parts.map(p => `${kmOf(p.from_m)}–${kmOf(p.to_m)} km`).join(", ")}` : ""])),
      ...(r.condition ? fact("State found", CONDITION_WORDS[r.condition]) : []),
      ...(r.persons || r.hours ? fact("Work", [r.persons ? `${r.persons} ${r.persons === 1 ? "person" : "people"}` : null, r.hours ? `${r.hours} hours` : null].filter(Boolean).join(", ")) : []),
      ...(r.done && r.done.length ? fact("Done", r.done.map(d => DONE_WORDS[d] || d).join(", ")) : []),
      ...(r.link ? fact("The report", el("a", { href: r.link, target: "_blank", rel: "noopener" }, "Open it")) : []),
      ...fact("Reported", `${whenSaid(r.made_at)} by ${r.made_by}`)),
    r.text ? el("div", { class: "said", style: "margin:4px 0 8px" }, r.text) : "",
    r.closed && r.closed.length ? el("div", { class: "sub" }, "Said done by it: ", ...r.closed.flatMap((x, i) => [i ? "; " : "", el("button", { class: "link", onclick: () => openEntry(x.id) }, x.text.length > 50 ? `${x.text.slice(0, 50)}…` : x.text)])) : "",
    mine && mine.length ? photoRow(mine) : "",
    el("div", { class: "actions" },
      hasPhotos() ? addPhotosButton({ report: r.id, area }, mine && mine.length ? "Add more photos" : "Add photos of it") : "",
      mayReport() ? el("button", { class: "btn", onclick: () => { reportAsk = { id: r.id, kind: r.kind, done_on: r.done_on, year_only: r.year_only, text: r.text || "", condition: r.condition, persons: r.persons ?? "", hours: r.hours ?? "", done: [...r.done], link: r.link || "", closes: [],
        covers: r.covers.filter(c => c.route).map(c => ({ route: c.route.id, code: c.route.code, name: (over && over.routes.find(x => x.id === c.route.id) || {}).name, whole: c.whole, parts: c.parts, lines: [] })), changedCovers: false }; render(); } }, "Change it…") : "",
      mayReport() ? el("button", { class: "btn quiet", title: "Undo brings it back", onclick: () => { if (confirm("Take this report away? Undo brings it back.")) reportChange(() => send("DELETE", `reports/${r.id}`)).then(ok => { if (ok) visit({ report: null }); }); } }, "Take it away") : ""));
}
// The days photos were taken, near the routes chosen or in the whole area, the latest first: the photos say which day it
// was, and where one went. "It was this day" says the day and that its photos are of the report; with no route chosen
// yet, it also chooses the routes they were taken near.
let photoDaysFor = null, photoDays = null;
function reportPhotoDays(d) {
  if (!hasPhotos()) return "";
  const routes = d.covers.map(c => c.route).join(","), key = `${area}|${routes}`;
  if (photoDaysFor !== key) {
    photoDaysFor = key; photoDays = null;
    get("photos/days", { area, routes: routes || null }).then(l => { if (photoDaysFor === key) { photoDays = l; render(); } }).catch(() => { if (photoDaysFor === key) { photoDays = []; render(); } });
  }
  if (!photoDays) return el("div", { class: "field sub" }, "Looking for photos from there…");
  if (!photoDays.length) return el("div", { class: "field sub" }, d.covers.length ? "No photos were taken near these routes." : "No photos with a day and a place from this area.");
  const shown = photoDays.slice(0, d.allDays ? 80 : 6);
  return el("div", { class: "field photo-days" }, d.covers.length ? "Days with photos near these routes" : "Days with photos here: which day was it?",
    ...shown.map(day => {
      const chosen = d.photoDay === String(day.day);
      return el("div", { class: `photo-day${chosen ? " chosen" : ""}` },
        el("div", {}, el("strong", {}, dayIs(String(day.day))), ` ${day.photos.length} ${day.photos.length === 1 ? "photo" : "photos"}`,
          day.routes.length ? el("span", { class: "sub" }, `, near ${day.routes.slice(0, 5).map(r => r.code).join(", ")}${day.routes.length > 5 ? "…" : ""}`) : "", " ",
          chosen ? el("span", { class: "tag" }, "this day") : el("button", { class: "btn", type: "button", onclick: () => chooseDay(d, day) }, "It was this day")),
        photoRow(day.photos.slice(0, 8)),
        day.photos.length > 8 ? el("button", { class: "link", type: "button", onclick: () => showPhoto(day.photos, 8) }, `and ${day.photos.length - 8} more`) : "");
    }),
    photoDays.length > shown.length ? el("button", { class: "link", type: "button", onclick: () => { d.allDays = true; render(); } }, `Earlier days (${photoDays.length - shown.length})`) : "",
    d.photos && d.photos.length ? el("label", { style: "display:block;margin-top:6px" }, el("input", { type: "checkbox", checked: d.attach ? "" : false, onchange: e => { d.attach = e.target.checked; } }),
      ` The ${d.photos.length} ${d.photos.length === 1 ? "photo" : "photos"} of that day are of this report`) : "");
}
function chooseDay(d, day) {
  d.done_on = String(day.day); d.year_only = false; d.photoDay = String(day.day);
  d.photos = day.photos.map(p => p.id); d.attach = true;
  if (!d.covers.length) {
    for (const r of day.routes) {
      const ours = over && over.routes.find(x => x.id === r.id);
      d.covers.push({ route: r.id, code: r.code, name: ours ? ours.name : "", whole: true, parts: [], lines: [] });
    }
    d.changedCovers = true;
  }
  render();
}
// Saying what was done: the day, where (whole routes, or parts by two clicks or in km), and what was found or done.
function reportForm() {
  const d = reportAsk, today = localDay();
  const onRoutes = (over ? over.routes : []).filter(r => !d.covers.some(c => c.route === r.id));
  const add = el("select", { "aria-label": "Add a route", onchange: e => { const r = over.routes.find(x => x.id === e.target.value); if (r) { d.covers.push({ route: r.id, code: r.code, name: r.name, whole: true, parts: [], lines: [] }); d.changedCovers = true; render(); } } },
    el("option", { value: "" }, d.covers.length ? "Add another route…" : "Which route? Choose…"), ...onRoutes.map(r => el("option", { value: r.id }, `${r.code} ${r.name || ""}`)));
  const covers = el("ol", { class: "covers" }, ...d.covers.map((c, i) => {
    const fromKm = el("input", { type: "number", min: 0, step: 0.1, style: "width:4.5em", "aria-label": "From, km" }), toKm = el("input", { type: "number", min: 0, step: 0.1, style: "width:4.5em", "aria-label": "To, km" });
    return el("li", {},
      el("div", {}, el("strong", {}, c.code), ` ${c.name || ""} `, el("button", { class: "link", type: "button", onclick: () => { d.covers.splice(i, 1); d.changedCovers = true; render(); } }, "take out")),
      el("label", { style: "margin-right:10px" }, el("input", { type: "radio", name: `whole${i}`, checked: c.whole ? "" : false, onchange: () => { c.whole = true; c.parts = []; c.lines = []; d.changedCovers = true; render(); } }), " All of it"),
      el("label", {}, el("input", { type: "radio", name: `whole${i}`, checked: c.whole ? false : "", onchange: () => { c.whole = false; d.changedCovers = true; pickReportPart(i); render(); } }), " Only part of it"),
      c.whole ? "" : el("div", { class: "sub" },
        c.parts.length ? `${c.parts.map(p => `${kmOf(p.from_m)}–${kmOf(p.to_m)} km`).join(", ")} ` : "No part yet. ",
        el("button", { class: "link", type: "button", onclick: () => pickReportPart(i) }, c.parts.length ? "add a part on the map" : "choose it on the map"),
        " or from ", fromKm, " to ", toKm, " km ", el("button", { class: "link", type: "button", onclick: () => { if (fromKm.value !== "" && toKm.value !== "") { d.changedCovers = true; setReportParts(i, [{ from_m: Number(fromKm.value) * 1000, to_m: Number(toKm.value) * 1000 }]); } } }, "add")));
  }));
  const field = (label, input) => el("label", { class: "field" }, label, input);
  const kind = el("select", { onchange: e => { d.kind = e.target.value; render(); } }, ...Object.entries(REPORT_KINDS).map(([k, says]) => el("option", { value: k, selected: d.kind === k }, says)));
  const day = d.year_only
    ? el("input", { type: "number", min: 1950, max: Number(today.slice(0, 4)), step: 1, style: "width:6em", value: String(d.done_on).slice(0, 4), oninput: e => { d.done_on = `${e.target.value}-07-01`; } })
    : el("input", { type: "date", value: d.done_on, max: today, oninput: e => { d.done_on = e.target.value; } });
  const yearOnly = el("label", { style: "margin-left:10px" }, el("input", { type: "checkbox", checked: d.year_only ? "" : false,
    onchange: e => { d.year_only = e.target.checked; if (d.year_only) d.done_on = `${String(d.done_on).slice(0, 4)}-07-01`; render(); } }), " Only the year is known");
  const open = typeof entries !== "undefined" && entries ? entries.filter(e => e.open && !e.notice && e.routes.some(r => d.covers.some(c => c.route === r.id))) : [];
  const done = async e => {
    e.preventDefault();
    if (!d.covers.length) { problem = "Say which routes it was on."; render(); return; }
    if (d.covers.some(c => !c.whole && !c.parts.length)) { problem = "Say which part of the route: where it begins and where it ends."; render(); return; }
    const body = { kind: d.kind, done_on: d.done_on, year_only: Boolean(d.year_only), text: d.text || null, link: d.link || null,
                   condition: d.kind === "inspection" ? d.condition : null, persons: d.kind === "work" && d.persons !== "" ? Number(d.persons) : null,
                   hours: d.kind === "work" && d.hours !== "" ? Number(d.hours) : null, done: d.kind === "work" ? d.done : [] };
    if (!d.id || d.changedCovers) body.covers = d.covers.map(c => c.whole ? { route: c.route } : { route: c.route, parts: c.parts.map(p => ({ from_m: p.from_m, to_m: p.to_m })) });
    if (!d.id) body.closes = d.closes;
    if (d.attach && d.photos && d.photos.length) body.photos = d.photos;
    let made = null;
    const ok = await reportChange(async () => { made = d.id ? await send("PATCH", `reports/${d.id}`, body) : await send("POST", "reports", body); });
    if (!ok) { render(); return; }
    reportAsk = null;
    if (made) { reportShown = null; visit({ view: "reports", report: made.id }); }
  };
  return el("form", { class: "entry-form", onsubmit: done },
    el("h2", {}, d.id ? "Change the report" : "A report"),
    field("What it is", kind),
    el("label", { class: "field" }, d.year_only ? "The year it was done" : "The day it was done", el("div", {}, day, yearOnly)),
    el("div", { class: "field" }, "Where", covers, onRoutes.length ? add : ""),
    reportPhotoDays(d),
    d.kind === "inspection" ? el("div", { class: "field" }, "The state found ", ...Object.entries(CONDITION_WORDS).map(([k, says]) => el("label", { style: "margin-right:10px" },
      el("input", { type: "radio", name: "condition", checked: d.condition === k ? "" : false, onchange: () => { d.condition = k; } }), ` ${says}`))) : "",
    d.kind === "work" ? el("div", { class: "field" }, "People ", el("input", { type: "number", min: 0, step: 1, style: "width:4.5em", value: d.persons, oninput: e => { d.persons = e.target.value; } }),
      " and hours in all ", el("input", { type: "number", min: 0, step: 0.5, style: "width:5.5em", value: d.hours, oninput: e => { d.hours = e.target.value; } })) : "",
    d.kind === "work" ? el("div", { class: "field" }, "Done: ", ...Object.entries(DONE_WORDS).map(([k, says]) => el("label", { style: "margin-right:8px;white-space:nowrap" },
      el("input", { type: "checkbox", checked: d.done.includes(k) ? "" : false, onchange: e => { d.done = e.target.checked ? [...d.done, k] : d.done.filter(x => x !== k); } }), ` ${says}`))) : "",
    field("What was found or done", el("textarea", { rows: 3, maxlength: 4000, oninput: e => { d.text = e.target.value; } }, d.text)),
    field("Where the report itself is kept (a link to SharePoint)", el("input", { type: "url", value: d.link, placeholder: "https://…", style: "width:100%", oninput: e => { d.link = e.target.value; } })),
    !d.id && open.length ? el("div", { class: "field" }, "Issues it saw to (they are said done):", ...open.map(x => el("label", { style: "display:block" },
      el("input", { type: "checkbox", checked: d.closes.includes(x.id) ? "" : false, onchange: e => { d.closes = e.target.checked ? [...d.closes, x.id] : d.closes.filter(y => y !== x.id); } }), " ", kindTag(x), ` ${x.text.length > 60 ? `${x.text.slice(0, 60)}…` : x.text}`))) : "",
    el("div", { class: "actions" }, el("button", { class: "btn main" }, d.id ? "Save" : "Report it"),
      el("button", { class: "btn", type: "button", onclick: () => { reportAsk = null; stopPlacing(); render(); } }, "Cancel")));
}

// When each route was last done, on the map: the routes grey, and what the reports covered in the colour of its age,
// the latest on top. And the parts of a report being said.
function drawReports() {
  whenLoaded(() => {
    const on = view === "reports" && hasReports() && reportsFor === area;
    map.setPaintProperty("routes", "line-color", on ? AGE_COLOURS.never : css("--sign"));
    const half = DUE[sinceKind] / 2, now = Date.now();
    map.getSource("since").setData({ type: "FeatureCollection", features: on && sinceLines && !reportAsk ? sinceLines.map(s => {
      const years = (now - new Date(`${s.done_on}T12:00:00`).getTime()) / 3.15576e10;
      return { type: "Feature", properties: { t: new Date(`${s.done_on}T12:00:00`).getTime() / 8.64e7, colour: years > DUE[sinceKind] ? AGE_COLOURS.due : years > half ? AGE_COLOURS.ageing : AGE_COLOURS.recent },
               geometry: { type: "LineString", coordinates: s.line } };
    }) : [] });
    if (view !== "reports") return;         // what is being chosen elsewhere is drawn by its own part
    const picked = [];
    if (reportAsk) for (const c of reportAsk.covers) picked.push(...c.lines.map(coordinates => ({ type: "LineString", coordinates })));
    if (placing && placing.reportPart !== undefined && placing.from) picked.push({ type: "Point", coordinates: placing.from });
    map.getSource("entry-pick").setData({ type: "FeatureCollection", features: picked.map(geometry => ({ type: "Feature", properties: {}, geometry })) });
  });
}
// What the reports say of a route, in its panel: when it was last inspected and worked on, and its reports.
function routeUpkeep(r) {
  const s = (upkeep || []).find(x => x.id === r.id), mine = shownRoute;
  if (mine && mine.reports === undefined) { mine.reports = null; get("reports", { route: r.id }).then(l => { mine.reports = l; if (shownRoute === mine && view === "routes") renderRoute(); }).catch(() => {}); }
  const line = (kind, label) => {
    const last = s && s[kind];
    return el("div", {}, el("i", { class: "age-dot", style: `background:${AGE_COLOURS[ageOf(last, kind)]}` }), ` ${label}: `,
      last ? `${whenDone(last)}${last.condition ? ` (${CONDITION_WORDS[last.condition].toLowerCase()})` : ""}${last.all_of_it ? "" : ", part of it"}${last.due ? ": due" : ""}` : "never reported");
  };
  const list = mine && mine.reports;
  return [el("h2", { class: "next" }, "Upkeep"),
    line("inspection", "Last inspected"), line("work", "Last work party"),
    list && list.length ? el("ul", { style: "margin-top:6px" }, ...list.slice(0, 8).map(x => el("li", { class: "hit entry", tabindex: 0, onclick: () => openReport(x.id), onkeydown: enter(() => openReport(x.id)) },
      reportTag(x), el("span", { class: "grow" }, whenDone(x), x.text ? el("span", { class: "sub" }, ` ${x.text.length > 50 ? `${x.text.slice(0, 50)}…` : x.text}`) : "")))) : "",
    mayReport() ? el("div", { class: "actions" }, el("button", { class: "btn", onclick: () => newReport(r) }, "Report work on this route")) : ""];
}
