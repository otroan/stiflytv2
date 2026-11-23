// What the pages share: making elements from data (always as text), asking the server, and moving the map.
// A page gives `map`.
const API = new URL("api/v1/", location.origin + "/").href;
// Text from the data is always inserted as text, never as HTML.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
}
// The role switch: one who may change things can look at the pages as one who may do less. The choice is
// this tab's, and is sent with every request, so that the server answers and refuses as it would that one.
const LOOKING_AS = sessionStorage.getItem("stiflyt2.as");
const THROUGH = { "X-Stiflyt-Through": "viewer", ...(LOOKING_AS ? { "X-Stiflyt-As": LOOKING_AS } : {}) };
const ONE_WHO = { reader: "a reader", operations: "someone in operations", editor: "an editor" };
const ROLE_SAYS = { reader: "reader: looks", operations: "operations: changes signs, not routes", editor: "editor: changes routes and signs" };
function lookAs(role) {
  if (role) sessionStorage.setItem("stiflyt2.as", role); else sessionStorage.removeItem("stiflyt2.as");
  location.reload();
}
// Put above a page's footer: which role the page is shown for, and the way to another. Nothing, for one who
// holds no role to step down from. `me` is what auth/me answered.
function roleSwitch(me, footer) {
  for (const old of document.querySelectorAll(".role-switch")) old.remove();
  if (!me.may_look_as || !me.may_look_as.length) return;
  const holds = me.really || me.role;
  const choose = el("select", { "aria-label": "Show the pages as", style: "width:auto;padding:2px 6px;font:inherit",
      onchange: e => lookAs(e.target.value === holds ? null : e.target.value) },
    ...[holds, ...[...me.may_look_as].reverse()].map(r => el("option", { value: r, selected: r === me.role }, ROLE_SAYS[r] || r)));
  const bar = me.really
    ? el("div", { class: "role-switch", role: "status", style: "padding:8px 16px;background:#fde8d8;color:#7a2c00;font-size:13px;font-weight:500" },
        `Shown as ${ONE_WHO[me.role] || me.role} sees it. You are ${ONE_WHO[holds] || holds}. `, choose, " ",
        el("button", { class: "link", style: "color:inherit", onclick: () => lookAs(null) }, `Back to ${holds}`))
    : el("div", { class: "role-switch", style: "padding:6px 16px;border-top:1px solid var(--line);color:var(--muted);font-size:13px" },
        el("label", {}, "Show the pages as ", choose));
  footer.parentNode.insertBefore(bar, footer);
}
// Not signed in any more (the session ran out, or you were taken off the list): to the sign-in, and back here.
let leaving = false;
function signIn() { if (!leaving) { leaving = true; location.href = new URL(`auth/login?next=${encodeURIComponent(location.pathname)}`, API).href; } }
async function get(path, params = {}) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) url.searchParams.set(k, v);
  const r = await fetch(url, { headers: THROUGH });
  if (r.status === 401) signIn();
  if (!r.ok) {         // what the service says, where it says something in words
    let message = `${r.status} ${r.statusText}`;
    try { const d = (await r.json()).detail; if (typeof d === "string") message = d; } catch {}
    throw new Error(message);
  }
  return r.json();
}
// A change. `more` is further request headers. A page that keeps track of what others do says so in
// `afterChange`: what it shows after a change of its own is brought up to date by whoever made it.
let afterChange = null;
async function send(method, path, body, more) {
  const r = await fetch(new URL(path, API), { method, headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...THROUGH, ...(more || {}) }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) signIn();
  if (!r.ok) {
    let message = `${r.status} ${r.statusText}`;
    try { const d = (await r.json()).detail; message = typeof d === "string" ? d : JSON.stringify(d); } catch {}
    throw new Error(message);
  }
  if (afterChange) afterChange();
  return r.status === 204 ? null : r.json();
}
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// Today where the person is, as a date field says it: not in UTC, which is yesterday until after midnight here.
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const km = v => `${v.toLocaleString("en", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} km`;
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
}
function go(center, zoom) {
  const to = { center, zoom: Math.max(map.getZoom(), zoom) };
  if (calm) map.jumpTo(to); else map.flyTo(to);
}
// Who else is at work in the pages now (the feature `presence`): this page says where it is every 20 seconds,
// and is told who else has a page open, which is shown in `box`. Only for one who has the feature: the others
// who have it see this one the same way.
function presence(me, box, where) {
  if (!box || !me || !me.name || !(me.features || []).includes("presence")) return;
  const PAGE = { editor: "route editor", routes: "routes", signs: "signs", trips: "trips", issues: "issues", photos: "photos", reports: "reports" };
  let every = null;
  const tell = async () => {
    if (document.hidden) return;
    let others;
    try {
      const r = await fetch(new URL("here", API), { method: "POST", headers: { "Content-Type": "application/json", ...THROUGH }, body: JSON.stringify(where()) });
      if (r.status === 403) clearInterval(every);         // not for this one after all: asked from a script, say
      if (!r.ok) return;
      others = await r.json();
    } catch { return; }
    box.replaceChildren(...(others.length ? ["Also here now: ", ...others.flatMap((o, i) => [i ? "; " : "", el("strong", {}, o.name),
      ` (${[PAGE[o.page] || "a page", o.area, o.route || o.site].filter(Boolean).join(", ")}${o.unsaved ? `, ${o.unsaved} not saved` : ""})`])] : []));
    box.hidden = !others.length;
  };
  every = setInterval(tell, 20000);
  tell();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tell(); });
}
// A web search for a route's page on UT.no, by the names of its ends, opened in the person's own browser: there is no
// search for it on the server (UT.no's own runs through its API, which wants a client we do not have).
function utnoSearch(name, code) {
  const words = (name || "").split(/\s+[–-]\s+/).map(w => w.trim()).filter(Boolean).map(w => `"${w}"`).join(" ") || code;
  return `https://www.google.com/search?q=${encodeURIComponent(`site:ut.no rutebeskrivelse ${words}`)}`;
}
