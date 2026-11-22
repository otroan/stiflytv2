// The base maps, and the control on the map that switches between them. Shared by the pages: what is
// chosen in one is what the other opens with.
const BASEMAPS = [
  { id: "grey", label: "Topographic map, grey (Kartverket)", source: { type: "raster", tileSize: 256, maxzoom: 18, attribution: "© Kartverket",
      tiles: ["https://cache.kartverket.no/v1/wmts/1.0.0/topograatone/default/webmercator/{z}/{y}/{x}.png"] } },
  { id: "colour", label: "Topographic map, in colour (Kartverket)", source: { type: "raster", tileSize: 256, maxzoom: 18, attribution: "© Kartverket",
      tiles: ["https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png"] } },
  // To look at beside Kartverket's. Not a source for the routes: nothing is traced from it.
  { id: "osm", label: "OpenStreetMap (made by its users)", source: { type: "raster", tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"] } },
];
const baseChosen = () => { const b = localStorage.getItem("stiflyt2.base"); return BASEMAPS.some(m => m.id === b) ? b : "grey"; };
const baseSources = () => Object.fromEntries(BASEMAPS.map(m => [`base-${m.id}`, m.source]));
const baseLayers = () => BASEMAPS.map(m => ({ id: `base-${m.id}`, type: "raster", source: `base-${m.id}`,
  layout: { visibility: m.id === baseChosen() ? "visible" : "none" } }));

class BaseControl {
  onAdd(map) {
    if (!document.getElementById("basemap-style")) {
      const style = document.createElement("style");
      style.id = "basemap-style";
      style.textContent = `.basemaps { position: relative; }
        .basemaps .menu { position: absolute; top: 0; right: 36px; background: #fff; border-radius: 4px; box-shadow: 0 0 0 2px #0000001a; padding: 4px; white-space: nowrap; }
        .basemaps .menu button { display: block; width: 100%; height: auto; padding: 5px 10px 5px 26px; text-align: left; font: inherit; border: 0; border-radius: 3px; position: relative; }
        .basemaps .menu button[aria-checked=true]::before { content: ""; position: absolute; left: 10px; top: 50%; width: 8px; height: 8px; margin-top: -4px; border-radius: 50%; background: currentColor; }`;
      document.head.append(style);
    }
    this.box = document.createElement("div");
    this.box.className = "maplibregl-ctrl maplibregl-ctrl-group basemaps";
    const open = document.createElement("button");
    open.type = "button";
    open.title = "Base map: Kartverket's topographic map in grey or in colour, or OpenStreetMap";
    open.setAttribute("aria-label", "Base map");
    open.setAttribute("aria-expanded", "false");
    open.innerHTML = '<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true" style="display:block;margin:auto"><path d="M9 2l7 3.6L9 9.2 2 5.6z" fill="none" stroke="#16242e" stroke-width="1.5" stroke-linejoin="round"/><path d="M2 9l7 3.6L16 9M2 12.4L9 16l7-3.6" fill="none" stroke="#16242e" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>';
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    const show = on => { menu.hidden = !on; open.setAttribute("aria-expanded", String(on)); };
    const mark = () => { for (const b of menu.children) b.setAttribute("aria-checked", String(b.dataset.base === baseChosen())); };
    for (const base of BASEMAPS) {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.setAttribute("role", "menuitemradio");
      choice.dataset.base = base.id;
      choice.textContent = base.label;
      choice.addEventListener("click", () => {
        localStorage.setItem("stiflyt2.base", base.id);
        for (const other of BASEMAPS) map.setLayoutProperty(`base-${other.id}`, "visibility", other.id === base.id ? "visible" : "none");
        mark();
        show(false);
        open.focus();
      });
      menu.append(choice);
    }
    mark();
    open.addEventListener("click", () => show(menu.hidden));
    this.box.addEventListener("keydown", e => { if (e.key === "Escape" && !menu.hidden) { e.stopPropagation(); show(false); open.focus(); } });
    this.away = e => { if (!this.box.contains(e.target)) show(false); };
    document.addEventListener("click", this.away);
    this.box.append(open, menu);
    return this.box;
  }
  onRemove() { document.removeEventListener("click", this.away); this.box.remove(); }
}
