// The front page's map: the routes in use, the one open, the sign sites, and what a click on it means.
const empty = { type: "FeatureCollection", features: [] };
const GLACIER = "#1f6fc5";      // the blue a route is drawn in over a glacier
const NOTICE_RED = "#c62828", ISSUE_AMBER = "#ef8f00", ISSUE_DONE = "#9aa5ad";     // an issue: a notice to walkers, work, done
const PHOTO_BLUE = "#3949ab";   // where a photo was taken
const OWNED_PURPLE = "#7b3fa0"; // a property: its parcel, and where a route crosses it
const OPEN_WIDTH = ["interpolate", ["linear"], ["zoom"], 8, 3.5, 15, 6];      // the open route
const SINCE_WIDTH = ["interpolate", ["linear"], ["zoom"], 8, 2.5, 15, 6];     // how long since a stretch was done

const map = new maplibregl.Map({
  container: "map", hash: true, center: [7.6, 61.7], zoom: 9, maxZoom: 19,
  style: {
    version: 8, glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: {
      ...baseSources(),
      routes: { type: "geojson", data: empty }, sites: { type: "geojson", data: empty }, rays: { type: "geojson", data: empty },
      places: { type: "geojson", data: empty }, entries: { type: "geojson", data: empty }, "entry-lines": { type: "geojson", data: empty }, "entry-pick": { type: "geojson", data: empty }, since: { type: "geojson", data: empty }, owned: { type: "geojson", data: empty },
      // The property map, from zoom 13: drawn among the owners, where a click asks who owns the ground.
      parcels: { type: "vector", minzoom: 13, maxzoom: 15, tiles: [`${API}tiles/parcels/{z}/{x}/{y}.mvt`] }, photos: { type: "geojson", data: empty, cluster: true, clusterRadius: 42, clusterMaxZoom: 17 },
    },
    layers: [
      ...baseLayers(),
      { id: "route-hot", type: "line", source: "routes", filter: ["in", "|none|", ["get", "ids"]], layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#f5c400", "line-width": 9 } },
      // An issue along a stretch is a band under the route (the feature `issues`): red for a notice to walkers.
      { id: "entry-lines", type: "line", source: "entry-lines", layout: { visibility: "none", "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ["case", ["!", ["get", "open"]], ISSUE_DONE, ["get", "notice"], NOTICE_RED, ISSUE_AMBER],
                 "line-width": ["interpolate", ["linear"], ["zoom"], 8, 5, 15, 14], "line-opacity": ["case", ["get", "chosen"], 0.7, 0.4] } },
      // The parts of an issue while they are chosen: the same band, stronger.
      { id: "entry-pick", type: "line", source: "entry-pick", filter: ["==", ["geometry-type"], "LineString"], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ISSUE_AMBER, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 6, 15, 16], "line-opacity": 0.7 } },
      // A property (the feature `landowners`): every parcel, faintly, among the owners; the parcel clicked, under the routes.
      { id: "parcels-fill", type: "fill", source: "parcels", "source-layer": "parcels", minzoom: 13, layout: { visibility: "none" },
        paint: { "fill-color": OWNED_PURPLE, "fill-opacity": 0.04 } },
      { id: "parcels-edge", type: "line", source: "parcels", "source-layer": "parcels", minzoom: 13, layout: { visibility: "none" },
        paint: { "line-color": OWNED_PURPLE, "line-opacity": 0.35, "line-width": ["interpolate", ["linear"], ["zoom"], 13, 0.5, 16, 1.2] } },
      { id: "owned-fill", type: "fill", source: "owned", filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]], paint: { "fill-color": OWNED_PURPLE, "fill-opacity": 0.12 } },
      { id: "owned-edge", type: "line", source: "owned", filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]], paint: { "line-color": OWNED_PURPLE, "line-width": 2 } },
      // Over a glacier a route is blue round dots: there is no path there, and it is crossed as the ice lies.
      { id: "routes", type: "line", source: "routes", filter: ["!=", ["get", "kind"], "glacier"], paint: { "line-color": css("--sign"), "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.2, 15, 3], "line-opacity": 0.75 } },
      { id: "routes-ice", type: "line", source: "routes", filter: ["==", ["get", "kind"], "glacier"], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": GLACIER, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 15, 5], "line-dasharray": [0, 2], "line-opacity": 0.75 } },
      // How long since each stretch was inspected, or had a work party (the feature `reports`): the latest on top.
      { id: "since", type: "line", source: "since", layout: { "line-cap": "round", "line-join": "round", "line-sort-key": ["get", "t"] },
        paint: { "line-color": ["get", "colour"], "line-width": SINCE_WIDTH } },
      { id: "route-open", type: "line", source: "routes", filter: ["in", "|none|", ["get", "ids"]], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": css("--sign"), "line-width": OPEN_WIDTH } },
      { id: "route-open-ice", type: "line", source: "routes", filter: ["in", "|none|", ["get", "ids"]], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": GLACIER, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 5, 15, 8], "line-dasharray": [0, 1.8] } },
      // Where the route open crosses the property pointed at in its list.
      { id: "owned-along", type: "line", source: "owned", filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": OWNED_PURPLE, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 4, 15, 9], "line-opacity": 0.85 } },
      { id: "places", type: "circle", source: "places", minzoom: 10, paint: { "circle-radius": 3, "circle-color": "#fff", "circle-stroke-color": css("--ink"), "circle-stroke-width": 1.5 } },
      { id: "place-names", type: "symbol", source: "places", minzoom: 12,
        layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Semibold"], "text-size": 11, "text-offset": [0, 1], "text-anchor": "top" },
        paint: { "text-color": css("--muted"), "text-halo-color": "#fff", "text-halo-width": 1.5 } },
      { id: "rays-hot", type: "line", source: "rays", filter: ["==", ["get", "hot"], true], paint: { "line-color": "#f5c400", "line-width": 9 } },
      { id: "rays", type: "line", source: "rays", paint: { "line-color": css("--ink"), "line-width": 3 } },
      { id: "ray-text", type: "symbol", source: "rays", minzoom: 15,
        layout: { "symbol-placement": "line-center", "text-field": ["get", "text"], "text-font": ["Open Sans Semibold"], "text-size": 12, "text-offset": [0, -0.9], "text-allow-overlap": true },
        paint: { "text-halo-color": "#fff", "text-halo-width": 2 } },
      { id: "sites-small", type: "circle", source: "sites", minzoom: 10, layout: { visibility: "none" },
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 15, 5], "circle-color": "#fff", "circle-stroke-color": css("--sign"), "circle-stroke-width": 1.5 } },
      { id: "sites", type: "circle", source: "sites",
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 4, 14, 8],
                 // What is going on at a site: signs on the order, signs ordered and not up yet, or no signs there at all.
                 "circle-color": ["match", ["get", "state"], "order", "#e65100", "waiting", "#00796b", "none", "#b9c1c7", "#fff"],
                 "circle-stroke-color": ["case", ["get", "open"], css("--ink"), css("--sign")], "circle-stroke-width": ["case", ["get", "open"], 3.5, 2] } },
      { id: "site-names", type: "symbol", source: "sites", minzoom: 10,
        layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Semibold"], "text-size": 12, "text-offset": [0, 1.2], "text-anchor": "top" },
        paint: { "text-halo-color": "#fff", "text-halo-width": 1.5 } },
      // An issue at a spot, or of a whole route (at its middle): amber, red for a notice, grey when done.
      { id: "entries", type: "circle", source: "entries", layout: { visibility: "none" },
        paint: { "circle-radius": ["case", ["get", "chosen"], 9, ["get", "open"], 6.5, 4.5],
                 "circle-color": ["case", ["!", ["get", "open"]], ISSUE_DONE, ["get", "notice"], NOTICE_RED, ISSUE_AMBER],
                 "circle-stroke-color": ["case", ["get", "chosen"], css("--ink"), "#fff"], "circle-stroke-width": ["case", ["get", "chosen"], 3, 1.5] } },
      // Where a photo was taken (the feature `photos`); photos near each other are one circle with their number.
      { id: "photo-groups", type: "circle", source: "photos", filter: ["has", "point_count"], layout: { visibility: "none" },
        paint: { "circle-radius": ["step", ["get", "point_count"], 11, 10, 14, 50, 18], "circle-color": PHOTO_BLUE, "circle-opacity": 0.9,
                 "circle-stroke-color": "#fff", "circle-stroke-width": 2 } },
      { id: "photo-counts", type: "symbol", source: "photos", filter: ["has", "point_count"], layout: { visibility: "none",
          "text-field": ["get", "point_count_abbreviated"], "text-font": ["Open Sans Semibold"], "text-size": 12, "text-allow-overlap": true },
        paint: { "text-color": "#fff" } },
      { id: "photos", type: "circle", source: "photos", filter: ["!", ["has", "point_count"]], layout: { visibility: "none" },
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 4, 16, 6.5], "circle-color": PHOTO_BLUE, "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 } },
      { id: "entry-pick-at", type: "circle", source: "entry-pick", filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-radius": 7, "circle-color": "#fff", "circle-stroke-color": ISSUE_AMBER, "circle-stroke-width": 3 } },
    ],
  },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new BaseControl(), "top-right");
