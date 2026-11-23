// Who owns the ground (the feature `landowners`): a click on the map gives the property there, and a route the
// properties it crosses, in order. Who owns them is asked of Kartverket's register when asked for here, and kept
// by nobody: the service keeps only that it was asked, by whom.
const hasLandowners = () => (me.features || []).includes("landowners");
let ownedAt = null;          // the property clicked: { lon, lat, found, problem, owners: null | "asking" | true }
let ownedAlong = null;       // the properties along the open route: { route, found, problem, owners: null | "asking" | true }
let ownedHot = null;         // the property pointed at in the route's list: its stretches are drawn
const ownedPopup = new maplibregl.Popup({ closeButton: true, closeOnClick: false, maxWidth: "340px", className: "owned-box" });
ownedPopup.on("close", () => { if (ownedAt) { ownedAt = null; drawLandowners(); } });

function ownerRows(owners) {
  if (!owners.length) return el("div", { class: "sub" }, "The register names nobody.");
  return el("ul", { class: "owners" }, ...owners.map(o => el("li", {},
    el("strong", {}, o.name || "No name given"), o.share && o.share !== "1/1" ? el("span", { class: "sub" }, ` ${o.share}`) : "",
    o.role && o.role !== "owner" ? el("span", { class: "tag", title: o.registered === false ? "Not registered as the title holder" : "" }, ` ${o.role}`) : "",
    o.address ? el("div", { class: "sub" }, o.address) : "",
    o.org_number ? el("div", { class: "sub" }, `Org. no. ${o.org_number}`) : "")));
}
const unitTitle = u => [el("strong", {}, u.label), u.name ? ` ${u.name}` : "", u.municipality ? el("span", { class: "sub" }, `, ${u.municipality}`) : ""];

// The property at a spot: asked for by a click on the map that is not on a route, among the landowners.
async function lookAt(lngLat) {
  const mine = ownedAt = { lon: lngLat.lng, lat: lngLat.lat, found: null, problem: null, owners: null };
  ownedPopup.setLngLat(lngLat).setDOMContent(el("div", { class: "sub" }, "Looking it up…")).addTo(map);
  try { mine.found = await get("landowners/at", { lon: mine.lon, lat: mine.lat }); }
  catch (err) { mine.problem = err.message; }
  if (ownedAt === mine) { showAt(); drawLandowners(); }
}
async function ownersAt() {
  const mine = ownedAt;
  mine.owners = "asking"; showAt();
  try { const got = await get("landowners/at", { lon: mine.lon, lat: mine.lat, owners: true }); mine.found = got; mine.owners = true; }
  catch (err) { mine.problem = err.message; mine.owners = null; }
  if (ownedAt === mine) showAt();
}
function showAt() {
  const a = ownedAt;
  if (!a) return;
  const f = a.found;
  ownedPopup.setDOMContent(a.problem && !f ? el("div", { class: "sub" }, a.problem) : !f.parcel ? el("div", { class: "sub" }, f.said) : el("div", {},
    ...f.units.map(u => el("div", { class: "owned-unit" }, ...unitTitle(u),
      u.type && u.type !== "Grunneiendom" ? el("div", { class: "sub" }, u.type) : "",
      u.owners ? ownerRows(u.owners) : "")),
    f.units.length ? "" : el("div", { class: "sub" }, "A parcel with no property unit."),
    el("div", { class: "sub" }, [f.area_m2 ? `${f.area_m2 >= 10000 ? `${(f.area_m2 / 10000).toLocaleString("en", { maximumFractionDigits: 1 })} ha` : `${f.area_m2.toLocaleString("en")} m²`}` : "",
      f.disputed ? "its bounds are disputed" : ""].filter(Boolean).join(", ")),
    f.owners_unavailable ? el("div", { class: "error" }, f.owners_unavailable) : "",
    a.problem ? el("div", { class: "error" }, a.problem) : "",
    f.units.some(u => u.id) && a.owners !== true ? el("div", { class: "actions" },
      el("button", { class: "btn", disabled: a.owners === "asking", onclick: ownersAt }, a.owners === "asking" ? "Asking the register…" : "Who owns it")) : ""));
}

