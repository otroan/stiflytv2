// A walk through both pages in a real browser, with real clicks: the front page (areas, an area, a route,
// the signs) and the route editor. It changes nothing: every request but GET is stopped before it leaves.
// With STIFLYT2_EDIT=1, and only then, it goes on to change a route's name and take that back and bring it
// back, to try undo and redo: for a viewer on the test database (run.sh asks for it), never the real one.
//
//   node tests/browser/smoke.mjs http://localhost:8113/
//
// Needs Node 22 or newer and Chrome (CHROME names another binary). Against a service with sign-in,
// STIFLYT2_TOKEN is sent as a bearer token to that address only. Exits 1 when a check fails, when the
// page throws, or when one of the service's own addresses answers with an error.
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const HOME = new URL(process.argv[2] || "http://localhost:8100/").href, TOKEN = process.env.STIFLYT2_TOKEN || "";
const EDIT = process.env.STIFLYT2_EDIT === "1" && !TOKEN;
let editing = false;
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9400 + Math.floor(Math.random() * 500);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = mkdtempSync(join(tmpdir(), "stiflyt2-smoke-"));
const chrome = spawn(CHROME, ["--headless=new", "--no-first-run", `--user-data-dir=${profile}`, `--remote-debugging-port=${PORT}`, "--window-size=1500,1050", "about:blank"], { stdio: "ignore" });
let target;
for (let i = 0; i < 60 && !target; i++) { await sleep(500); try { target = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find(t => t.type === "page"); } catch {} }
if (!target) { console.error("Chrome did not start"); process.exit(2); }
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let seq = 0;
const waiting = new Map(), thrown = [], refused = [], stopped = [], failed = [], asked = [], said = [];
const send = (method, params = {}) => new Promise(r => { waiting.set(++seq, r); ws.send(JSON.stringify({ id: seq, method, params })); });
ws.addEventListener("message", m => {
  const d = JSON.parse(m.data);
  if (d.id && waiting.has(d.id)) { waiting.get(d.id)(d.result); waiting.delete(d.id); }
  // A box the page puts up (alert, confirm) would stop everything here: it is answered yes, and told of below.
  if (d.method === "Page.javascriptDialogOpening") { said.push(d.params.message); send("Page.handleJavaScriptDialog", { accept: true }); }
  if (d.method === "Runtime.exceptionThrown") thrown.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === "Network.responseReceived" && d.params.response.url.startsWith(HOME) && d.params.response.status >= 400 && !d.params.response.url.endsWith("/favicon.ico"))
    refused.push(`${d.params.response.status} ${d.params.response.url.slice(HOME.length - 1)}`);
  if (d.method === "Fetch.requestPaused") {
    const q = d.params.request;
    asked.push(q.url.slice(HOME.length - 1));
    if (q.method !== "GET" && !editing) { stopped.push(`${q.method} ${q.url.slice(HOME.length - 1)}`); send("Fetch.failRequest", { requestId: d.params.requestId, errorReason: "BlockedByClient" }); return; }
    send("Fetch.continueRequest", { requestId: d.params.requestId, headers: [...Object.entries(q.headers).map(([name, value]) => ({ name, value })), ...(TOKEN ? [{ name: "Authorization", value: `Bearer ${TOKEN}` }] : [])] });
  }
});
const js = async expr => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); return !r ? undefined : r.exceptionDetails ? `EXCEPTION ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}` : r.result.value; };
const until = async (expr, seconds = 30) => { for (let i = 0; i < seconds * 4; i++) { if (await js(expr) === true) return true; await sleep(250); } return false; };
const mouse = (type, x, y, buttons = 0) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons, clickCount: 1 });
const click = async at => { await mouse("mouseMoved", at.x, at.y); await mouse("mousePressed", at.x, at.y, 1); await mouse("mouseReleased", at.x, at.y); };
const centre = async sel => JSON.parse(await js(`(() => { const e = ${sel}; if (!e) return "null"; e.scrollIntoView({ block: "center" }); const r = e.getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }); })()`));
const clickOn = async sel => { const at = await centre(sel); if (!at) return false; await click(at); return true; };
const text = sel => js(`(document.querySelector(${JSON.stringify(sel)}) || {}).innerText || ""`);
const onMap = expr => js(`(() => { const c = ${expr}; if (!c) return "null"; const p = map.project(c), r = document.getElementById("map").getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + p.x), y: Math.round(r.top + p.y) }); })()`).then(JSON.parse);
const check = (what, ok, said = "") => { console.log(`${ok ? "ok  " : "FAIL"} ${what}${said ? `: ${String(said).replace(/\s+/g, " ").slice(0, ok ? 110 : 500)}` : ""}`); if (!ok) failed.push(what); };
const open = async address => { await send("Page.navigate", { url: new URL(address, HOME).href }); };

