// Photos from the routes (the feature `photos`): points on the map where they were taken, a row of them in a route
// and in an issue, and one shown large in a box of its own. Adding and changing them needs the editor or operations
// role as well; the server refuses the rest.
let photosAll = null, photosFor = null;      // the photos of the area, as fetched
let photoList = [], photoAt = 0;             // the photos the box steps through, and which is shown
let photoReport = null;                      // what the last photos added came to: { added, again, unplaced, failed }
let photoBusy = null;                        // photos being added: { done, of }
let photoMore = 120;                         // how many of the area's photos the part shows
const hasPhotos = () => (me.features || []).includes("photos");
const mayAddPhotos = () => hasPhotos() && (me.features || []).some(f => f === "routes" || f === "signs");
const photoUrl = (p, size) => new URL(`photos/${p.id}/${size}`, API).href;
const photoBox = document.getElementById("photo");
// The photos on the map can be switched off and on, by a button among the map's own; the choice is remembered.
let photosShown = localStorage.getItem("stiflyt2.photos") !== "off";
const photoSwitch = el("button", { type: "button", class: "photo-switch", onclick: () => {
  photosShown = !photosShown;
  localStorage.setItem("stiflyt2.photos", photosShown ? "on" : "off");
  drawPhotos();
} }, svg("svg", { width: 18, height: 18, viewBox: "0 0 18 18", "aria-hidden": "true", style: "display:block;margin:auto" },
  svg("path", { d: "M2.5 6.2c0-.9.7-1.6 1.6-1.6h1.8l1.2-1.8h3.8l1.2 1.8h1.8c.9 0 1.6.7 1.6 1.6v6.6c0 .9-.7 1.6-1.6 1.6H4.1c-.9 0-1.6-.7-1.6-1.6z", fill: "none", stroke: "currentColor", "stroke-width": 1.5, "stroke-linejoin": "round" }),
  svg("circle", { cx: 9, cy: 9.4, r: 2.6, fill: "none", stroke: "currentColor", "stroke-width": 1.5 })));
const photoControl = { onAdd() { this.box = el("div", { class: "maplibregl-ctrl maplibregl-ctrl-group", hidden: "" }, photoSwitch); return this.box; }, onRemove() { this.box.remove(); } };
map.addControl(photoControl, "top-right");

