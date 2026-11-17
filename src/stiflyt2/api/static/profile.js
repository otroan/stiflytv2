// A route's height, shared by the pages that show one: what it climbs in words, and the profile drawn.
// The page gives `el` and `svg` (elements from data, as text), `km` (a length in words), `map`, and
// `go(center, zoom)` (moves the map there).
let spot = null;       // where on the map the pointer is on a route's height profile
const whole = v => Math.round(v).toLocaleString("en");
const hours = m => { const r = Math.round(m / 5) * 5; return r < 60 ? `${r} min` : `${Math.floor(r / 60)} h${r % 60 ? ` ${r % 60} min` : ""}`; };
const BY_NUMBERS = { G: "green", B: "blue", R: "red", S: "black" };
function leaveProfile() { if (spot) { spot.remove(); spot = null; } }
// What a route climbs, in words. `h` is its height as the route itself says it, or as the profile came back.
function climbs(route, h) {
  if (!h.known_m) return [el("span", { class: "sub" }, h.waiting && h.waiting.why ? "Kartverket's height service did not answer. It is asked again tonight, and when the route is next opened." : "No heights for its line yet.")];
  const place = id => (route.ends.find(e => e.id === id) || {}).place;
  const way = route.shape === "loop" ? ", walked round" : route.shape !== "line" ? ""
    : place(h.from_node) && place(h.to_node) ? `, walked from ${place(h.from_node)} to ${place(h.to_node)}`
    : place(h.from_node) ? `, walked from ${place(h.from_node)}` : ", walked from its western end";
  const rest = h.on_foot_m - h.known_m;
  return [`${whole(h.up_m)} m up and ${whole(h.down_m)} m down${way}. Between ${whole(h.low_m)} and ${whole(h.high_m)} m above sea level.`,
    h.there ? el("div", { class: "sub" }, `About ${hours(h.there.minutes)}` + (route.shape === "line" && h.back.minutes !== h.there.minutes ? ` that way and ${hours(h.back.minutes)} back` : "") + ", by Naismith's rule"
      + (h.there.steep_down_m + h.back.steep_down_m >= 30 ? ", with time added for going down steeply" : "") + ". "
      + `Length and climb alone make it ${BY_NUMBERS[h.there.at_least]}` + (route.shape === "line" && h.back.at_least !== h.there.at_least ? ` that way and ${BY_NUMBERS[h.back.at_least]} back` : "") + ".") : "",
    h.ice_m >= 20 ? el("div", { class: "sub" }, `${km(h.ice_m / 1000)} of it is on snow and ice, as Kartverket has the ground.`) : "",
    rest > 5 ? el("div", { class: "sub" }, `That is for ${km(h.known_m / 1000)} of it: ${km(rest / 1000)} has no heights yet. `,
      h.waiting && h.waiting.why ? "Kartverket's height service did not answer; it is asked again tonight." : "They are being asked for: open the route again in a moment.") : ""];
}
// The profile itself: height against distance walked, snow and ice shaded, the places passed ticked off.
// Pointing at it shows the spot on the map; a click goes there.
function drawProfile(box, route, h) {
  const S = h.samples.filter(p => p[1] !== null);
  if (S.length < 2) { box.replaceChildren(); return; }
  const W = 316, across = W - 42, H = 118, top = 16, foot = H - 4, total = Math.max(h.on_foot_m, 1);    // the heights are written beside the drawing
  let lo = h.low_m, hi = h.high_m;
  if (hi - lo < 60) { const mid = (hi + lo) / 2; lo = mid - 30; hi = mid + 30; }      // level ground is not drawn as a mountain
  const x = at => at / total * across, y = z => top + (hi - z) / (hi - lo) * (foot - top);
  const runs = [[]];                // unbroken parts: a point with no height is where one ends
  for (const p of h.samples) { if (p[1] === null) { if (runs.at(-1).length) runs.push([]); } else runs.at(-1).push(p); }
  const ground = [], ridge = [], ice = [];
  for (const run of runs.filter(r => r.length > 1)) {
    const line = run.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" L");
    ridge.push(`M${line}`);
    ground.push(`M${x(run[0][0]).toFixed(1)},${foot} L${line} L${x(run.at(-1)[0]).toFixed(1)},${foot} Z`);
    for (let i = 0; i < run.length - 1; i++) {
      if (!(run[i][2] && run[i + 1][2])) continue;
      let j = i; while (j < run.length - 1 && run[j][2] && run[j + 1][2]) j++;
      ice.push(`M${x(run[i][0]).toFixed(1)},${foot} L${run.slice(i, j + 1).map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" L")} L${x(run[j][0]).toFixed(1)},${foot} Z`);
      i = j;
    }
  }
  const hair = svg("line", { class: "at", y1: top - 2, y2: foot, hidden: "" }), said = svg("text", { class: "said", y: 10 });
  const names = route.shape === "line" ? [h.from_node, h.to_node].map(id => (route.ends.find(e => e.id === id) || {}).place || "") : ["", ""];
  const drawing = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: 0,
      "aria-label": `Height profile: ${whole(h.up_m)} metres up and ${whole(h.down_m)} metres down over ${km(h.known_m / 1000)}, between ${whole(h.low_m)} and ${whole(h.high_m)} metres above sea level. Arrow keys move along it.` },
    svg("path", { class: "ground", d: ground.join(" ") }), svg("path", { class: "onice", d: ice.join(" ") }),
    svg("line", { class: "foot", x1: 0, x2: across, y1: foot, y2: foot }),
    ...h.places.map(p => svg("line", { class: "passed", x1: x(p.at_m), x2: x(p.at_m), y1: foot - 5, y2: foot + 3 }, svg("title", {}, p.name))),
    svg("path", { class: "ridge", d: ridge.join(" ") }), hair,
    svg("text", { x: W, y: y(h.high_m) + 4, "text-anchor": "end" }, `${whole(h.high_m)} m`),
    svg("text", { x: W, y: Math.min(foot, y(h.low_m) + 4), "text-anchor": "end" }, `${whole(h.low_m)} m`), said);
  let now = null;
  const show = i => {
    now = Math.max(0, Math.min(S.length - 1, i));
    const p = S[now];
    hair.removeAttribute("hidden"); hair.setAttribute("x1", x(p[0])); hair.setAttribute("x2", x(p[0]));
    said.textContent = `${km(p[0] / 1000)}, ${whole(p[1])} m${p[2] ? ", snow and ice" : ""}`;
    if (!spot) spot = new maplibregl.Marker({ element: el("div", { class: "spot" }) });
    spot.setLngLat([p[3], p[4]]).addTo(map);
  };
  const nearest = at => { let a = 0, b = S.length - 1; while (b - a > 1) { const m = (a + b) >> 1; if (S[m][0] < at) a = m; else b = m; } return at - S[a][0] < S[b][0] - at ? a : b; };
  const under = e => { const r = drawing.getBoundingClientRect(); return nearest((e.clientX - r.left) / (r.width * across / W) * total); };
  const hide = () => { now = null; hair.setAttribute("hidden", ""); said.textContent = ""; leaveProfile(); };
  drawing.addEventListener("pointermove", e => show(under(e)));
  drawing.addEventListener("pointerleave", hide);
  drawing.addEventListener("blur", hide);
  drawing.addEventListener("click", e => { const p = S[under(e)]; go([p[3], p[4]], Math.max(map.getZoom(), 14)); });
  drawing.addEventListener("keydown", e => {
    const step = Math.max(1, Math.round(S.length / 60));
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); show((now === null ? (e.key === "ArrowRight" ? -step : S.length - 1 + step) : now) + (e.key === "ArrowRight" ? step : -step)); }
    else if (e.key === "Enter" && now !== null) go([S[now][3], S[now][4]], Math.max(map.getZoom(), 14));
    else if (e.key === "Escape" && now !== null) { e.stopPropagation(); hide(); }
  });
  box.replaceChildren(drawing, names[0] || names[1] ? el("div", { class: "ends sub" }, el("span", {}, names[0]), el("span", {}, names[1])) : "");
}
