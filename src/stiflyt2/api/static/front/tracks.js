// GPX tracks walked on the routes (the feature `tracks`): a lens of their own, with the area's tracks on the map and
// a track open: what it comes to, what cleaning took out, and parts cut out by hand. Adding and changing them needs
// the editor or operations role as well; the server refuses the rest.
const TRACK_PINK = "#d81b60";
let tracksAll = null, tracksFor = null, trackLines = null;     // the area's tracks, and their lines to draw
let trackOpen = null;              // the id of the track open
let trackShown = null;             // the track open as fetched: its lines, its stops and its cuts
let trackBusy = null;              // files being added: { done, of }
let trackReport = null;            // what the last files added came to: { added, again, failed }
let trackUndo = null;              // your latest decision in operations, which an undo would take back
let trackRenaming = false;
const hasTracks = () => (me.features || []).includes("tracks");
const mayAddTracks = () => hasTracks() && (me.features || []).some(f => f === "routes" || f === "signs");
const lasting = s => { const m = Math.round(s / 60); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`; };
const walkedOn = t => t.started_at ? new Date(t.started_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : null;
const clock = s => new Date(s).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

whenLoaded(() => {
  for (const id of ["tracks", "track-came", "track-open", "track-stops", "track-pick"]) map.addSource(id, { type: "geojson", data: empty });
  const width = ["interpolate", ["linear"], ["zoom"], 8, 1.5, 15, 3];
  // Under the places and the sign sites: the area's tracks thin, the one open strong, and the track as it came dashed
  // under it, so that what cleaning and cutting took out shows.
  map.addLayer({ id: "track-came", type: "line", source: "track-came", layout: { visibility: "none", "line-join": "round" },
    paint: { "line-color": "#6b7780", "line-width": 1.5, "line-dasharray": [2, 2] } }, "places");
  map.addLayer({ id: "tracks", type: "line", source: "tracks", layout: { visibility: "none", "line-cap": "round", "line-join": "round" },
    paint: { "line-color": TRACK_PINK, "line-width": width, "line-opacity": ["case", ["get", "open"], 0, 0.75] } }, "places");
  map.addLayer({ id: "track-open", type: "line", source: "track-open", layout: { visibility: "none", "line-cap": "round", "line-join": "round" },
    paint: { "line-color": TRACK_PINK, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3, 15, 5] } }, "places");
  map.addLayer({ id: "track-stops", type: "circle", source: "track-stops", layout: { visibility: "none" },
    paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 3, 15, 6], "circle-color": "#fff", "circle-stroke-color": TRACK_PINK, "circle-stroke-width": 2 } }, "places");
  map.addLayer({ id: "track-pick", type: "circle", source: "track-pick",
    paint: { "circle-radius": 7, "circle-color": "#fff", "circle-stroke-color": TRACK_PINK, "circle-stroke-width": 3 } });
});

async function loadTracks() {
  if (!hasTracks() || !area) { tracksAll = trackLines = tracksFor = null; return; }
  const asked = area;
  try {
    const [list, drawn] = await Promise.all([get("tracks", { area: asked }), get("tracks.geojson", { area: asked })]);
    if (asked !== area) return;
    tracksAll = list; trackLines = drawn; tracksFor = asked;
  } catch { /* the map is drawn without them */ }
  if (mayAddTracks()) { try { trackUndo = (await get("tracks/undo")).last; } catch { trackUndo = null; } }
  render();
}
async function fetchTrack(id) {
  try { const got = await get(`tracks/${id}`); if (trackOpen !== id) return; trackShown = got; problem = null; fitTrack(got); }
  catch (err) { if (trackOpen === id) trackShown = { id, problem: err.message }; }
  if (view === "tracks") render();
}
function fitTrack(t) {
  const all = t.line ? t.line.coordinates.flat() : [];
  if (!all.length) return;
  const lon = all.map(c => c[0]), lat = all.map(c => c[1]);
  map.fitBounds([[Math.min(...lon), Math.min(...lat)], [Math.max(...lon), Math.max(...lat)]], { padding: 60, maxZoom: 15, animate: !calm });
}
function openTrack(id) { visit({ view: "tracks", track: id, route: null, site: null, entry: null, report: null }); }
// A change of yours to a track: made, then the area's tracks and the track open fetched anew.
async function trackChange(request) {
  problem = null;
  try { const got = await request(); if (got && got.id === trackOpen) trackShown = got; } catch (err) { problem = err.message; }
  if (trackOpen && (!trackShown || trackShown.id !== trackOpen || !trackShown.line)) trackShown = null;
  await loadTracks();
  if (trackOpen && !trackShown) fetchTrack(trackOpen);
}
function undoTrack() {
  if (!trackUndo) return;
  const txid = trackUndo.txid;
  trackUndo = null;
  trackChange(async () => { await send("POST", "tracks/undo", { txid }); trackShown = null; });
}

// GPX files sent from the page as they are: each its own request, so that one that fails does not stop the rest.
const trackFiles = list => [...list].filter(f => /\.gpx$/i.test(f.name) || /gpx/.test(f.type));
async function addTracks(files) {
  const told = { added: 0, again: 0, failed: [] };
  trackBusy = { done: 0, of: files.length };
  render();
  for (const f of files) {
    const url = new URL("tracks", API);
    url.searchParams.set("area", area);
    url.searchParams.set("filename", f.name);
    try {
      const r = await fetch(url, { method: "POST", headers: { ...THROUGH, "Content-Type": "application/gpx+xml" }, body: f });
      if (!r.ok) { let said = `${r.status}`; try { said = (await r.json()).detail; } catch {} told.failed.push(`${f.name}: ${said}`); }
      else { const t = await r.json(); if (t.again) told.again++; else told.added++; }
    } catch (err) { told.failed.push(`${f.name}: ${err.message}`); }
    trackBusy.done++;
    if (view === "tracks") render();
  }
  trackBusy = null; trackReport = told;
  await loadTracks();
}

function renderTracks() {
  if (trackOpen) { renderTrack(); return; }
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null, entry: null }) }, "‹ Areas"));
  const all = tracksAll || [], R = trackReport, many = (n, one, more) => `${n} ${n === 1 ? one : more}`;
  let drop = "";
  if (mayAddTracks()) {
    const pick = el("input", { type: "file", accept: ".gpx,application/gpx+xml", multiple: "", hidden: "", onchange: e => { const f = trackFiles(e.target.files); if (f.length) addTracks(f); } });
    drop = el("div", { class: "photo-drop", ondragover: e => { e.preventDefault(); e.currentTarget.classList.add("over"); }, ondragleave: e => e.currentTarget.classList.remove("over"),
        ondrop: e => { e.preventDefault(); e.currentTarget.classList.remove("over"); const f = trackFiles(e.dataTransfer.files); if (f.length) addTracks(f); } },
      pick,
      el("div", {}, trackBusy ? `Adding tracks: ${trackBusy.done} of ${trackBusy.of} done…` : "Drop GPX files here, or"),
      trackBusy ? el("progress", { max: trackBusy.of, value: trackBusy.done, style: "width:100%" }) : el("div", { class: "actions", style: "justify-content:center" },
        el("button", { class: "btn main", type: "button", onclick: () => pick.click() }, "Choose GPX files")),
      el("div", { class: "sub" }, "Each is kept as it came, and cleaned: jumps and stops taken out. The same file is never kept twice. Up to 30 MB each."));
  }
  panel.replaceChildren(back,
    el("h2", { class: "area" }, `Tracks in ${area}`),
    el("div", { class: "sub", style: "margin-bottom:8px" }, "GPX tracks walked on the routes here, to compare a route with. Only those who are signed in see them."),
    drop,
    R ? el("div", { class: R.failed.length ? "error" : "said", role: "status", style: "margin:8px 0" },
      [R.added && `${many(R.added, "track", "tracks")} added`, R.again && `${many(R.again, "was", "were")} here already`, R.failed.length && `${R.failed.length} not added`].filter(Boolean).join("; ") + ".",
      ...R.failed.map(f => el("div", { class: "sub" }, f))) : "",
    all.length ? el("ul", { style: "margin-top:8px" }, ...all.map(t => el("li", { class: "hit track", tabindex: 0, onclick: () => openTrack(t.id), onkeydown: enter(() => openTrack(t.id)) },
      el("span", { class: "grow" }, el("span", { class: "said clip" }, t.name),
        el("span", { class: "sub" }, ` ${[walkedOn(t) || (t.origin.startsWith("v1:") ? "from v1, a line alone" : "no times"), t.moving_s ? `${lasting(t.moving_s)} walking` : null, `added by ${t.made_by}`].filter(Boolean).join(", ")}`)),
      el("span", { class: "far" }, t.length_m === null ? "" : km(t.length_m / 1000)))))
      : el("div", { class: "sub" }, tracksAll ? "None yet." : "Loading…"));
}
function renderTrack() {
  const listed = (tracksAll || []).find(x => x.id === trackOpen);
  if (!trackShown || trackShown.id !== trackOpen) { trackShown = { id: trackOpen }; fetchTrack(trackOpen); }
  const t = trackShown.name ? trackShown : listed;
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ track: null }) }, `‹ Tracks in ${area.toUpperCase()}`));
  const told = problem ? el("div", { class: "error", role: "alert" }, problem) : "";
  if (!t) { panel.replaceChildren(back, trackShown.problem ? el("div", { class: "error" }, `Could not get the track: ${trackShown.problem}`) : el("div", { class: "sub" }, "Loading…")); return; }
  const can = mayAddTracks(), c = t.cleaned || {}, fact = (name, ...said) => [el("dt", {}, name), el("dd", {}, ...said)];
  const name = trackRenaming && can ? el("form", { class: "rename", onsubmit: e => { e.preventDefault(); const v = e.target.querySelector("input").value.trim(); trackRenaming = false;
      if (v && v !== t.name) trackChange(() => send("PATCH", `tracks/${t.id}`, { name: v }).then(got => { trackShown = { ...trackShown, name: got.name }; return null; })); else render(); } },
    el("input", { type: "text", value: t.name, maxlength: 200, "aria-label": "What the track is called" }), el("button", { class: "btn main" }, "Keep"))
    : el("h2", {}, t.name);
  const cleaned = [c.jumps && `${c.jumps} ${c.jumps === 1 ? "point that jumped away was" : "points that jumped away were"} taken out`,
    c.stops && `${c.stops} ${c.stops === 1 ? "stop" : "stops"} of two minutes or more (${lasting(c.stopped_s)} in all) made one point each`,
    c.rides && `${c.rides === 1 ? "a ride" : `${c.rides} rides`} faster than one walks (${km(c.ride_m / 1000)}: a car, a boat, a lift) left out`,
    c.gaps && `cut in two where the device was off (${c.gaps})`, c.short && "a bit too short to be a part left out"].filter(Boolean);
  const cuts = trackShown.cuts || [];
  panel.replaceChildren(back, told, name,
    el("dl", { class: "facts" },
      ...fact("Walked", t.started_at ? `${walkedOn(t)}, ${clock(t.started_at)}–${clock(t.ended_at)}` : t.origin.startsWith("v1:") ? "From v1: a line alone, with no times or heights" : "The file has no times"),
      ...fact("Length", t.length_m === null ? "not worked out yet" : `${km(t.length_m / 1000)}${t.parts > 1 ? `, in ${t.parts} parts` : ""}`),
      ...(t.has_time && t.total_s !== null ? fact("Time", `${lasting(t.total_s)} in all, ${lasting(t.moving_s)} walking`) : []),
      ...(t.has_height && t.up_m !== null ? fact("Climb", `${Math.round(t.up_m).toLocaleString("en")} m up, ${Math.round(t.down_m).toLocaleString("en")} m down, from ${Math.round(t.low_m).toLocaleString("en")} to ${Math.round(t.high_m).toLocaleString("en")} m`,
        el("div", { class: "sub" }, "By the device's own heights")) : []),
      ...fact("Added", `${whenSaid(t.made_at)} by ${t.made_by}`)),
    el("div", { class: "sub" }, cleaned.length ? `Cleaned: ${cleaned.join("; ")}. The track as it came is the dashed line.` : "Nothing was taken out in cleaning."),
    cuts.length ? el("h2", { class: "next" }, "Cut out by hand") : "",
    cuts.length ? el("ul", {}, ...cuts.map(x => el("li", { class: "cut" },
      el("span", { class: "grow" }, `From ${kmOf(x.from_m)} to ${kmOf(x.to_m)} km of the track as it came (${km((x.to_m - x.from_m) / 1000)})`),
      can ? el("button", { class: "btn quiet", onclick: () => trackChange(() => send("DELETE", `tracks/${t.id}/cuts/${x.n}`)) }, "Put it back") : ""))) : "",
    el("div", { class: "actions" },
      can ? el("button", { class: "btn", title: "A detour, or the drive home: click where it begins and where it ends", onclick: cutTrack }, "Cut out a part") : "",
      can ? el("button", { class: "btn", onclick: () => { trackRenaming = true; render(); } }, "Rename") : "",
      t.has_file ? el("a", { class: "btn", href: new URL(`tracks/${t.id}/gpx`, API).href }, "The file as it came") : "",
      el("a", { class: "btn", href: new URL(`tracks/${t.id}/gpx?cleaned=true`, API).href }, "Cleaned, as GPX"),
      can ? el("button", { class: "btn quiet", title: "Undo brings it back", onclick: () => { if (confirm("Take this track away? Undo brings it back.")) trackChange(async () => { await send("DELETE", `tracks/${t.id}`); visit({ track: null }); }); } }, "Take it away") : ""));
  if (trackRenaming) panel.querySelector("form.rename input").focus();
}
// A part to cut out: where it begins, then where it ends, clicked on the track.
function cutTrack() {
  placing = { trackCut: true, from: null };
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren("Click on the track where the part to cut out begins.", el("button", { class: "btn", onclick: () => { stopPlacing(); drawTracks(); } }, "Cancel"));
}
function placedTrackCut(e) {
  const at = [e.lngLat.lng, e.lngLat.lat];
  if (!placing.from) {
    placing.from = at;
    hint.replaceChildren("And where it ends.", el("button", { class: "btn", onclick: () => { stopPlacing(); drawTracks(); } }, "Cancel"));
    drawTracks();
    return;
  }
  const from = placing.from, id = trackOpen;
  stopPlacing();
  trackChange(() => send("POST", `tracks/${id}/cuts`, { from_point: from, to_point: at }));
}

function drawTracks() {
  whenLoaded(() => {
    if (!map.getLayer("tracks")) return;
    const on = hasTracks() && view === "tracks" && tracksFor === area, shown = on && trackOpen && trackShown && trackShown.id === trackOpen && trackShown.line;
    for (const id of ["tracks", "track-came", "track-open", "track-stops"]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    map.getSource("tracks").setData(on && trackLines ? { type: "FeatureCollection", features: trackLines.features.map(f => ({ ...f, properties: { ...f.properties, open: f.properties.id === trackOpen } })) } : empty);
    map.getSource("track-open").setData(shown ? { type: "Feature", properties: {}, geometry: trackShown.line } : empty);
    map.getSource("track-came").setData(shown && trackShown.came ? { type: "Feature", properties: {}, geometry: trackShown.came } : empty);
    map.getSource("track-stops").setData(shown ? { type: "FeatureCollection", features: trackShown.stops.map(s => ({ type: "Feature", properties: { s: s.s, at: s.at }, geometry: { type: "Point", coordinates: [s.lon, s.lat] } })) } : empty);
    map.getSource("track-pick").setData(placing && placing.trackCut && placing.from ? { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: placing.from } } : empty);
  });
}