async function loadPhotos() {
  if (!hasPhotos() || !area) { photosAll = null; photosFor = null; return; }
  const asked = area;
  try { const got = await get("photos", { area: asked }); if (asked !== area) return; photosAll = got; photosFor = asked; } catch { /* the map is drawn without them */ }
  render();
}
// Photos sent from the page as they are: each its own request, so that one that fails does not stop the rest.
async function addPhotos(files, params) {
  const told = { added: 0, again: 0, unplaced: 0, failed: [] };
  photoBusy = { done: 0, of: files.length };
  hint.hidden = false;
  for (const f of files) {
    hint.replaceChildren(`Adding photos: ${photoBusy.done + 1} of ${files.length}…`);
    if (view === "photos") renderPhotos();
    const url = new URL("photos", API);
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) url.searchParams.set(k, v);
    try {
      const r = await fetch(url, { method: "POST", headers: { ...THROUGH, "Content-Type": f.type || "application/octet-stream" }, body: f });
      if (!r.ok) { let said = `${r.status}`; try { said = (await r.json()).detail; } catch {} told.failed.push(`${f.name}: ${said}`); }
      else { const p = await r.json(); if (p.again) told.again++; else { told.added++; if (p.lon === null) told.unplaced++; } }
    } catch (err) { told.failed.push(`${f.name}: ${err.message}`); }
    photoBusy.done++;
  }
  photoBusy = null; photoReport = told;
  hint.hidden = true;
  problem = told.failed.length && view !== "photos" ? `Not added: ${told.failed.join("; ")}` : null;
  if (shownRoute) shownRoute.photos = null;
  if (entryShown) entryShown.photos = null;
  await Promise.all([loadPhotos(), loadEntries()]);
}
// A button that asks for photos and adds them, placed where their cameras say, else at `params.lon`, `lat`.
function addPhotosButton(params, label = "Add photos") {
  if (!mayAddPhotos()) return "";
  const pick = el("input", { type: "file", accept: "image/*,.heic,.heif", multiple: "", hidden: "", onchange: e => { const files = [...e.target.files]; if (files.length) addPhotos(files, params); } });
  return el("span", {}, pick, el("button", { class: "btn", type: "button", title: "Placed where the camera says they were taken; without that, here", onclick: () => pick.click() }, label));
}
// The photos of an area as a part of its own: adding many at once (for operations and the route responsible), those
// that wait to be placed, and all of them, the newest taken first.
const photoFiles = list => [...list].filter(f => /^image\//.test(f.type) || /\.(heic|heif|jpe?g|png|webp)$/i.test(f.name));
function renderPhotos() {
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null, entry: null }) }, "‹ Areas"));
  const all = photosAll || [], waiting = all.filter(p => p.lon === null), R = photoReport;
  const many = (n, one, more) => `${n} ${n === 1 ? one : more}`;
  let drop = "";
  if (mayAddPhotos()) {
    const pick = el("input", { type: "file", accept: "image/*,.heic,.heif", multiple: "", hidden: "", onchange: e => { const f = photoFiles(e.target.files); if (f.length) addPhotos(f, { area }); } });
    const folder = el("input", { type: "file", webkitdirectory: "", multiple: "", hidden: "", onchange: e => { const f = photoFiles(e.target.files); if (f.length) addPhotos(f, { area }); } });
    drop = el("div", { class: "photo-drop", ondragover: e => { e.preventDefault(); e.currentTarget.classList.add("over"); }, ondragleave: e => e.currentTarget.classList.remove("over"),
        ondrop: e => { e.preventDefault(); e.currentTarget.classList.remove("over"); const f = photoFiles(e.dataTransfer.files); if (f.length) addPhotos(f, { area }); } },
      pick, folder,
      el("div", {}, photoBusy ? `Adding photos: ${photoBusy.done} of ${photoBusy.of} done…` : "Drop photos here, or"),
      photoBusy ? el("progress", { max: photoBusy.of, value: photoBusy.done, style: "width:100%" }) : el("div", { class: "actions", style: "justify-content:center" },
        el("button", { class: "btn main", type: "button", onclick: () => pick.click() }, "Choose photos"),
        el("button", { class: "btn", type: "button", title: "Every photo in a folder, as from a trip", onclick: () => folder.click() }, "Choose a folder")),
      el("div", { class: "sub" }, "Each is placed where its camera says it was taken. The same photo is never kept twice. JPEG, PNG, HEIC and WebP, up to 40 MB."));
  }
  panel.replaceChildren(back,
    el("h2", { class: "area" }, `Photos in ${area}`),
    el("div", { class: "sub", style: "margin-bottom:8px" }, `${many(all.length, "photo", "photos")} from the routes here. Only those who are signed in see them.`),
    drop,
    R ? el("div", { class: R.failed.length ? "error" : "said", role: "status", style: "margin:8px 0" },
      [R.added && `${many(R.added, "photo", "photos")} added`, R.again && `${many(R.again, "was", "were")} here already`, R.unplaced && `${R.unplaced} with no position from the camera`,
       R.failed.length && `${R.failed.length} not added`].filter(Boolean).join("; ") + ".",
      ...R.failed.map(f => el("div", { class: "sub" }, f))) : "",
    waiting.length ? el("h2", { class: "next" }, `${many(waiting.length, "photo waits", "photos wait")} to be placed`) : "",
    waiting.length ? el("div", { class: "sub" }, "Open one and choose \u201cPlace it on the map\u201d.") : "",
    waiting.length ? photoRow(waiting) : "",
    all.length ? el("h2", { class: "next" }, "All of them, the newest first") : "",
    all.length ? photoRow(all.slice(0, photoMore)) : el("div", { class: "sub" }, photosAll ? "None yet." : "Loading…"),
    all.length > photoMore ? el("div", { class: "actions" }, el("button", { class: "btn", onclick: () => { photoMore += 240; render(); } }, `Show more (${all.length - photoMore} left)`)) : "");
}
// A row of thumbnails; a click shows the photo large, with the others of the row to step through.
function photoRow(list) {
  return el("div", { class: "photo-row" }, ...list.map((p, i) => el("button", { class: "photo-thumb", type: "button", title: p.caption || "Show the photo", onclick: () => showPhoto(list, i) },
    el("img", { src: photoUrl(p, "thumb"), alt: p.caption || "A photo", loading: "lazy", width: 72, height: 72 }))));
}
const whenTaken = p => p.taken_at ? new Date(p.taken_at).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : null;