await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable");
await send("Fetch.enable", { patterns: [{ urlPattern: `${HOME}*` }] });

// The front page: the areas come first.
await open("");
check("the front page lists the areas", await until(`document.querySelectorAll("#panel li.hit.area").length > 0`), await text("#panel"));
check("routes is the part that is open, and the basket is out of the way", await js(`document.getElementById("tab-routes").getAttribute("aria-current") === "page" && getComputedStyle(document.getElementById("basket")).display === "none"`));
check("the routes are on the map", await until(`Boolean(lines) && lines.features.length > 0 && styled`));

// An area: what its routes add up to, and the routes.
check("an area can be clicked", await clickOn(`document.querySelector("#panel li.hit.area")`));
check("the area shows its numbers and its routes", await until(`document.querySelectorAll("#panel dl.facts dt").length >= 4 && document.querySelectorAll("#panel li.hit").length > 0 && Boolean(found)`), await text("#panel dl.facts"));
check("the address says which area", (await js("location.search")).includes("area="), await js("location.search"));
const row = await centre(`document.querySelector("#panel li.hit")`);
await mouse("mouseMoved", row.x, row.y); await sleep(300);
check("pointing at a route in the list shows it on the map", await js(`JSON.stringify(map.getFilter("route-hot")).includes(over.routes[0].id)`));

// A route: length, climb, grading, description.
await click(row);
check("a click on a route opens it", await until(`Boolean(route) && Boolean(shownRoute && shownRoute.detail) && document.querySelector("#panel h2 .code") !== null`), await text("#panel h2"));
check("the route says what it climbs, or that it is not known yet", await until(`(() => { const dt = [...document.querySelectorAll("#panel dl.facts dt")].find(d => d.textContent === "Climbs"); return Boolean(dt) && dt.nextElementSibling.innerText.trim().length > 3; })()`), await text("#panel dl.facts"));
await until(`Boolean(shownRoute && shownRoute.height)`, 25);        // heights not kept yet are asked for while one waits
if (await js(`Boolean(shownRoute && shownRoute.height && shownRoute.height.known_m > 0)`)) {
  check("the height profile is drawn", await until(`Boolean(document.querySelector("#panel .profile svg"))`, 5));
  const at = await centre(`document.querySelector("#panel .profile svg")`);
  await mouse("mouseMoved", at.x, at.y); await sleep(300);
  check("pointing at the height profile shows the spot on the map", await js(`Boolean(document.querySelector(".spot")) && document.querySelector("#panel .profile text.said").textContent.includes("km")`));
  await mouse("mouseMoved", 900, 900);
} else console.log("    (no heights for this route here: the profile is not checked)");
check("the route links to the editor", (await js(`[...document.querySelectorAll("#panel a.btn")].map(a => a.getAttribute("href")).join(" ")`)).includes("/ruter?yours="));
await js("history.back()");
check("the browser's back goes back to the area", await until(`route === null && document.querySelectorAll("#panel dl.facts dt").length >= 4`), await js("location.search"));

// A click on a route on the map.
const alone = await onMap(`(() => { const f = lines.features.find(f => f.properties.areas.includes("|" + area + "|") && f.properties.ids.split("|").filter(Boolean).length === 1); if (!f) return null; map.jumpTo({ center: f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)], zoom: 13 }); return f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)]; })()`);
if (alone) {
  await sleep(1500);
  const again = await onMap(`map.getCenter().toArray()`);
  await click(again);
  check("a click on a route on the map opens it", await until(`Boolean(route)`, 10), await text("#panel h2"));
  await js(`visit({ route: null })`);
} else console.log("    (no stretch here is walked by one route only: the click on the map is not checked)");