map.addControl(new maplibregl.ScaleControl(), "bottom-right");
// What is drawn waits for the map's style once. After that it is drawn at once, also while the map is busy with something else.
let styled = false;
const waiting_for_style = [];
map.once("load", () => { styled = true; for (const draw of waiting_for_style.splice(0)) draw(); });
const whenLoaded = draw => { if (styled) draw(); else waiting_for_style.push(draw); };
function drawMap() {
  whenLoaded(() => {
    // Among the routes the sign sites are small points to click; among the signs they say what is going on at each.
    const among = view === "routes";
    map.setLayoutProperty("sites", "visibility", among ? "none" : "visible");
    map.setLayoutProperty("site-names", "visibility", among ? "none" : "visible");
    map.setLayoutProperty("sites-small", "visibility", among ? "visible" : "none");
    // The routes of the area stand out; those of other areas are there to be seen past.
    for (const id of ["routes", "routes-ice"]) map.setPaintProperty(id, "line-opacity", area ? ["case", ["in", `|${area}|`, ["get", "areas"]], 0.8, 0.25] : 0.75);
    const walked = route && over && over.routes.find(r => r.code === route), mine = ["in", `|${walked ? walked.id : "none"}|`, ["get", "ids"]];
    // Among the reports the open route is an outline, ice and all: the colour of how long since it was done is in it.
    const outline = view === "reports";
    map.setPaintProperty("route-open", "line-color", outline ? css("--ink") : css("--sign"));
    map.setPaintProperty("route-open", "line-width", outline ? 2.5 : OPEN_WIDTH);
    map.setPaintProperty("route-open", "line-gap-width", outline ? SINCE_WIDTH : 0);
    map.setFilter("route-open", outline ? mine : ["all", mine, ["!=", ["get", "kind"], "glacier"]]);
    map.setFilter("route-open-ice", outline ? ["in", "|none|", ["get", "ids"]] : ["all", mine, ["==", ["get", "kind"], "glacier"]]);
    map.setFilter("routes-ice", ["all", ["!", mine], ["==", ["get", "kind"], "glacier"]]);     // the open route's own dots alone: two rows of dots smear
    map.getSource("sites").setData({ type: "FeatureCollection", features: (found ? found.sites : []).map(s => ({ type: "Feature",
      properties: { key: s.key, name: s.name || (s.near ? `by ${s.near.name}` : ""), state: s.no_signs ? "none" : onOrder(s) ? "order" : waiting(s) ? "waiting" : "", open: s.key === open }, geometry: { type: "Point", coordinates: [s.lon, s.lat] } })) });
    for (const m of stakes) m.remove();
    stakes = [];
    const site = found && found.sites.find(s => s.key === open), rays = [];
    for (const post of site ? site.posts : []) {
      if (post.taken_out) continue;
      const number = site.posts.indexOf(post) + 1;
      const stake = el("div", { class: `stake${post.assumed ? " assumed" : ""}${me.operates ? "" : " fixed"}`,
        title: `Post ${number}${post.assumed ? ", where it is assumed to stand" : ""}${me.operates ? ". Drag it to where it stands: the arrows follow." : ""}` },
        site.posts.length > 1 ? String(number) : "");
      const marker = new maplibregl.Marker({ element: stake, draggable: me.operates }).setLngLat([post.lon, post.lat]).addTo(map);
      marker.on("dragend", () => { const at = marker.getLngLat(); change(() => send("PATCH", `signs/posts/${post.key}`, { lon: at.lng, lat: at.lat })); });
      stakes.push(marker);
      const ways = new Map();                     // the signs of a post by the way they point: one line for each way
      for (const sign of post.signs) if (!sign.taken_out && sign.bearing !== null) ways.set(sign.bearing, [...(ways.get(sign.bearing) || []), sign]);
      for (const [bearing, signs] of ways) {
        const far = 28, b = bearing * Math.PI / 180, named = signs.filter(g => g.text);
        const text = named.length ? `${named[0].text}${named[0].km === null ? "" : ` ${kmText(named[0].km)}`}${signs.length > 1 ? `, and ${signs.length - 1} more` : ""}` : `${signs.length} with no name`;
        rays.push({ type: "Feature", properties: { text, post: post.key, bearing, hot: Boolean(hot && hot.post === post.key && hot.bearing === bearing) }, geometry: { type: "LineString", coordinates: [[post.lon, post.lat],
          [post.lon + far * Math.sin(b) / (111320 * Math.cos(post.lat * Math.PI / 180)), post.lat + far * Math.cos(b) / 111320]] } });
      }
    }
    drawn = rays;
    map.getSource("rays").setData({ type: "FeatureCollection", features: rays });
  });
}

