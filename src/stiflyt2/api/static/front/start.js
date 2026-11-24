// The front page starts: the browser's back, the tabs, Escape, and what the address asks for.
window.addEventListener("popstate", () => arrive(here()));
for (const [id, is] of [["tab-routes", "routes"], ["tab-signs", "signs"], ["tab-issues", "issues"], ["tab-photos", "photos"], ["tab-reports", "reports"], ["tab-landowners", "landowners"], ["tab-trips", "trips"], ["tab-planner", "planner"]])
  document.getElementById(id).addEventListener("click", e => { if (e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); visit({ view: is, route: null, site: null, entry: null, report: null }); });

document.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === "z" && (view === "signs" || view === "issues" || view === "reports") && !["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) { e.preventDefault(); if (view === "issues") undoEntry(); else if (view === "reports") undoReport(); else undoLast(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { if (photoBox.open) return; if (!basketBox.hidden) { basketBox.hidden = true; basket.setAttribute("aria-expanded", "false"); basket.focus(); } else if (placing) { stopPlacing(); if (entryAsk && entryAsk.picking) { entryAsk.picking = null; render(); } } else if (entryAsk) { entryAsk = null; render(); } else if (entryOpen) visit({ entry: null }); else if (reportAsk) { reportAsk = null; render(); } else if (reportOpen) visit({ report: null }); else if (editing) { editing = null; render(); } else if (open) visit({ site: null }); else if (lookingAtAdrift) { lookingAtAdrift = false; render(); } else if (ownedAt) ownedPopup.remove(); else if (route) leaveRoute(); } });

(async () => {
  try {
    me = await get("auth/me");
    roleSwitch(me, document.getElementById("who"));
    presence(me, document.getElementById("present"), () => {
      const site = view === "signs" && open && found && found.sites.find(s => s.key === open);
      const issue = view === "issues" && entryOpen && (entries || []).find(x => x.id === entryOpen);
      return { page: view, area, route: route || (issue && issue.routes.length ? issue.routes[0].code : null),
               site: site ? site.name || (site.near ? `by ${site.near.name}` : null) : issue ? `${ISSUE_KINDS[issue.kind]}, ${issue.text}`.slice(0, 60) : null };
    });
    // The parts that are features of their own (ADR-005) are there only for one who has them.
    document.getElementById("tab-trips").hidden = !(me.features || []).includes("trips");
    document.getElementById("tab-issues").hidden = !hasIssues();
    document.getElementById("tab-photos").hidden = !hasPhotos();
    document.getElementById("tab-reports").hidden = !hasReports();
    document.getElementById("tab-landowners").hidden = !hasLandowners();
    document.getElementById("tab-planner").hidden = !hasPlanner();
    const out = me.sign_in ? el("button", { class: "link", onclick: async () => { await send("POST", "auth/logout"); location.reload(); } }, "Sign out") : "";
    document.getElementById("who").replaceChildren(me.name ? (me.operates ? `Signed in as ${me.name}. What you decide about signs is kept under that name. ` : `Signed in as ${me.name}, to look. Changing signs needs the operations role. `) : "Read-only. ", out);
    areas = await get("owned/areas");
    const drawn = get("owned/routes.geojson").then(all => { lines = inUse(all); whenLoaded(() => map.getSource("routes").setData(lines)); }).catch(() => {});
    get("places.geojson").then(all => whenLoaded(() => map.getSource("places").setData({ type: "FeatureCollection", features: all.features.filter(f => f.properties.type === "place") }))).catch(() => {});
    // The address says where to start. With none, the area last looked at; with no such, the areas.
    const at = here();
    // With no area in the address: the first of yours, as your profile says; else the one last looked at.
    if (!new URLSearchParams(location.search).has("area")) at.area = (me.profile && me.profile.areas[0]) || localStorage.getItem("stiflyt2.area") || localStorage.getItem("stiflyt2.signs.area");
    await Promise.all([arrive(at, false), drawn]);
    if (!location.hash) fit();
  } catch (err) { panel.replaceChildren(el("div", { class: "error" }, `No contact with the server: ${err.message}`)); }
})();
