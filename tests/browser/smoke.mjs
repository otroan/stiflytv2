// A walk through both pages in a real browser, with real clicks: the front page (areas, an area, a route,
// the signs) and the route editor. It changes nothing: every request but GET is stopped before it leaves.
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
const waiting = new Map(), thrown = [], refused = [], stopped = [], failed = [];
const send = (method, params = {}) => new Promise(r => { waiting.set(++seq, r); ws.send(JSON.stringify({ id: seq, method, params })); });
ws.addEventListener("message", m => {
  const d = JSON.parse(m.data);
  if (d.id && waiting.has(d.id)) { waiting.get(d.id)(d.result); waiting.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") thrown.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === "Network.responseReceived" && d.params.response.url.startsWith(HOME) && d.params.response.status >= 400 && !d.params.response.url.endsWith("/favicon.ico"))
    refused.push(`${d.params.response.status} ${d.params.response.url.slice(HOME.length - 1)}`);
  if (d.method === "Fetch.requestPaused") {
    const q = d.params.request;
    if (q.method !== "GET") { stopped.push(`${q.method} ${q.url.slice(HOME.length - 1)}`); send("Fetch.failRequest", { requestId: d.params.requestId, errorReason: "BlockedByClient" }); return; }
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
const check = (what, ok, said = "") => { console.log(`${ok ? "ok  " : "FAIL"} ${what}${said ? `: ${String(said).replace(/\s+/g, " ").slice(0, 110)}` : ""}`); if (!ok) failed.push(what); };
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
await clickOn(`document.querySelector("#panel li.hit")`);
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

check("the pages threw nothing", thrown.length === 0, thrown.join(" | "));
const unasked = refused.filter(r => !r.startsWith("403 /api/v1/signs/undo"));      // that one was asked for as a reader, to be refused
check("the service answered every request of its own", unasked.length === 0, unasked.join(" | "));
check("nothing was changed", stopped.length === 0, stopped.join(" | "));
console.log(failed.length ? `\n${failed.length} failed` : "\nall passed");
ws.close(); chrome.kill(); await sleep(300); rmSync(profile, { recursive: true, force: true });
process.exit(failed.length ? 1 : 0);