map.on("click", e => {
  if (placing && placing.issue) { placedEntry(e); return; }
  if (placing && placing.photo) { placedPhoto(e); return; }
  if (placing && placing.reportPart !== undefined) { placedReportPart(e); return; }
  if (placing === "site") { stopPlacing(); change(async () => { open = (await send("POST", "signs/sites", { area, lon: e.lngLat.lng, lat: e.lngLat.lat })).key; }); return; }
  if (placing && placing.to) {
    const wanted = { post: placing.to, lon: e.lngLat.lng, lat: e.lngLat.lat };
    stopPlacing();
    change(async () => {
      const got = await send("POST", "signs/destinations", wanted);
      if (got.needs !== "name") return;
      const name = prompt("That is no place of yours, but a spot on a route. What is the sign to say?", "");
      if (name && name.trim()) await send("POST", "signs/destinations", { ...wanted, name: name.trim() });
    });
    return;
  }
  if (placing) { const node = placing.node; stopPlacing(); change(() => send("POST", "signs/posts", { node, lon: e.lngLat.lng, lat: e.lngLat.lat })); return; }
  if (view === "planner") { if (!planIgnoreClick) planAt(e.lngLat); return; }      // anywhere: a point of the walk
  const box = [[e.point.x - 6, e.point.y - 6], [e.point.x + 6, e.point.y + 6]];
  // Among the signs a site comes before the photos taken by it: they are there to be looked at beside it.
  const site = view === "signs" && map.queryRenderedFeatures(box, { layers: ["sites"] })[0];
  if (site) { openSite(site.properties.key); return; }
  const shown = map.getLayoutProperty("photos", "visibility") === "visible";
  const group = shown && map.queryRenderedFeatures(box, { layers: ["photo-groups"] })[0];
  if (group) { openGroup(group); return; }
  const shot = shown && map.queryRenderedFeatures(box, { layers: ["photos"] })[0];
  if (shot) { openPhoto(shot.properties.id); return; }
  const issue = map.getLayoutProperty("entries", "visibility") === "visible" && map.queryRenderedFeatures(box, { layers: ["entries", "entry-lines"] })[0];
  if (issue) { openEntry(issue.properties.id); return; }
  const hit = map.queryRenderedFeatures(box, { layers: [view === "signs" ? "sites" : "sites-small"] })[0];
  if (hit) { openSite(hit.properties.key); return; }
  if (!routesOpen()) return;
  // Among the routes, the reports and the issues a click on one opens it there; where several run together, it asks which.
  const at = routesAt(e.point);
  if (several) { several.remove(); several = null; }
  if (view === "landowners" && area && !at.length) { lookAt(e.lngLat); return; }      // off the routes: who owns it there
  if (!area && at.length) visit({ area: at[0].code.slice(0, 3).toLowerCase() });
  else if (at.length === 1) showRoute(at[0].code);
  else if (at.length > 1) several = new maplibregl.Popup({ closeButton: false, maxWidth: "300px" }).setLngLat(e.lngLat).setDOMContent(el("div", {},
    el("div", { class: "sub" }, `${at.length} routes go here.`),
    el("ul", {}, ...at.map(r => el("li", {}, el("button", { class: "link", onclick: () => showRoute(r.code) }, r.code), r.name ? ` ${r.name}` : ""))))).addTo(map);
});
// What is under the pointer, said in a small box that follows it: the routes of a line, with their length, grading,
// climb, and whether they are closed or have open issues, and what the stretch is; an issue; a photo.
const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "map-tip", offset: 16, maxWidth: "320px" });
let tipSays = "";
const STRETCH = { trail: "path", tractor_road: "tractor road", road: "road", glacier: "glacier", water: "over water", off_trail: "no path", connector: "connector", other: "other" };
function showTip(e) {
  const box = [[e.point.x - 5, e.point.y - 5], [e.point.x + 5, e.point.y + 5]];
  const visible = id => map.getLayer(id) && map.getLayoutProperty(id, "visibility") !== "none";
  const hit = layers => { const on = layers.filter(visible); return on.length ? map.queryRenderedFeatures(box, { layers: on })[0] : null; };
  let key = "", body = null;
  const group = hit(["photo-groups"]), shot = !group && hit(["photos"]), issue = !group && !shot && hit(["entries", "entry-lines"]);
  if (group) {
    key = `g${group.properties.cluster_id}`;
    body = () => el("div", {}, el("strong", {}, `${group.properties.point_count} photos`), el("div", { class: "sub" }, "Click to come nearer"));
  } else if (shot) {
    const p = (photosAll || []).find(x => x.id === shot.properties.id);
    key = `p${shot.properties.id}`;
    body = () => el("div", { class: "tip-photo" }, p ? el("img", { src: photoUrl(p, "thumb"), alt: "", width: 96, height: 96 }) : "",
      el("div", {}, el("strong", {}, p && p.caption || "A photo"), p && whenTaken(p) ? el("div", { class: "sub" }, `Taken ${whenTaken(p)}`) : ""));
  } else if (issue) {
    const x = (entries || []).find(y => y.id === issue.properties.id);
    key = `e${issue.properties.id}`;
    body = () => x ? el("div", {}, kindTag(x), " ", el("span", { class: "sub" }, placeSaid(x)), el("div", {}, x.text.length > 120 ? `${x.text.slice(0, 120)}…` : x.text)) : el("div", {}, "An issue");
  } else {
    const at = routesAt(e.point), line = hit(["routes", "routes-ice"]);
    if (at.length && !several) {
      const p = line ? line.properties : {};
      key = `r${at.map(r => r.id).join()}|${p.edge_id || ""}`;
      body = () => {
        const shown = at.slice(0, 3);
        const about = r => {
          if (r.on_foot_m === undefined) return "";
          const closed = typeof inForce === "function" ? inForce(r.id) : [], open = typeof entriesOn === "function" ? entriesOn(r.id).filter(x => x.open && !x.active) : [];
          return el("div", { class: "sub" }, [km(r.on_foot_m / 1000), r.grading && GRADING[r.grading] ? GRADING[r.grading].split(",")[0].toLowerCase() : null,
            r.up_m ? `${Math.round(r.up_m).toLocaleString("en")} m up` : null].filter(Boolean).join(", "),
            ...closed.slice(0, 1).map(x => el("span", { class: "tip-closed" }, x.kind === "closed" ? " Closed" : " Warning")),
            open.length ? ` ${open.length} open ${open.length === 1 ? "issue" : "issues"}` : "");
        };
        return el("div", {},
          ...shown.map(r => el("div", { class: "tip-route" }, el("strong", {}, r.code), r.name ? ` ${r.name}` : "", about(r))),
          at.length > shown.length ? el("div", { class: "sub" }, `and ${at.length - shown.length} more`) : "",
          p.kind ? el("div", { class: "sub tip-stretch" }, `This stretch: ${STRETCH[p.kind] || p.kind}, ${p.marked === true ? "marked" : p.marked === false ? "not marked" : "marking not said"}`
            + (at.length > 1 ? `, shared by ${at.length} routes` : "")) : "");
      };
    }
  }
  if (!body) { tip.remove(); tipSays = ""; return; }
  if (key !== tipSays) { tip.setDOMContent(body()); tipSays = key; }
  tip.setLngLat(e.lngLat);
  if (!tip.isOpen()) tip.addTo(map);
}
map.getCanvas().addEventListener("mouseleave", () => { tip.remove(); tipSays = ""; });
map.on("movestart", () => { tip.remove(); tipSays = ""; });
// Whether a click on a route opens it: in the parts a route opens in, and not while a report or an issue is being said.
const routesOpen = () => ROUTE_VIEWS.includes(view) && !reportAsk && !entryAsk;
// The routes in use that run where the map was clicked: of the area, or with none chosen, of any.
function routesAt(point) {
  const ids = new Map();
  for (const f of map.queryRenderedFeatures([[point.x - 5, point.y - 5], [point.x + 5, point.y + 5]], { layers: ["routes", "routes-ice"] })) {
    const codes = f.properties.codes_in_use.split("|");
    f.properties.ids.split("|").forEach((id, i) => { if (id) ids.set(id, codes[i]); });
  }
  return area ? (over ? over.routes.filter(r => ids.has(r.id)) : []) : [...ids].map(([id, code]) => ({ id, code }));
}
map.on("mousemove", e => {
  if (placing || map.isMoving()) { tip.remove(); tipSays = ""; return; }
  showTip(e);
  const issues = [...(map.getLayoutProperty("entries", "visibility") === "visible" ? ["entries", "entry-lines"] : []),
                  ...(map.getLayoutProperty("photos", "visibility") === "visible" ? ["photos", "photo-groups"] : [])];
  // Among the owners every spot can be clicked: a hand where a property is drawn, else a crosshair (zoomed out, or none known).
  const owners = view === "landowners" && area && map.getLayoutProperty("parcels-fill", "visibility") === "visible";
  map.getCanvas().style.cursor = map.queryRenderedFeatures(e.point, { layers: [view === "signs" ? "sites" : "sites-small", ...issues] }).length || (routesOpen() && routesAt(e.point).length) ? "pointer"
    : owners ? (map.queryRenderedFeatures(e.point, { layers: ["parcels-fill"] }).length ? "pointer" : "crosshair") : view === "planner" ? (planDrag ? "grabbing" : map.getLayer("plan") && map.queryRenderedFeatures(e.point, { layers: ["plan", "plan-dashed"] }).length ? "grab" : "crosshair") : "";
  const ray = map.queryRenderedFeatures([[e.point.x - 5, e.point.y - 5], [e.point.x + 5, e.point.y + 5]], { layers: ["rays"] })[0];
  point(ray ? ray.properties.post : null, ray ? ray.properties.bearing : null);
});
// The map moves to what is chosen: a route, the routes of the area, or with no area all there are.
function fitTo(wanted, maxZoom) {
  if (!lines) return false;
  let w = 180, s = 90, e = -180, n = -90;
  for (const f of lines.features) {
    if (!wanted(f)) continue;
    for (const [x, y] of f.geometry.type === "LineString" ? f.geometry.coordinates : f.geometry.coordinates.flat()) { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }
  }
  if (w > e) return false;
  map.fitBounds([[w, s], [e, n]], { padding: 60, maxZoom, animate: !calm });
  return true;
}
function fit() {
  if (fitTo(f => !area || f.properties.areas.includes(`|${area}|`), 13)) return;
  if (!found || !found.sites.length) return;
  const lons = found.sites.map(s => s.lon), lats = found.sites.map(s => s.lat);
  map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: 60, maxZoom: 13, animate: !calm });
}
// Only what is in use is drawn. Each stretch says whose it is in a form the map can be asked about: its routes, and
// the areas they are of, by their codes and as routes added to an area from another.
let rawLines = null;
function inUse(all) {
  rawLines = all;
  const addedTo = new Map();
  for (const a of areas || []) for (const id of a.added || []) addedTo.set(id, [...(addedTo.get(id) || []), a.area]);
  const features = [];
  for (const f of all.features) {
    const of = (f.properties.routes || []).filter(r => r.in_use);
    if (!of.length) continue;
    f.properties.ids = `|${of.map(r => r.id).join("|")}|`;
    f.properties.codes_in_use = `|${of.map(r => r.code).join("|")}|`;
    f.properties.areas = `|${[...new Set(of.flatMap(r => [r.code.slice(0, 3).toLowerCase(), ...(addedTo.get(r.id) || [])]))].join("|")}|`;
    features.push(f);
  }
  return { type: "FeatureCollection", features };
}