// The properties a route crosses, in its panel among the landowners: fetched when the route is opened there.
function routeLandowners(r) {
  if (!ownedAlong || ownedAlong.route !== r.id) {
    const mine = ownedAlong = { route: r.id, found: null, problem: null, owners: null };
    ownedHot = null;
    get("landowners/along", { route: r.id }).then(f => { mine.found = f; }).catch(err => { mine.problem = err.message; })
      .then(() => { if (ownedAlong === mine && route === r.code) renderRoute(); });
  }
  const A = ownedAlong, f = A.found;
  const ask = async () => {
    const mine = ownedAlong;
    mine.owners = "asking"; renderRoute();
    try { mine.found = await get("landowners/along", { route: r.id, owners: true }); mine.owners = true; }
    catch (err) { mine.problem = err.message; mine.owners = null; }
    if (ownedAlong === mine && route === r.code) renderRoute();
  };
  const point = p => { ownedHot = p; drawLandowners(); };
  const span = s => `${kmOf(s.from_m)}–${kmOf(s.to_m)} km`;
  return [el("h2", { class: "next" }, "Landowners"),
    !f ? el("div", { class: A.problem ? "error" : "sub" }, A.problem || "Working out the properties it crosses…") : el("div", {},
      el("div", { class: "sub" }, `${f.properties.filter(p => p.id).length} ${f.properties.filter(p => p.id).length === 1 ? "property" : "properties"} along its ${km(f.metres / 1000)}, in the order it crosses them.`),
      f.not_loaded.length ? el("div", { class: "sub" }, `The property map of ${f.not_loaded.join(", ")} is not loaded: what lies there is not known.`) : "",
      el("ul", { class: "owned-along" }, ...f.properties.map(p => el("li", { class: "hit", tabindex: 0, onmouseenter: () => point(p), onmouseleave: () => point(null), onfocus: () => point(p), onblur: () => point(null) },
        el("div", {}, ...unitTitle(p), el("span", { class: "far" }, `${p.metres.toLocaleString("en")} m`)),
        el("div", { class: "sub" }, p.spans.map(span).join(", ")),
        p.owners ? ownerRows(p.owners) : ""))),
      f.unknown.length ? el("div", { class: "sub" }, `No parcel in the property map ${f.unknown.map(s => `from ${span(s).replace("–", " to ")}`).join(", ")}.`) : "",
      f.owners_unavailable ? el("div", { class: "error" }, f.owners_unavailable) : "",
      A.problem ? el("div", { class: "error" }, A.problem) : "",
      el("div", { class: "actions" },
        f.properties.some(p => p.id) && A.owners !== true ? el("button", { class: "btn", disabled: A.owners === "asking", onclick: ask,
          title: "Asked of Kartverket's property register; that you asked is kept, not what it said" }, A.owners === "asking" ? "Asking the register…" : "Look up who owns them") : "",
        f.properties.some(p => p.id) ? el("a", { class: "btn", href: new URL(`landowners/along.xlsx?route=${encodeURIComponent(r.id)}`, API).href,
          title: "The properties and their owners as an Excel list. Personal data: for the work on the route" }, "Download the list (Excel)") : ""),
      el("div", { class: "sub", style: "margin-top:6px" }, "From Kartverket's property map and property register. Names and addresses are personal data: for the work on the route, not to be passed on."))];
}

function renderLandowners() {
  const back = el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ area: null, route: null, site: null, entry: null, report: null }) }, "‹ Areas"));
  const rows = over ? over.routes.slice().sort((a, b) => a.code.localeCompare(b.code, "en", { numeric: true })) : [];
  panel.replaceChildren(back,
    el("h2", { class: "area" }, `Landowners in ${area}`),
    el("div", { class: "sub" }, "Click the map where you want to know who owns the ground, or open a route for the properties it crosses, in order."),
    !over ? el("div", { class: "sub" }, "Loading…") : el("ul", { style: "margin-top:8px" }, ...rows.map(r => el("li", { class: "hit", tabindex: 0, onclick: () => showRoute(r.code), onkeydown: enter(() => showRoute(r.code)),
      onmouseenter: () => pointRoute(r.id), onmouseleave: () => pointRoute(null) },
      el("span", { class: "code" }, r.code), el("span", { class: "grow" }, r.name || ""), el("span", { class: "far" }, km(r.on_foot_m / 1000))))));
}

// On the map: the parcel clicked, and where the open route crosses the property pointed at in its list.
function drawLandowners() {
  whenLoaded(() => {
    const on = view === "landowners" && hasLandowners();
    if (!on && ownedAt) { ownedAt = null; ownedPopup.remove(); }
    const features = [];
    if (on && ownedAt && ownedAt.found && ownedAt.found.outline) features.push({ type: "Feature", properties: {}, geometry: ownedAt.found.outline });
    if (on && route && ownedHot) features.push(...ownedHot.lines.map(coordinates => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates } })));
    map.getSource("owned").setData({ type: "FeatureCollection", features });
  });
}
