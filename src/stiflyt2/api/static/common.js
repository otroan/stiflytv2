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
const THROUGH = { "X-Stiflyt-Through": "viewer" };
// Not signed in any more (the session ran out, or you were taken off the list): to the sign-in, and back here.
let leaving = false;
function signIn() { if (!leaving) { leaving = true; location.href = new URL(`auth/login?next=${encodeURIComponent(location.pathname)}`, API).href; } }
async function get(path, params = {}) {
  const url = new URL(path, API);
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) url.searchParams.set(k, v);
  const r = await fetch(url, { headers: THROUGH });
  if (r.status === 401) signIn();
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
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
