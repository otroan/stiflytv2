// The front page starts: the browser's back, the tabs, Escape, and what the address asks for.
window.addEventListener("popstate", () => arrive(here()));
for (const [id, is] of [["tab-routes", "routes"], ["tab-signs", "signs"], ["tab-trips", "trips"]])
  document.getElementById(id).addEventListener("click", e => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); visit({ view: is, route: null, site: null }); });

document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === "z" && view === "signs" && !["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) { e.preventDefault(); undoLast(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { if (!basketBox.hidden) { basketBox.hidden = true; basket.setAttribute("aria-expanded", "false"); basket.focus(); } else if (placing) stopPlacing(); else if (editing) { editing = null; render(); } else if (open) visit({ site: null }); else if (lookingAtAdrift) { lookingAtAdrift = false; render(); } else if (route) visit({ route: null }); } });

(async () => {
  try {
    me = await get("auth/me");
    roleSwitch(me, document.getElementById("who"));
    // The parts that are features of their own (ADR-005) are there only for one who has them.
    document.getElementById("tab-trips").hidden = !(me.features || []).includes("trips");
    const out = me.sign_in ? el("button", { class: "link", onclick: async () => { await send("POST", "auth/logout"); location.reload(); } }, "Sign out") : "";
    document.getElementById("who").replaceChildren(me.name ? (me.operates ? `Signed in as ${me.name}. What you decide about signs is kept under that name. ` : `Signed in as ${me.name}, to look. Changing signs needs the operations role. `) : "Read-only. ", out);
    areas = await get("owned/areas");
    const drawn = get("owned/routes.geojson").then(all => { lines = inUse(all); whenLoaded(() => map.getSource("routes").setData(lines)); }).catch(() => {});
    get("places.geojson").then(all => whenLoaded(() => map.getSource("places").setData({ type: "FeatureCollection", features: all.features.filter(f => f.properties.type === "place") }))).catch(() => {});
    // The address says where to start. With none, the area last looked at; with no such, the areas.
    const at = here();
    if (!new URLSearchParams(location.search).has("area")) at.area = localStorage.getItem("stiflyt2.area") || localStorage.getItem("stiflyt2.signs.area");
    await Promise.all([arrive(at, false), drawn]);
    if (!location.hash) fit();
  } catch (err) { panel.replaceChildren(el("div", { class: "error" }, `No contact with the server: ${err.message}`)); }
})();
