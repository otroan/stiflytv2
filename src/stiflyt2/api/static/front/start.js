// The front page starts: the browser's back, the tabs, Escape, and what the address asks for.
window.addEventListener("popstate", () => arrive(here()));
for (const [id, is] of [["tab-routes", "routes"], ["tab-signs", "signs"], ["tab-issues", "issues"], ["tab-trips", "trips"]])
  document.getElementById(id).addEventListener("click", e => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); visit({ view: is, route: null, site: null, entry: null }); });

document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === "z" && (view === "signs" || view === "issues") && !["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) { e.preventDefault(); if (view === "issues") undoEntry(); else undoLast(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { if (!basketBox.hidden) { basketBox.hidden = true; basket.setAttribute("aria-expanded", "false"); basket.focus(); } else if (placing) { stopPlacing(); if (entryAsk && entryAsk.picking) { entryAsk.picking = null; render(); } } else if (entryAsk) { entryAsk = null; render(); } else if (entryOpen) visit({ entry: null }); else if (editing) { editing = null; render(); } else if (open) visit({ site: null }); else if (lookingAtAdrift) { lookingAtAdrift = false; render(); } else if (route) visit({ route: null }); } });

(async () => {
  try {
    me = await get("auth/me");
    roleSwitch(me, document.getElementById("who"));
    presence(me, document.getElementById("present"), () => {
      const site = view === "signs" && open && found && found.sites.find(s => s.key === open);
      const issue = view === "issues" && entryOpen && (entries || []).find(x => x.id === entryOpen);
      return { page: view, area, route: view === "routes" ? route : issue && issue.routes.length ? issue.routes[0].code : null,
               site: site ? site.name || (site.near ? `by ${site.near.name}` : null) : issue ? `${ISSUE_KINDS[issue.kind]}, ${issue.text}`.slice(0, 60) : null };
    });
    // The parts that are features of their own (ADR-005) are there only for one who has them.
    document.getElementById("tab-trips").hidden = !(me.features || []).includes("trips");
    document.getElementById("tab-issues").hidden = !hasIssues();
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