// The signs: a part one chooses.
check("the signs tab can be clicked", await clickOn(`document.getElementById("tab-signs")`));
check("the signs of the area are listed, and the basket is there", await until(`view === "signs" && document.querySelectorAll("#panel li.hit").length > 0 && getComputedStyle(document.getElementById("basket")).display !== "none"`), (await text("#panel")).slice(0, 80));
await sleep(800);
const forSigns = () => asked.filter(a => a.startsWith("/api/v1/signs?area=")).length, before = forSigns();
await clickOn(`document.querySelector("#panel li.hit")`);
check("opening a sign site asks for the signs as they are now", await (async () => { for (let i = 0; i < 20; i++) { if (forSigns() > before) return true; await sleep(250); } return false; })());
check("a sign site opens with its posts", await until(`Boolean(open) && document.querySelectorAll("#panel .post").length > 0`), await text("#panel h2"));
check("the address says which site", (await js("location.search")).includes("site="));
if (await js(`me.operates && document.querySelectorAll("#panel .plate:not([disabled])").length > 0`)) {
  await clickOn(`document.querySelector("#panel .plate:not([disabled])")`); await sleep(400);
  check("a click on a sign chooses it", (await text("#chosen")).includes("1 sign chosen"), await text("#chosen"));
  await clickOn(`[...document.querySelectorAll("#chosen button")].find(b => b.textContent === "Clear")`); await sleep(300);
  check("and it is let go of again", await js(`chosen.size === 0 && document.getElementById("chosen").hidden`));
} else console.log("    (signed in to look: choosing a sign is not checked)");
await clickOn(`document.getElementById("tab-routes")`);
check("the routes tab goes back to the routes", await until(`view === "routes" && document.querySelectorAll("#panel dl.facts dt").length >= 4`));
const dot = await js(`(() => { const s = found.sites[0]; map.jumpTo({ center: [s.lon, s.lat], zoom: 13 }); return s.key; })()`);
await sleep(1500);
await click(await onMap(`map.getCenter().toArray()`));
check("a sign site's point among the routes opens the site among the signs", await until(`view === "signs" && open === ${JSON.stringify(dot)}`, 10), await text("#panel h2"));

// The role switch: one who may change things looks at the pages as a reader, and is one to the server too.
if (await js(`Boolean(me.may_look_as && me.may_look_as.length)`)) {
  const holds = await js("me.role"), where = await js("area");
  check("the foot of the side offers to show the pages as a lesser role", (await text(".role-switch")).includes("Show the pages as"), await text(".role-switch"));
  await js(`(() => { const s = document.querySelector(".role-switch select"); s.value = "reader"; s.dispatchEvent(new Event("change")); })()`);
  check("as a reader, the page says so and the server answers as to a reader", await until(`typeof me !== "undefined" && me.role === "reader" && me.really === ${JSON.stringify(holds)} && !me.operates && /Shown as a reader sees it/.test((document.querySelector(".role-switch") || {}).innerText || "")`, 20), await text(".role-switch"));
  await open(`?area=${where}&view=signs`);
  await until(`view === "signs" && Boolean(found) && document.querySelectorAll("#panel li.hit").length > 0`);
  await clickOn(`document.querySelector("#panel li.hit")`);
  check("a reader can choose no sign and is offered nothing to change", await until(`Boolean(open) && document.querySelectorAll("#panel .plate").length > 0 && document.querySelectorAll("#panel .plate:not([disabled])").length === 0 && ![...document.querySelectorAll("#panel button")].some(b => /No signs here|Another post|Change/.test(b.textContent))`, 10));
  check("and is refused by the server, not only by the page", await js(`fetch("/api/v1/signs/undo", { headers: THROUGH }).then(r => r.status === 403)`));
  await clickOn(`[...document.querySelectorAll(".role-switch button")].find(b => b.textContent.startsWith("Back to"))`);
  check("back as oneself", await until(`typeof me !== "undefined" && me.role === ${JSON.stringify(holds)} && me.really === null`, 20), await text(".role-switch"));
  await until(`Boolean(over)`);
} else console.log("    (asked with a token, or by one with no lesser role to look as: the role switch is not checked)");

// The route editor.
const yours = await js(`over.routes[0].id`);
await open("ruter");
check("the editor opens with its map", await until(`typeof map !== "undefined" && map.loaded() && document.getElementById("editing").textContent.length > 0`), await text("#editing"));
await open(`ruter?yours=${yours}`);
check("the editor opens one of your routes from its address", await until(`[...document.querySelectorAll("#selected dt")].some(d => d.textContent === "On foot")`), (await text("#selected")).slice(0, 80));
if (await js(`Boolean(openRoute ? openRoute.height.known_m : document.querySelector("#selected .profile"))`))
  check("and draws its height profile with the same code", await until(`Boolean(document.querySelector("#selected .profile svg"))`, 25));

