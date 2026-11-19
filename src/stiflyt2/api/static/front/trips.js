// Trips for work (the feature `trips`, ADR-005): choose routes, and get a trip on foot that covers them, cut
// into days that end at huts. The tab is only there for one who has the feature; the server refuses the rest.
// The days are told apart by colour and by their number: on the map, and in the list, which is the legend.
const DAY_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const LATER_DAYS = "#5f666c";       // more days than colours: the rest share one, and are told apart by their numbers
let trip = null, tripProblem = null, tripBusy = false, tripDay = null, tripFor = null, tripPlaces = null;
const tripAsk = { all: true, routes: new Set(), start: "", hours: 7, tent: false, ice: false, back: false, mapped: true };
const dayColour = n => DAY_COLOURS[n - 1] || LATER_DAYS;
const hoursOf = m => `${Math.floor(m / 60)} h ${String(Math.round(m) % 60).padStart(2, "0")}`;
const placeOf = (end, no) => end.place || (no ? "a junction, with no hut" : "a junction");

async function planTrip() {
  if (tripBusy) return;
  tripBusy = true; tripProblem = null; render();
  const body = { start: tripAsk.start.trim() || null, hours: tripAsk.hours, tent: tripAsk.tent, over_ice: tripAsk.ice, back_to_start: tripAsk.back, mapped: tripAsk.mapped,
                 ...(tripAsk.all ? { area } : { routes: [...tripAsk.routes] }) };
  try { trip = await send("POST", "trips/plan", body); tripFor = area; tripDay = null; }
  catch (err) { tripProblem = err.message; }
  tripBusy = false; render();
  if (trip && !tripProblem) showDays(trip.days);
}
function showDays(days) {
  const all = days.flatMap(d => d.coords), lon = all.map(c => c[0]), lat = all.map(c => c[1]);
  if (all.length) map.fitBounds([[Math.min(...lon), Math.min(...lat)], [Math.max(...lon), Math.max(...lat)]], { padding: 60, maxZoom: 14, animate: !calm });
}
// A day, or all of them, as a GPX file to walk by.
function tripGpx(days) {
  const safe = s => String(s).replace(/[<>&"]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const credit = trip && trip.credits && trip.credits.length ? `<metadata><copyright author="${safe(trip.credits.join(". "))}"/></metadata>\n` : "";
  const text = `<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Stiflyt" xmlns="http://www.topografix.com/GPX/1/1">\n${credit}`
    + days.map(d => `<trk><name>${safe(`Day ${d.day}: ${placeOf(d.from)} – ${placeOf(d.to)}`)}</name><desc>${safe(d.steps.map(s => `${s.says}, ${km(s.metres / 1000)}`).join(". "))}.</desc><trkseg>\n`
      + d.coords.map(c => `<trkpt lat="${c[1]}" lon="${c[0]}"/>`).join("\n") + "\n</trkseg></trk>").join("\n") + "\n</gpx>\n";
  const link = el("a", { href: URL.createObjectURL(new Blob([text], { type: "application/gpx+xml" })),
                         download: days.length === 1 ? `trip-${area}-day-${days[0].day}.gpx` : `trip-${area}.gpx` });
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 5000);
}
function renderTrips() {
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null }) }, "‹ Areas"));
  if (!over) { panel.replaceChildren(back, el("h2", { class: "area" }, area), overProblem ? el("div", { class: "error" }, `Could not get the routes: ${overProblem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  if (tripFor !== area) { trip = null; tripDay = null; tripAsk.routes.clear(); tripAsk.all = true; tripFor = area; }
  if (!tripPlaces) { tripPlaces = []; get("places.geojson").then(all => { tripPlaces = [...new Set(all.features.filter(f => f.properties.type === "place").map(f => f.properties.name))].sort(); if (view === "trips") render(); }).catch(() => {}); }
  const routes = over.routes;
  // Ticking a route says "these, not all": said in the form as it stands, so that the list stays where it was scrolled to.
  const every = el("input", { type: "checkbox", checked: tripAsk.all ? "" : false, onchange: e => { tripAsk.all = e.target.checked; said(); } });
  const summary = el("summary", {}), plan = el("button", { class: "btn main" });
  const said = () => {
    every.checked = tripAsk.all;
    summary.textContent = tripAsk.all ? "Or choose which routes…" : `${tripAsk.routes.size} of ${routes.length} routes chosen`;
    plan.disabled = tripBusy || (!tripAsk.all && !tripAsk.routes.size);
    plan.textContent = tripBusy ? "Working it out…" : "Plan the trip";
  };
  const pick = el("details", { class: "how", open: tripAsk.all ? false : "" }, summary,
    el("ul", { class: "trip-routes" }, ...routes.map(r => el("li", {}, el("label", {},
      el("input", { type: "checkbox", value: r.code, checked: tripAsk.routes.has(r.code) ? "" : false,
                    onchange: e => { if (e.target.checked) tripAsk.routes.add(r.code); else tripAsk.routes.delete(r.code); tripAsk.all = false; said(); } }),
      " ", el("span", { class: "code" }, r.code), " ", r.name || "")))));
  said();
  const form = el("form", { class: "trip-ask", onsubmit: e => { e.preventDefault(); planTrip(); } },
    el("label", {}, every, ` Every route in use here (${routes.length})`),
    pick,
    el("label", {}, "It begins at ", el("input", { type: "text", list: "tripplaces", value: tripAsk.start, placeholder: "wherever the least is walked twice", maxlength: 120, oninput: e => { tripAsk.start = e.target.value; } })),
    el("datalist", { id: "tripplaces" }, ...(tripPlaces || []).map(name => el("option", { value: name }))),
    el("label", {}, "A day is ", el("input", { type: "number", min: 3, max: 14, step: 0.5, value: tripAsk.hours, style: "width:5em", oninput: e => { tripAsk.hours = Number(e.target.value) || 7; } }), " hours of walking, with no rests"),
    ...[["mapped", "Between routes, also along other paths and roads (FKB, NVDB, OpenStreetMap)"], ["tent", "With a tent: a day may end anywhere"], ["ice", "Over glaciers too"], ["back", "It ends where it began"]].map(([key, says]) =>
      el("label", {}, el("input", { type: "checkbox", checked: tripAsk[key] ? "" : false, onchange: e => { tripAsk[key] = e.target.checked; } }), ` ${says}`)),
    el("div", { class: "actions" }, plan));
  const told = [];
  if (tripProblem) told.push(el("div", { class: "error", role: "alert" }, `No trip was planned: ${tripProblem}`));
  if (trip) {
    const T = trip.total, days = trip.days;
    told.push(el("h2", { class: "next" }, `${days.length} ${days.length === 1 ? "day" : "days"}, ${km(T.metres / 1000)}`),
      el("div", { class: "sub" }, `${hoursOf(T.minutes)} of walking by Naismith's rule, with no rests; ${Math.round(T.up_m).toLocaleString("en")} m up. `
        + (T.twice_m > 50 ? `${km(T.twice_m / 1000)} of it is walked twice, or to get from one route to the next` : "Nothing is walked twice")
        + (T.back_m > 50 ? `; ${km(T.back_m / 1000)} back the same way` : "") + (T.off_route_m > 50 ? `; ${km(T.off_route_m / 1000)} along paths and roads off your routes` : "") + "."),
      ...(trip.notes || []).map(n => el("div", { class: "sub" }, n)),
      trip.credits && trip.credits.length ? el("div", { class: "sub" }, `${trip.credits.join(". ")}.`) : "",
      el("div", { class: "sub" }, "On the map each day is a line in its colour and ends at its number; S is where the trip begins. What is walked a second time is dashed."),
      trip.left_out.length ? el("div", { class: "sub" }, `Left out, as no way on foot leads there from the rest: ${trip.left_out.join(", ")}.`) : "",
      trip.not_on_foot.length ? el("div", { class: "sub" }, `Not walked where they go ${[...new Set(trip.not_on_foot.map(x => x.what))].join(" or ")}: ${trip.not_on_foot.map(x => x.code).join(", ")}.`) : "",
      el("ol", { class: "trip-days" }, ...days.map(d => el("li", { class: tripDay === d.day ? "hit current" : "hit", tabindex: 0, "data-day": d.day,
          onclick: () => { tripDay = tripDay === d.day ? null : d.day; render(); showDays(tripDay ? [d] : days); },
          onkeydown: e => { if (e.key === "Enter") e.target.click(); } },
        el("span", { class: "swatch", style: `background:${dayColour(d.day)}` }, String(d.day)),
        el("div", { class: "grow" },
          el("div", {}, `${placeOf(d.from)} – ${placeOf(d.to, d.no_hut)}`),
          el("div", { class: "sub" }, `${km(d.metres / 1000)}, ${d.up_m.toLocaleString("en")} m up, ${hoursOf(d.minutes)}`
            + (d.long ? ": longer than asked for, as no hut lies nearer" : "") + (d.no_hut ? ": a night in a tent, or a day to walk on from" : "")),
          d.routes.length ? el("div", { class: "sub" }, `Covers ${d.routes.join(", ")}`) : el("div", { class: "sub" }, "Only to get there"),
          el("ol", { class: "trip-steps" }, ...d.steps.map(s => el("li", { class: s.again ? "again" : "" }, `${s.says}, ${km(s.metres / 1000)}`)))),
        el("button", { class: "link", type: "button", title: "The day as a GPX file", onclick: e => { e.stopPropagation(); tripGpx([d]); } }, "GPX")))),
      el("div", { class: "actions" }, el("button", { class: "btn", onclick: () => tripGpx(days) }, "All days as one GPX file"),
        tripDay ? el("button", { class: "btn", onclick: () => { tripDay = null; render(); showDays(days); } }, "Show all days") : ""));
  }
  panel.replaceChildren(back, el("h2", { class: "area" }, `Trips in ${area}`),
    el("div", { class: "sub", style: "margin-bottom:8px" }, "A trip on foot that covers the routes you choose, for whoever is to inspect or mark them: as little as possible is walked twice, and the days end at huts."),
    form, ...told);
}
// The days on the map: each a line in its colour with its number where it ends. The routes fade behind them.
let tripLayers = false;
function drawTrip() {
  whenLoaded(() => {
    const on = view === "trips" && Boolean(trip) && tripFor === area;
    if (!tripLayers) {
      if (!on) return;
      map.addSource("trip", { type: "geojson", data: empty });
      map.addSource("trip-ends", { type: "geojson", data: empty });
      map.addLayer({ id: "trip-casing", type: "line", source: "trip", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#fff", "line-width": ["case", ["get", "chosen"], 9, 6] } });
      map.addLayer({ id: "trip", type: "line", source: "trip", filter: ["!", ["get", "again"]], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ["get", "colour"], "line-width": ["case", ["get", "chosen"], 5, 3], "line-opacity": ["case", ["get", "dim"], 0.35, 1] } });
      map.addLayer({ id: "trip-again", type: "line", source: "trip", filter: ["get", "again"], layout: { "line-cap": "butt", "line-join": "round" },
        paint: { "line-color": ["get", "colour"], "line-width": ["case", ["get", "chosen"], 5, 3], "line-opacity": ["case", ["get", "dim"], 0.35, 1], "line-dasharray": [1.5, 1.2] } });
      map.addLayer({ id: "trip-ends", type: "circle", source: "trip-ends", paint: { "circle-radius": 10, "circle-color": ["get", "colour"], "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
      map.addLayer({ id: "trip-numbers", type: "symbol", source: "trip-ends", layout: { "text-field": ["get", "day"], "text-size": 12, "text-font": ["Open Sans Semibold"], "text-allow-overlap": true }, paint: { "text-color": "#fff" } });
      for (const id of ["trip", "trip-again"]) {
        map.on("click", id, e => { const d = e.features[0].properties.n; tripDay = tripDay === d ? null : d; render(); });
        map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
      }
      tripLayers = true;
    }
    const days = on ? trip.days : [];
    map.getSource("trip").setData({ type: "FeatureCollection", features: days.flatMap(d => d.steps.map(s => ({ type: "Feature",
      properties: { n: d.day, colour: dayColour(d.day), again: s.again, chosen: tripDay === d.day, dim: tripDay !== null && tripDay !== d.day },
      geometry: { type: "LineString", coordinates: d.coords.slice(s.at[0], Math.max(s.at[1], s.at[0] + 1) + 1) } }))) });
    map.getSource("trip-ends").setData({ type: "FeatureCollection", features: [
      ...(days.length ? [{ type: "Feature", properties: { day: "S", colour: "#1f2a30" }, geometry: { type: "Point", coordinates: days[0].coords[0] } }] : []),
      ...days.map(d => ({ type: "Feature", properties: { day: String(d.day), colour: dayColour(d.day) }, geometry: { type: "Point", coordinates: d.coords[d.coords.length - 1] } }))] });
    for (const id of ["routes"]) if (map.getLayer(id)) map.setPaintProperty(id, "line-opacity", on ? 0.3 : 1);
  });
}