function showPhoto(list, i) {
  photoList = list; photoAt = i;
  drawPhoto();
  if (!photoBox.open) photoBox.showModal();
}
function openPhoto(id) {
  const list = photosAll || [], i = list.findIndex(p => p.id === id);
  if (i >= 0) showPhoto(list, i); else get(`photos/${id}`).then(p => showPhoto([p], 0)).catch(() => {});
}
function drawPhoto() {
  const p = photoList[photoAt];
  if (!p) { photoBox.close(); return; }
  const can = mayAddPhotos(), step = d => { photoAt = (photoAt + d + photoList.length) % photoList.length; drawPhoto(); };
  const caption = el("input", { type: "text", value: p.caption || "", maxlength: 500, placeholder: "What it shows", "aria-label": "What the photo shows" });
  const issue = p.entry && (entries || []).find(e => e.id === p.entry);
  const save = async fields => {
    try { const now = await send("PATCH", `photos/${p.id}`, fields); photoList[photoAt] = now; problem = null; }
    catch (err) { problem = err.message; }
    loadPhotos(); drawPhoto();
  };
  photoBox.replaceChildren(
    el("img", { class: "photo-large", src: photoUrl(p, "1600"), alt: p.caption || "A photo" }),
    el("div", { class: "photo-about" },
      can ? el("form", { onsubmit: e => { e.preventDefault(); save({ caption: caption.value }); } }, caption) : el("div", { class: "said" }, p.caption || ""),
      el("div", { class: "sub" }, (whenTaken(p) ? `Taken ${whenTaken(p)}, added by ${p.made_by}. ` : `Added by ${p.made_by}. `)
        + (p.placed === "camera" ? "Where the camera said it was." : p.placed === "hand" ? "Placed by hand." : "Not placed on the map yet.")),
      issue ? el("div", { class: "sub" }, "Shows the issue ", el("button", { class: "link", onclick: () => { photoBox.close(); openEntry(issue.id); } }, issue.text.length > 60 ? `${issue.text.slice(0, 60)}…` : issue.text)) : "",
      problem ? el("div", { class: "error" }, problem) : "",
      el("div", { class: "actions" },
        photoList.length > 1 ? el("button", { class: "btn", "aria-label": "The photo before", onclick: () => step(-1) }, "‹") : "",
        photoList.length > 1 ? el("span", { class: "sub" }, `${photoAt + 1} of ${photoList.length}`) : "",
        photoList.length > 1 ? el("button", { class: "btn", "aria-label": "The next photo", onclick: () => step(1) }, "›") : "",
        el("span", { class: "grow" }),
        p.lon !== null ? el("button", { class: "btn", onclick: () => { photoBox.close(); go([p.lon, p.lat], 15); } }, "Show where") : "",
        can ? el("button", { class: "btn", title: "Click on the map where it was taken", onclick: () => { photoBox.close(); placePhoto(p.id); } }, p.lon === null ? "Place it on the map" : "Move it") : "",
        el("a", { class: "btn", href: photoUrl(p, "original"), target: "_blank", rel: "noopener" }, "The original"),
        can ? el("button", { class: "btn quiet", title: "Undo brings it back", onclick: async () => {
          if (!confirm("Take this photo away? Undo brings it back.")) return;
          try { await send("DELETE", `photos/${p.id}`); photoList.splice(photoAt, 1); photoAt = Math.min(photoAt, photoList.length - 1); } catch (err) { problem = err.message; }
          await loadPhotos(); loadEntries(); drawPhoto();
        } }, "Take it away") : "",
        el("button", { class: "btn main", onclick: () => photoBox.close() }, "Close"))));
}
photoBox.addEventListener("keydown", e => { if (e.key === "ArrowLeft" && photoList.length > 1) { photoAt = (photoAt - 1 + photoList.length) % photoList.length; drawPhoto(); } if (e.key === "ArrowRight" && photoList.length > 1) { photoAt = (photoAt + 1) % photoList.length; drawPhoto(); } });
photoBox.addEventListener("click", e => { if (e.target === photoBox) photoBox.close(); });

// A circle of photos: a click comes nearer, until they part; photos taken at one spot are shown in the box together.
async function openGroup(group) {
  const source = map.getSource("photos"), id = group.properties.cluster_id;
  try {
    const zoom = await source.getClusterExpansionZoom(id);
    if (zoom <= 17 && zoom > map.getZoom()) { map.easeTo({ center: group.geometry.coordinates, zoom, animate: !calm }); return; }
    const leaves = await source.getClusterLeaves(id, 200, 0);
    const list = leaves.map(f => (photosAll || []).find(p => p.id === f.properties.id)).filter(Boolean);
    if (list.length) showPhoto(list, 0);
  } catch { /* the map is as it was */ }
}
// Where a photo was taken: the next click on the map.
function placePhoto(id) {
  placing = { photo: id };
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren("Click on the map where the photo was taken.", el("button", { class: "btn", onclick: stopPlacing }, "Cancel"));
}
async function placedPhoto(e) {
  const id = placing.photo;
  stopPlacing();
  try { await send("PATCH", `photos/${id}`, { lon: e.lngLat.lng, lat: e.lngLat.lat }); problem = null; } catch (err) { problem = err.message; }
  await loadPhotos();
}

// The photos on the map: a point where each was taken, among the routes and the issues.
function drawPhotos() {
  whenLoaded(() => {
    const here = hasPhotos() && (view === "routes" || view === "issues" || view === "photos"), on = here && photosShown && photosAll && photosFor === area;
    photoControl.box.hidden = !here;
    photoSwitch.setAttribute("aria-pressed", String(photosShown));
    photoSwitch.title = photosShown ? "Hide the photos on the map" : "Show the photos on the map";
    photoSwitch.setAttribute("aria-label", photoSwitch.title);
    for (const id of ["photos", "photo-groups", "photo-counts"]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
    map.getSource("photos").setData({ type: "FeatureCollection", features: (on ? photosAll : []).filter(p => p.lon !== null).map(p => ({ type: "Feature",
      properties: { id: p.id }, geometry: { type: "Point", coordinates: [p.lon, p.lat] } })) });
  });
}