check("the editor has read the lines of your routes by itself, when the page opened", await until(`ownedLines.features.length > 0`, 30));

// Where several routes run on one line, a click asks which, by code and name.
const shared = await js(`(() => { const f = ownedLines.features.find(f => f.properties.routes.length >= 2 && f.geometry.coordinates.length > 3);
  return f ? JSON.stringify({ at: f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)], routes: f.properties.routes.map(r => ({ id: r.id, code: r.code })) }) : null; })()`);
if (shared) {
  const { at, routes: sharing } = JSON.parse(shared), wasOpen = await js("openRoute.id");
  await js(`(() => { clearRoute(); map.jumpTo({ center: ${JSON.stringify(at)}, zoom: 14 }); })()`); await until(`map.loaded() && !map.isMoving()`, 15); await sleep(900);
  await click(await onMap(JSON.stringify(at)));
  const names = JSON.parse(await js(`namesOf(${JSON.stringify(sharing.map(r => r.id))}).then(JSON.stringify)`));
  const wanted = sharing.map((r, i) => r.code + (names[i] ? " " + names[i] : "")).sort();
  check("several routes on one line: the chooser gives each by code and name", await until(`JSON.stringify([...document.querySelectorAll(".maplibregl-popup button[data-route]")].map(b => b.textContent).sort()) === ${JSON.stringify(JSON.stringify(wanted))}`, 15), `${await text(".maplibregl-popup")} (wanted ${wanted.join(" | ")})`);
  await js(`(() => { closePopups(); return openOwnedRoute(${JSON.stringify(wasOpen)}); })()`); await until(`Boolean(openRoute) && openRoute.id === ${JSON.stringify(wasOpen)}`, 20);
} else console.log("    (no line that two routes share: the chooser is not tried)");

check("a place says what lies round it", await js(`(async () => { const all = await get("places.geojson"), one = all.features.find(f => f.properties.type === "place"); if (!one) return true;
  const found = await get("places/" + one.properties.id + "/around"); return Array.isArray(found.ends) && Array.isArray(found.places); })()`));

