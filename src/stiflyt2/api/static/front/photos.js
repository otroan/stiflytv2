// Photos from the routes (the feature `photos`): points on the map where they were taken, a row of them in a route
// and in an issue, and one shown large in a box of its own. Adding and changing them needs the editor or operations
// role as well; the server refuses the rest.
let photosAll = null, photosFor = null;      // the photos of the area, as fetched
let photoList = [], photoAt = 0;             // the photos the box steps through, and which is shown
const hasPhotos = () => (me.features || []).includes("photos");
const mayAddPhotos = () => hasPhotos() && (me.features || []).some(f => f === "routes" || f === "signs");
const photoUrl = (p, size) => new URL(`photos/${p.id}/${size}`, API).href;
const photoBox = document.getElementById("photo");

async function loadPhotos() {
  if (!hasPhotos() || !area) { photosAll = null; photosFor = null; return; }
  const asked = area;
  try { const got = await get("photos", { area: asked }); if (asked !== area) return; photosAll = got; photosFor = asked; } catch { /* the map is drawn without them */ }
  render();
}
// Photos sent from the page as they are: each its own request, so that one that fails does not stop the rest.
async function addPhotos(files, params) {
  const failed = [];
  let n = 0;
  hint.hidden = false;
  for (const f of files) {
    hint.replaceChildren(`Adding photos: ${++n} of ${files.length}…`);
    const url = new URL("photos", API);
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) url.searchParams.set(k, v);
    try {
      const r = await fetch(url, { method: "POST", headers: { ...THROUGH, "Content-Type": f.type || "application/octet-stream" }, body: f });
      if (!r.ok) { let said = `${r.status}`; try { said = (await r.json()).detail; } catch {} failed.push(`${f.name}: ${said}`); }
    } catch (err) { failed.push(`${f.name}: ${err.message}`); }
  }
  hint.hidden = true;
  problem = failed.length ? `Not added: ${failed.join("; ")}` : null;
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
    const on = hasPhotos() && photosAll && photosFor === area && (view === "routes" || view === "issues");
    map.setLayoutProperty("photos", "visibility", on ? "visible" : "none");
    map.getSource("photos").setData({ type: "FeatureCollection", features: (on ? photosAll : []).filter(p => p.lon !== null).map(p => ({ type: "Feature",
      properties: { id: p.id }, geometry: { type: "Point", coordinates: [p.lon, p.lat] } })) });
  });
}