// Changing things, where that was asked for: a name changed three times, taken back and brought back.
if (EDIT) {
  editing = true;
  const id = await js("openRoute.id"), was = await js("openRoute.name"), named = n => `Boolean(openRoute) && openRoute.name === ${JSON.stringify(n)}`;
  const trial = ["one", "two", "three"].map(n => `Trial ${n} ${Date.now() % 100000}`);     // never a name it has already
  for (const name of trial)
    await js(`(async () => { await send("PATCH", "owned/routes/${id}", { version: openRoute.version, name: ${JSON.stringify(name)} }); renderOwnedRoute(await get("owned/routes/${id}")); await refreshUndo(); return true; })()`);
  check("Undo offers the latest change, and there is nothing to bring back yet", await until(`/renamed/.test(document.getElementById("undo").textContent) && document.getElementById("redo").hidden && !document.getElementById("morelatest").hidden`), await text("#undobar"));
  await clickOn(`document.getElementById("undo")`);
  check("Undo takes it back, and Redo offers to bring it back", await until(`${named(trial[1])} && /Redo.*renamed/.test(document.getElementById("redo").textContent)`), await text("#undobar"));
  await clickOn(`document.getElementById("redo")`);
  check("Redo brings it back, and has nothing more", await until(`${named(trial[2])} && document.getElementById("redo").hidden`), await text("#undobar"));
  await clickOn(`document.getElementById("morelatest")`);
  check("the latest changes are listed, newest first", await until(`document.querySelectorAll("#latest li").length >= 3 && /Back to before this \\(3\\)/.test(document.querySelectorAll("#latest li")[2].textContent)`), (await text("#latest ol")).slice(0, 400));
  await clickOn(`document.querySelectorAll("#latest li button")[2]`);
  check("going back to before the first of them takes all three back in one go", await until(`Boolean(openRoute) && openRoute.name === ${JSON.stringify(was)} && document.getElementById("latest").hidden && !document.getElementById("redo").hidden`), await js("openRoute && openRoute.name"));
  const key = type => send("Input.dispatchKeyEvent", { type, modifiers: 10, key: "Z", code: "KeyZ", windowsVirtualKeyCode: 90 });
  await js(`document.activeElement && document.activeElement.blur()`);
  for (const name of trial) { await key("keyDown"); await key("keyUp"); await until(`${named(name)} && !document.getElementById("redo").disabled`, 10); }
  check("Ctrl, Shift and Z bring them back one at a time, in the order they were made", await js(named(trial[2])), await js("openRoute && openRoute.name"));
  await clickOn(`document.getElementById("morelatest")`);
  await until(`document.querySelectorAll("#latest li button").length >= 3`);
  await clickOn(`document.querySelectorAll("#latest li button")[2]`);
  check("and the route is left as it was found", await until(`Boolean(openRoute) && openRoute.name === ${JSON.stringify(was)}`), await js("openRoute && openRoute.name"));
  // An end dragged with the mouse to the junction before it: what that would give is shown while it is held,
  // it is done when let go, and Undo takes it back.
  const several = await js(`(() => { const n = {}; for (const f of ownedLines.features) for (const r of f.properties.routes) n[r.id] = (n[r.id] || 0) + 1;
    return Object.keys(n).filter(id => n[id] >= 3 && n[id] <= 12).sort()[0] || null; })()`);       // a route of a few stretches, one line
  if (several) { await js(`openOwnedRoute(${JSON.stringify(several)})`); await until(`Boolean(openRoute) && openRoute.id === ${JSON.stringify(several)}`, 20); }
  const far = await js(`(() => { if (openRoute.shape !== "line") return null; const ends = new Set(openRoute.ends.map(e => e.id)), end = openRoute.ends[0];
    const inner = openRoute.nodes.filter(n => !ends.has(n.id)).sort((a, b) => Math.hypot(a.lon - end.lon, a.lat - end.lat) - Math.hypot(b.lon - end.lon, b.lat - end.lat))[0];
    if (!inner) return null;
    map.fitBounds([[Math.min(end.lon, inner.lon), Math.min(end.lat, inner.lat)], [Math.max(end.lon, inner.lon), Math.max(end.lat, inner.lat)]], { padding: 260, animate: false, maxZoom: 16 });
    return JSON.stringify({ end: [end.lon, end.lat], inner: [inner.lon, inner.lat], legs: openRoute.length.legs }); })()`);
  if (far) {
    const { end, inner, legs } = JSON.parse(far);
    await until(`map.loaded() && map.queryRenderedFeatures({ layers: ["selected-nodes"] }).some(f => f.properties.end)`, 20); await sleep(600);
    const from = await onMap(JSON.stringify(end)), to = await onMap(JSON.stringify(inner));
    await mouse("mouseMoved", from.x, from.y); await sleep(150);
    await mouse("mousePressed", from.x, from.y, 1);
    for (let i = 1; i <= 12; i++) { await mouse("mouseMoved", from.x + (to.x - from.x) * i / 12, from.y + (to.y - from.y) * i / 12, 1); await sleep(30); }
    check("held still, an end says what letting go there would give, before anything is changed", await until(`/^Let go: .* ends .*cut back by \\d+ stretch/.test(document.getElementById("hint").textContent) && openRoute.length.legs === ${legs}`, 15), await text("#hint"));
    check("and what it says is not hidden behind Undo", await js(`document.getElementById("hint").getBoundingClientRect().top >= document.getElementById("undobar").getBoundingClientRect().bottom && !document.getElementById("hint").hidden`));
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
    check("Escape lets go of what is held: nothing is changed, and the route stays open", await until(`drag === null && document.getElementById("hint").hidden && Boolean(openRoute) && openRoute.length.legs === ${legs}`, 5));
    await mouse("mouseReleased", to.x, to.y); await sleep(300);
    await mouse("mouseMoved", from.x, from.y); await sleep(150);
    await mouse("mousePressed", from.x, from.y, 1);
    for (let i = 1; i <= 12; i++) { await mouse("mouseMoved", from.x + (to.x - from.x) * i / 12, from.y + (to.y - from.y) * i / 12, 1); await sleep(30); }
    await sleep(700);
    await mouse("mouseReleased", to.x, to.y);
    check("let go, the route is cut back to there", await until(`Boolean(openRoute) && openRoute.length.legs < ${legs}`, 20), await js("openRoute && openRoute.length.legs"));
    await until(`/end of .* moved/.test(document.getElementById("undo").textContent)`, 10);
    await clickOn(`document.getElementById("undo")`);
    check("and Undo puts the end back", await until(`Boolean(openRoute) && openRoute.length.legs === ${legs}`, 20), await text("#undobar"));
  } else console.log("    (no route of a few stretches in one line was found: dragging an end is not tried)");
  // Two places that are one: a place made where the route ends and one at the junction before it, by the
  // same name. From the panel of the first, the other is taken in; Undo takes that back, and then the two.
  const twice = await js(`(async () => { const ends = new Set(openRoute.ends.map(e => e.id)), end = openRoute.ends.find(e => !e.place);
    const inner = end && openRoute.nodes.filter(n => !ends.has(n.id)).sort((p, q) => Math.hypot(p.lon - end.lon, p.lat - end.lat) - Math.hypot(q.lon - end.lon, q.lat - end.lat))[0];
    if (!end || !inner) return null;
    const name = "Trial hut " + Date.now() % 100000, a = await send("POST", "places/at-junction", { node_id: end.id, name }), b = await send("POST", "places/at-junction", { node_id: inner.id, name });
    await refreshOwned(); await openPlace(a.id); return JSON.stringify({ a: a.id, b: b.id }); })()`);
  if (twice && !twice.startsWith("EXCEPTION")) {
    const { a, b } = JSON.parse(twice), take = `[...document.querySelectorAll("#place button")].find(x => x.textContent === "They are one place: take it in")`;
    check("the panel of a place lists another of the same name near it", await until(`/Round this place/.test(document.getElementById("place").textContent) && Boolean(${take})`, 20), (await text("#place")).slice(250, 700));
    await clickOn(take);
    check("taken in, the two are one place with both arrival points", await until(`/Access points \\(2\\)/.test(document.getElementById("place").textContent) && !${take}`, 20), (await text("#place")).slice(0, 160));
    check("and the other is no longer a place", await js(`get("places/${b}").then(() => false, () => true)`));
    check("Undo offers to take that back", await until(`/Undo\\s*two places made one$/.test(document.getElementById("undo").textContent)`, 10), await text("#undo"));
    for (let i = 0; i < 3; i++) { await clickOn(`document.getElementById("undo")`); await until(`!document.getElementById("undo").disabled`, 10); await sleep(400); }
    check("and with the two places taken back as well, nothing of the trial is left", await js(`Promise.all([get("places/${a}").then(() => false, () => true), get("places/${b}").then(() => false, () => true)]).then(gone => gone.every(Boolean))`));
  } else console.log(`    (no end without a place on a route of several stretches: making two places one is not tried) ${twice || ""}`);
  // Save or discard: what has been changed since the last save is counted, kept or taken back as a whole.
  const stays = `Stays ${Date.now() % 100000}`, goes = `Goes ${Date.now() % 100000}`, here = await js("openRoute.id"), found = await js("openRoute.name");
  const rename = name => js(`(async () => { await send("PATCH", "owned/routes/${here}", { version: openRoute.version, name: ${JSON.stringify(name)} }); await reloadOpen(); return true; })()`);
  const press = label => clickOn(`[...document.querySelectorAll("#unsaved button")].find(b => b.textContent === ${JSON.stringify(label)})`);
  await js(`saveAll()`); await until(`document.getElementById("unsaved").hidden`, 10);
  await rename(stays);
  check("a change is seen to take: it is counted as not saved", await until(`/^1 change not saved/.test(document.getElementById("unsaved").textContent) && !document.getElementById("unsaved").hidden`, 15), await text("#unsaved"));
  await press("Save");
  check("Save says it stays, and the count starts again", await until(`/^Saved: 1 change/.test(document.getElementById("unsaved").textContent)`, 10) && await until(`document.getElementById("unsaved").hidden`, 10), await text("#unsaved"));
  await rename(goes); await rename(`${goes} again`);
  check("two more changes are counted", await until(`/^2 changes not saved/.test(document.getElementById("unsaved").textContent)`, 15), await text("#unsaved"));
  await press("Discard");
  check("Discard takes both back in one go, and what was saved stays", await until(`Boolean(openRoute) && openRoute.name === ${JSON.stringify(stays)} && document.getElementById("unsaved").hidden`, 20), await js("openRoute && openRoute.name"));
  await clickOn(`document.getElementById("undo")`);
  check("and the trial is taken back", await until(`Boolean(openRoute) && openRoute.name === ${JSON.stringify(found)}`, 15), await js("openRoute && openRoute.name"));
  // From one route to another on the map: a click on the line of another route opens it, and asks first what
  // is to become of changes that are not saved. Two routes that share no stretch are found, and one opened.
  const pair = await js(`(() => { const lone = ownedLines.features.filter(f => JSON.parse(JSON.stringify(f.properties.routes)).length === 1 && f.geometry.coordinates.length > 3);
    const a = lone.find(f => f.properties.routes[0].id === openRoute.id) || lone[0], b = a && lone.find(f => f.properties.routes[0].id !== a.properties.routes[0].id);
    const mid = f => f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)];
    return a && b ? JSON.stringify({ a: a.properties.routes[0], b: b.properties.routes[0], at: mid(b), back: mid(a) }) : null; })()`);
  if (pair) {
    const { a, b, at, back } = JSON.parse(pair), isOpen = r => `Boolean(openRoute) && openRoute.id === ${JSON.stringify(r.id)}`;
    const show = async where => { await js(`map.jumpTo({ center: ${JSON.stringify(where)}, zoom: 14 })`); await until(`map.loaded() && !map.isMoving()`, 15); await sleep(900); return onMap(JSON.stringify(where)); };
    await js(`openOwnedRoute(${JSON.stringify(a.id)})`); await until(isOpen(a), 20);
    await js(`saveAll()`); await until(`unsaved.changes.length === 0`, 10);
    await click(await show(at));
    check("a click on the line of another route opens that route", await until(isOpen(b), 20), await js("openRoute && openRoute.code"));
    const was = await js("openRoute.name"), trial = `Leaving ${Date.now() % 100000}`;
    await js(`(async () => { await send("PATCH", "owned/routes/${b.id}", { version: openRoute.version, name: ${JSON.stringify(trial)} }); await reloadOpen(); return true; })()`);
    await until(`unsaved.changes.length === 1`, 15);
    await click(await show(back));
    const asks = `[...document.querySelectorAll(".maplibregl-popup button")]`;
    const called = await js(`namesOf([${JSON.stringify(a.id)}]).then(n => n[0])`);
    check(`with a change not saved, it asks first, and says what the route is called${called ? "" : " (this one has no name)"}`, await until(`/1 change not saved/.test((document.querySelector(".maplibregl-popup") || {}).textContent || "") && ${asks}.some(x => x.textContent === "Discard, and open " + ${JSON.stringify(a.code + (called ? " " + called : ""))})`, 15), await text(".maplibregl-popup"));
    check("and the route is still the one that was open", await js(isOpen(b)));
    await clickOn(`${asks}.find(x => x.textContent.startsWith("Discard, and open "))`);
    check("Discard, and open: the change is taken back and the other route is open", await until(`${isOpen(a)} && unsaved.changes.length === 0`, 20) && await js(`get("owned/routes/${b.id}").then(r => r.name === ${JSON.stringify(was)})`), await js("openRoute && openRoute.code"));
    check("the panel has carrying on along another line as a tool of its own", await js(`[...document.querySelectorAll("#selected button")].some(x => x.textContent === "Carry it on along another line…")`));
  } else console.log("    (no two routes with a stretch of their own each: going from one to the other is not tried)");
  editing = false;
} else console.log("    (not asked to change anything: undo, redo, dragging, places, saving and changing route are not tried)");

check("the pages threw nothing", thrown.length === 0, thrown.join(" | "));
const boxes = said.filter(s => !/^Take .* into /.test(s));       // that one asks before two places are made one, and is answered yes
check("and put up no box to say that something went wrong", boxes.length === 0, boxes.join(" | "));
const unasked = refused.filter(r => !r.startsWith("403 /api/v1/signs/undo") && !(EDIT && /^404 \/api\/v1\/places\/[0-9a-f-]{36}$/.test(r)));      // that one was asked for as a reader, to be refused
check("the service answered every request of its own", unasked.length === 0, unasked.join(" | "));
check("nothing was changed", stopped.length === 0, stopped.join(" | "));
console.log(failed.length ? `\n${failed.length} failed` : "\nall passed");
ws.close(); chrome.kill(); await sleep(300); rmSync(profile, { recursive: true, force: true });
process.exit(failed.length ? 1 : 0);
