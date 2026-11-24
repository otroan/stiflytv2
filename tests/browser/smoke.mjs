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
let pretend = null;         // what the service is made to say about who else is here
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
    // Somebody else at work, for the page to show: the answer is made up here. With a token it is always made up
    // here, nobody else: the service takes where a page is only from a page a person has open, and keeps the refusal.
    if ((pretend || TOKEN) && /\/api\/v1\/here$/.test(q.url)) { send("Fetch.fulfillRequest", { requestId: d.params.requestId, responseCode: 200, responseHeaders: [{ name: "Content-Type", value: "application/json" }], body: Buffer.from(JSON.stringify(pretend || [])).toString("base64") }); return; }
    // (Planning a trip, and saying where the page is, are asked with POST and keep nothing: they are let through, as a look is.)
    if (q.method !== "GET" && !editing && !/\/api\/v1\/(trips\/plan|planner\/plan|planner\/estimate|planner\/heights|here)$/.test(q.url)) { stopped.push(`${q.method} ${q.url.slice(HOME.length - 1)}`); send("Fetch.failRequest", { requestId: d.params.requestId, errorReason: "BlockedByClient" }); return; }
    send("Fetch.continueRequest", { requestId: d.params.requestId, headers: [...Object.entries(q.headers).map(([name, value]) => ({ name, value })), ...(TOKEN ? [{ name: "Authorization", value: `Bearer ${TOKEN}` }] : [])] });
  }
});
const js = async expr => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); return !r ? undefined : r.exceptionDetails ? `EXCEPTION ${r.exceptionDetails.exception?.description || r.exceptionDetails.text}` : r.result.value; };
const until = async (expr, seconds = 30) => { for (let i = 0; i < seconds * 4; i++) { if (await js(expr) === true) return true; await sleep(250); } return false; };
const mouse = (type, x, y, buttons = 0) => send("Input.dispatchMouseEvent", { type, x, y, button: "left", buttons, clickCount: 1 });
const click = async at => { await mouse("mouseMoved", at.x, at.y); await mouse("mousePressed", at.x, at.y, 1); await mouse("mouseReleased", at.x, at.y); };
const centre = async sel => JSON.parse(await js(`(() => { const e = ${sel}; if (!e) return "null"; e.scrollIntoView({ block: "center" }); const r = e.getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }); })()`));
// A lens under Routes, or a kind of plan under Plan: its row is there once its part is open.
const LENSES = { "lens-routes": "routes", "tab-issues": "issues", "tab-reports": "reports", "tab-photos": "photos", "tab-landowners": "landowners",
                 "tab-planner": "planner", "tab-trips": "trips" };
const toPart = async id => {
  const v = LENSES[id], plan = v === "planner" || v === "trips", top = plan ? "tab-plan" : "tab-routes", row = plan ? "plans" : "lenses";
  if (await js(`view === ${JSON.stringify(v)}`)) return true;
  if (await js(`document.getElementById("${row}").hidden`)) {
    await clickOn(`document.getElementById("${top}")`);
    await until(`partOf(view) === ${JSON.stringify(plan ? "plan" : "routes")}`, 15);
    if (await js(`view === ${JSON.stringify(v)}`)) return true;
  }
  return clickOn(`document.getElementById("${id}")`);
};
const clickOn = async sel => { const at = await centre(sel); if (!at) return false; await click(at); return true; };
const text = sel => js(`(document.querySelector(${JSON.stringify(sel)}) || {}).innerText || ""`);
const onMap = expr => js(`(() => { const c = ${expr}; if (!c) return "null"; const p = map.project(c), r = document.getElementById("map").getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + p.x), y: Math.round(r.top + p.y) }); })()`).then(JSON.parse);
const check = (what, ok, said = "") => { console.log(`${ok ? "ok  " : "FAIL"} ${what}${said ? `: ${String(said).replace(/\s+/g, " ").slice(0, ok ? 110 : 500)}` : ""}`); if (!ok) failed.push(what); };
const open = async address => { await send("Page.navigate", { url: new URL(address, HOME).href }); };

await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable");
await send("Fetch.enable", { patterns: [{ urlPattern: `${HOME}*` }] });

// The front page: the areas come first; for one whose profile names areas, the first of them, and the areas a step back.
await open("");
await until(`typeof me !== "undefined" && me.name !== undefined && (document.querySelectorAll("#panel li.hit.area").length > 0 || Boolean(area))`, 30);
const profiled = await js(`Boolean(me.profile && me.profile.areas.length)`);
if (profiled) {
  check("the front page opens with the first of one's areas", await until(`area === me.profile.areas[0]`, 20), await js("location.search"));
  await js(`visit({ area: null, route: null, site: null })`);
}
check("the front page lists the areas", await until(`document.querySelectorAll("#panel li.hit.area").length > 0`), await text("#panel"));
// The photos on the map are out of the way of the clicks on routes and sign sites, until the photos are tried.
await js(`(() => { if (typeof photosShown !== "undefined") { photosShown = false; drawPhotos(); } })()`);
check("routes is the part that is open, and the basket is out of the way", await js(`document.getElementById("tab-routes").getAttribute("aria-current") === "page" && getComputedStyle(document.getElementById("basket")).display === "none"`));
check("the routes are on the map", await until(`Boolean(lines) && lines.features.length > 0 && styled`));
// The search: a route by its code, opened where it is; and a place, the map gone there.
{
  const type = q => js(`(() => { const s = document.getElementById("search"); s.focus(); s.value = ${JSON.stringify(q)}; s.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  const first = await js(`get("search/routes", { q: "bre" }).then(r => r.length ? r[0].code : null)`);
  check("there are routes of ours to search for", Boolean(first));
  if (first) {
    await type(first);
    check("the search finds a route by its code", await until(`!document.getElementById("search-found").hidden && [...document.querySelectorAll("#search-found li.hit .code")].some(e => e.textContent === ${JSON.stringify(first)})`, 15),
      await text("#search-found"));
    await js(`document.getElementById("search").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }))`);
    check("and Enter opens it, in its area, from wherever one is", await until(`route === ${JSON.stringify(first)} && area === "bre" && Boolean(shownRoute && shownRoute.detail) && document.getElementById("search-found").hidden`, 20), await js("location.search"));
    await js("history.back()");
    check("and the browser's back goes back to where one was", await until(`route === null && area === null`, 15), await js("location.search"));
  }
  await type("Sprongdalshytta");
  check("the search finds a place by its name", await until(`[...document.querySelectorAll("#search-found li.hit")].some(e => /^Sprongdalshytta/.test(e.textContent))`, 15), await text("#search-found"));
  await js(`[...document.querySelectorAll("#search-found li.hit")].find(e => /^Sprongdalshytta/.test(e.textContent)).dispatchEvent(new MouseEvent("mousedown", { bubbles: true }))`);
  check("and the map goes there, saying what it is", await until(`Boolean(searchPin) && searchPin.isOpen() && /Sprongdalshytta/.test(searchPin.getElement().textContent)`, 15));
  await js("searchPin.remove()");
}
check("at the top three parts, and under the routes the lenses one has", await js(`[...document.querySelectorAll("nav[aria-label='Parts of Stiflyt'] a")].filter(a => !a.hidden).map(a => a.textContent).join(",") === "Routes,Signs,Plan"
  && !document.getElementById("lenses").hidden && document.getElementById("lens-routes").getAttribute("aria-current") === "page" && document.getElementById("plans").hidden`),
  await js(`[...document.querySelectorAll("header nav a")].filter(a => !a.hidden).map(a => a.textContent).join(",")`));

// Your areas (the profile): chosen and saved, listed first, and the front page opens with the first of them. And a route
// added to an area from another counts there, also on the map.
if (EDIT) {
  editing = true;
  await clickOn(`[...document.querySelectorAll("#panel button")].find(b => /areas…$/.test(b.textContent))`);
  await until(`Boolean(choosingAreas)`, 5);
  const mine = await js(`areas[areas.length - 1].area`);
  await js(`[...document.querySelectorAll("#panel li.area")].find(li => li.querySelector(".code").textContent === ${JSON.stringify(mine)}).querySelector("input").click()`);
  await js(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Save your areas").click()`);
  check("your areas are saved, and listed first", await until(`!choosingAreas && me.profile.areas[0] === ${JSON.stringify(mine)} && /Your areas/.test(document.getElementById("panel").textContent)`, 10), await text("#panel"));
  await open("");
  check("the front page opens with the first of your areas", await until(`typeof area !== "undefined" && area === ${JSON.stringify(mine)} && Boolean(over)`, 20), await js("location.search"));
  const lent = await js(`over.routes[0].code`);
  await js(`(async () => { await send("POST", "areas/xyz/added", { code: ${JSON.stringify(lent)} }); areas = await get("owned/areas"); lines = inUse(rawLines); })()`);
  check("a route added to another area counts there, in the areas and on the map", await until(`areas.some(a => a.area === "xyz" && a.routes === 1)
    && lines.features.some(f => f.properties.codes_in_use.includes("|" + ${JSON.stringify(lent)} + "|") && f.properties.areas.includes("|xyz|"))`, 10));
  // In that area its reports list has it; taken out in the page, every list of the area follows at once.
  await js(`visit({ area: "xyz", view: "reports", route: null, site: null })`);
  check("the area it was added to lists it among its routes to report on", await until(`reportsFor === "xyz" && upkeep.some(r => r.code === ${JSON.stringify(lent)})`, 15));
  await js(`visit({ view: "routes" })`);
  await until(`view === "routes" && Boolean(over) && Boolean([...document.querySelectorAll("#panel details button.link")].find(b => b.textContent === "take out"))`, 15);
  await js(`[...document.querySelectorAll("#panel details button.link")].find(b => b.textContent === "take out").click()`);
  check("taken out in the page, the area's reports list follows at once", await until(`Array.isArray(upkeep) && !upkeep.some(r => r.code === ${JSON.stringify(lent)})
    && !areas.some(a => a.area === "xyz")`, 15));
  await js(`(async () => { await send("PUT", "profile", { areas: [] }); me.profile = { areas: [], maintainer: null }; visit({ area: null, route: null, site: null }); })()`);
  await until(`area === null && document.querySelectorAll("#panel li.hit.area").length > 0`, 10);
  editing = false;
}

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
if (await js(`hasReports()`)) {
  // Another lens with the route open: the route stays open, seen through it; and back.
  const opened = await js("route");
  await clickOn(`document.getElementById("tab-reports")`);
  check("a lens chosen with a route open keeps the route open, seen through it", await until(`view === "reports" && route === ${JSON.stringify(opened)} && Boolean(shownRoute)`, 15), await js("location.search"));
  await js("history.back()");
  check("and the browser's back goes back to the overview, the route still open", await until(`view === "routes" && route === ${JSON.stringify(opened)} && Boolean(shownRoute && shownRoute.detail)`, 15), await js("location.search"));
}
check("the route says what it climbs, or that it is not known yet", await until(`(() => { const dt = [...document.querySelectorAll("#panel dl.facts dt")].find(d => d.textContent === "Climbs"); return Boolean(dt) && dt.nextElementSibling.innerText.trim().length > 3; })()`), await text("#panel dl.facts"));
await until(`Boolean(shownRoute && shownRoute.height)`, 25);        // heights not kept yet are asked for while one waits
if (await js(`Boolean(shownRoute && shownRoute.height && shownRoute.height.known_m > 0)`)) {
  check("the height profile is drawn", await until(`Boolean(document.querySelector("#panel .profile svg"))`, 5));
  // The route's photos and reports come after it, and draw the side anew: the spot is pointed at again until it holds.
  let spotted = false;
  for (let i = 0; i < 10 && !spotted; i++) {
    const at = await centre(`document.querySelector("#panel .profile svg")`);
    await mouse("mouseMoved", at.x + i % 2, at.y); await sleep(300);
    spotted = await js(`Boolean(document.querySelector(".spot")) && document.querySelector("#panel .profile text.said").textContent.includes("km")`);
  }
  check("pointing at the height profile shows the spot on the map", spotted);
  await mouse("mouseMoved", 900, 900);
} else console.log("    (no heights for this route here: the profile is not checked)");
check("the route links to the editor", (await js(`[...document.querySelectorAll("#panel a.btn")].map(a => a.getAttribute("href")).join(" ")`)).includes("/ruter?yours="));
// The route card: the parts to choose, and the card itself, a page of its own.
await clickOn(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Route card…")`);
check("Route card… asks which parts go on the card", await until(`Boolean(document.querySelector("#panel .card-ask")) && document.querySelectorAll("#panel .card-ask input[type=checkbox]").length >= 6`, 5),
  await text("#panel .card-ask"));
check("the card is a page of the route, with its facts, map and profile", await js(`(async () => { const r = over.routes.find(x => x.code === route);
  const got = await fetch(new URL("routecard/" + r.id + "?with=map,profile,walk", API), { headers: THROUGH }); const page = await got.text();
  return got.ok && page.includes("<h1>") && page.includes(r.code) && page.includes("cache.kartverket.no") && page.includes("Section by section"); })()`));
await clickOn(`[...document.querySelectorAll("#panel .card-ask button")].find(b => b.textContent === "Cancel")`);
await js("history.back()");
check("the browser's back goes back to the area", await until(`route === null && document.querySelectorAll("#panel dl.facts dt").length >= 4`), await js("location.search"));

// A click on a route on the map.
const alone = await onMap(`(() => { const f = lines.features.filter(f => f.properties.areas.includes("|" + area + "|") && f.properties.ids.split("|").filter(Boolean).length === 1)
  .sort((a, b) => b.geometry.coordinates.length - a.geometry.coordinates.length)[0]; if (!f) return null; map.jumpTo({ center: f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)], zoom: 13 }); return f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)]; })()`);
if (alone) {
  await sleep(1500);
  const again = await onMap(`map.getCenter().toArray()`);
  await sleep(300);
  await mouse("mouseMoved", again.x + 2, again.y); await mouse("mouseMoved", again.x, again.y);
  check("pointing at a route on the map says which, how long it is, and what the stretch is", await until(`Boolean(document.querySelector(".map-tip"))
    && /\\d km/.test(document.querySelector(".map-tip").textContent) && /This stretch: /.test(document.querySelector(".map-tip").textContent)`, 5), await text(".map-tip"));
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
await toPart("lens-routes");
check("the routes tab goes back to the routes", await until(`view === "routes" && document.querySelectorAll("#panel dl.facts dt").length >= 4`));
const dot = await js(`(() => { const s = found.sites[0]; map.jumpTo({ center: [s.lon, s.lat], zoom: 13 }); return s.key; })()`);
await sleep(1500);
await click(await onMap(`map.getCenter().toArray()`));
check("a sign site's point among the routes opens the site among the signs", await until(`view === "signs" && open === ${JSON.stringify(dot)}`, 10), await text("#panel h2"));

// Trips: a feature of its own. Without it the tab is not there. With it, a trip is planned, listed and drawn.
if (await js(`(me.features || []).includes("trips")`)) {
  await toPart("tab-trips");
  check("the trips tab asks which routes, where from and how long a day is", await until(`view === "trips" && Boolean(document.querySelector("form.trip-ask button.main")) && /Every route in use here/.test(document.querySelector("form.trip-ask").textContent)`, 20), await text("#panel h2"));
  await clickOn(`document.querySelector("form.trip-ask button.main")`);
  check("a trip is planned: every day is in the list, and a line on the map", await until(`Boolean(trip) && trip.days.length > 0 && document.querySelectorAll(".trip-days > li").length === trip.days.length && Boolean(map.getLayer("trip")) && trip.covered.length > 0`, 60), `${await text("#panel h2.next")} ${await text("#panel .error")}`);
  check("each day has walking instructions, in order", await js(`trip.days.every(d => d.steps.length > 0) && document.querySelectorAll(".trip-days li .trip-steps li").length === trip.days.reduce((n, d) => n + d.steps.length, 0) && /^(Along|Back the same way) .* to /.test(document.querySelector(".trip-steps li").textContent)`), await text(".trip-steps"));
  check("each day says from where to where, how far, the climb and the time", await js(`/ – .*\\d km, .* m up, \\d+ h \\d\\d/.test(document.querySelector(".trip-days li").textContent.replace(/\\s+/g, " "))`), await text(".trip-days li"));
  await clickOn(`document.querySelector(".trip-days li .grow")`);
  check("a click on a day shows that day alone", await until(`tripDay === 1 && document.querySelector(".trip-days li").classList.contains("current")`, 10));
  check("the address says the trips are open, so that it can be linked to", (await js("location.search")).includes("view=trips"));
  await toPart("lens-routes");
  await until(`view === "routes"`, 10);
} else check("the trips tab is not there for one who does not have the feature", await js(`document.getElementById("tab-trips").hidden`));

// The route planner (the feature `planner`): a click where a walk begins and one where it ends, anywhere, and the way
// between them is found, drawn in the colours of what it goes along, and timed with the heights.
if (await js(`(me.features || []).includes("planner")`)) {
  await toPart("tab-planner");
  const areaBefore = await js("localStorage.getItem('stiflyt2.area')");
  check("the planner is of no area: none is chosen, and the address names none", await until(`view === "planner" && area === null && !/[?&]area=/.test(location.search)`, 20), await js("location.search"));
  check("the planner asks where the walk begins", await until(`view === "planner" && /Plan a walk/.test(document.querySelector("#panel h2").textContent) && /Search for where the walk begins .* or click the map/.test(document.getElementById("panel").textContent)`, 20), await text("#panel h2"));
  await js(`planAsk.points.length = 0; replan(); map.jumpTo({ center: [7.402, 61.6687], zoom: 13 })`);
  await until(`!map.isMoving() && map.loaded()`, 10);
  const spot = async (lon, lat) => JSON.parse(await js(`(() => { const p = map.project([${lon}, ${lat}]); const r = document.getElementById("map").getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + p.x), y: Math.round(r.top + p.y) }); })()`));
  await click(await spot(7.38495, 61.67050));
  check("a click on the map is where it begins, a letter on the map and in the From field", await until(`planAsk.points.length === 1 && planMarkers.length === 1 && document.querySelector('[data-plan-field="from"]').placeholder.startsWith("Clicked at")`, 10));
  await click(await spot(7.41907, 61.66686));
  check("a second click is where it ends, and the way between is found and drawn", await until(`Boolean(plan) && plan.steps.length > 0 && map.getSource("plan").serialize().data.features.length === plan.steps.length`, 60), await text("#panel .error"));
  check("bridges and fords have marks of their own on the map", await js(`map.hasImage("plan-bridge") && map.hasImage("plan-ford") && map.getLayer("plan-crossings").type === "symbol"`));
  check("what the way goes along is said by kind, in the colours of the map", await js(`document.querySelectorAll(".plan-by li").length > 0 && plan.total.metres > 1000`), await text(".plan-by"));
  // Where it ends, found by its name instead: typed in the To field, and chosen among what is found.
  await js(`(() => { const f = document.querySelector('[data-plan-field="to"]'); f.focus(); f.value = "Sprongdals"; f.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  check("a place is searched for by its name as it is typed", await until(`[...document.querySelectorAll(".plan-found button")].some(b => b.textContent.startsWith("Sprongdalshytta"))`, 15), await text(".plan-found"));
  await clickOn(`[...document.querySelectorAll(".plan-found button")].find(b => b.textContent.startsWith("Sprongdalshytta"))`);
  check("chosen, it is where the walk ends, by its name, and the way is found anew", await until(`planAsk.names[planAsk.names.length - 1] === "Sprongdalshytta" && planAsk.points.length === 2
    && document.querySelector('[data-plan-field="to"]').value === "Sprongdalshytta" && !planBusy && !planStale && (Boolean(plan) || Boolean(planProblem))`, 60), `${await text("#panel h2.next")} ${await text("#panel .error")}`);
  check("the heights are asked for: the climb and the time, or why not", await until(`!heightsBusy && (Boolean(planHeights) && /of walking by Naismith/.test(document.getElementById("panel").textContent) || Boolean(heightsProblem))`, 60), await text("#panel .sub"));
  // A leg straight over the ground, where nothing is mapped: a sketch, dashed, and timed as such; and back again.
  await until(`!planBusy && !heightsBusy && !planStale`, 60);
  await clickOn(`[...document.querySelectorAll(".plan-modes button")].find(b => b.textContent === "Straight")`);
  check("a leg can be drawn straight, with no path: dashed, and said so", await until(`planAsk.modes[0] === "straight" && Boolean(plan) && !planStale && plan.legs[0].straight
    && plan.steps.every(s => s.class === "off") && map.getSource("plan").serialize().data.features.every(f => f.properties.dashed)
    && /straight, with no path/.test(document.querySelector(".plan-legs").textContent)`, 30), await text(".plan-legs"));
  check("and timed as ground with no path", await until(`!heightsBusy && (Boolean(planHeights) && planHeights.rough_m > 0 && /with no path, timed half as slow again/.test(document.getElementById("panel").textContent) || Boolean(heightsProblem))`, 60), await text("#panel .sub"));
  // Across country alone, even where there are paths: over the lattice where it is built, else drawn straight and said why.
  await until(`!planBusy && !heightsBusy && !planStale`, 60);
  await clickOn(`[...document.querySelectorAll(".plan-modes button")].find(b => b.textContent === "Across country")`);
  check("a leg can go across country even where there are paths", await until(`planAsk.modes[0] === "across" && Boolean(plan) && !planBusy && !planStale && plan.legs[0].mode === "across"
    && (plan.steps.some(s => s.class === "field") || Boolean(plan.legs[0].unfound))
    && document.querySelector(".plan-modes button[aria-pressed=true]").textContent === "Across country"`, 30), await text(".plan-legs"));
  await until(`!planBusy && !heightsBusy && !planStale`, 60);
  await clickOn(`[...document.querySelectorAll(".plan-modes button")].find(b => b.textContent === "Along paths")`);
  check("and back along paths", await until(`planAsk.modes[0] === "paths" && Boolean(plan) && !planStale && !plan.legs[0].straight && plan.steps.some(s => s.class !== "off")`, 30), await text(".plan-legs"));
  // The line taken hold of and dragged aside: a point it passes, put on the nearest path, and the way found anew.
  await until(`!planBusy && !heightsBusy`, 60);
  await js(`showPlan()`);
  await sleep(600); await until(`!map.isMoving()`, 10);
  const grip = await onMap(`(() => { const l = plan.legs[0]; return plan.coords[Math.floor((l.at[0] + l.at[1]) / 2)]; })()`);
  await mouse("mouseMoved", grip.x, grip.y); await mouse("mousePressed", grip.x, grip.y, 1);
  for (let k = 1; k <= 6; k++) { await mouse("mouseMoved", grip.x + 8 * k, grip.y + 5 * k, 1); await sleep(30); }
  await mouse("mouseReleased", grip.x + 48, grip.y + 30);
  // Let go of near a path, the point goes onto it; further off, it stays exactly where it was let go of, the way
  // straight to it: either way only the part between the junctions on either side is found anew.
  check("the line dragged aside makes a point of yours it passes, and only the part about it is found anew",
    await until(`planVisible().length === 3 && Boolean(plan) && !planBusy && !planStale && plan.legs.length === planAsk.points.length - 1
      && (() => { const v = planVisible()[1]; return planAsk.snap[v] ? !plan.off.some(o => o.point === v) : planAsk.modes[v - 1] !== "paths" && planAsk.modes[v] !== "paths"
                                                                          && plan.coords.some(c => c[0] === planAsk.points[v][0] && c[1] === planAsk.points[v][1]); })()
      && (planAsk.points.length === 3 || plan.legs.some(l => l.kept))`, 30),
    `${await js("JSON.stringify({ n: planAsk.points.length, h: planAsk.hidden, snap: planAsk.snap, modes: planAsk.modes, stale: planStale, busy: planBusy, prob: planProblem, legs: plan && plan.legs.map(l => [l.kept, l.straight]), off: plan && plan.off })")} ${await text("#panel .error")}`);
  await clickOn(`[...document.querySelectorAll(".plan-field button")].find(b => /Take point B away/.test(b.getAttribute("aria-label") || ""))`);
  check("taken away, it goes with the hidden points that held the way about it", await until(`planAsk.points.length === 2 && !planAsk.hidden.some(Boolean) && Boolean(plan) && !planBusy && plan.legs.length === 1`, 30),
    await js("JSON.stringify(planAsk.hidden)"));
  // Across country, and up in the mountains: asked for in the form, and the way found anew with them.
  await clickOn(`[...document.querySelectorAll(".trip-ask label")].find(l => /Across country/.test(l.textContent)).querySelector("input")`);
  check("across country can be asked for", await until(`planAsk.across === true && Boolean(plan) && !planBusy && plan.options.across === true`, 60), await text("#panel .error"));
  await clickOn(`[...document.querySelectorAll(".trip-ask label")].find(l => /Across country/.test(l.textContent)).querySelector("input")`);
  await until(`planAsk.across === false && Boolean(plan) && !planBusy`, 60);
  await js(`(() => { const r = [...document.querySelectorAll(".plan-prefer input")].find(i => /valleys/.test(i.getAttribute("aria-label"))); r.value = 60; r.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  check("and the mountains preferred to the valleys", await until(`planAsk.mountains === 0.6 && Boolean(plan) && !planBusy && plan.options.mountains === 0.6`, 60), await text("#panel .error"));
  await js(`(() => { const r = [...document.querySelectorAll(".plan-prefer input")].find(i => /valleys/.test(i.getAttribute("aria-label"))); r.value = 0; r.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await until(`planAsk.mountains === 0 && Boolean(plan) && !planBusy`, 60);
  // Not by itself: a change leaves the way as it was, faded and said to be from before, until "Find the way" is pressed,
  // which says how long it is thought to take; while it is found, a bar fills.
  await clickOn(`document.querySelector(".plan-auto input")`);
  await until(`planAsk.auto === false`, 5);
  await clickOn(`[...document.querySelectorAll(".trip-ask label")].find(l => /Ferries may be taken/.test(l.textContent)).querySelector("input")`);
  check("not by itself, a change waits for the button, which says about how long it takes", await until(`planStale && !planBusy && Boolean(planEstimate)
    && /^Find the way [(]about [0-9.]+ s[)]$/.test(document.querySelector(".plan-find button").textContent) && /as it was before/.test(document.getElementById("panel").textContent)`, 20),
    await text(".plan-find"));
  await clickOn(`document.querySelector(".plan-find button")`);
  check("and pressed, the way is found anew", await until(`!planStale && Boolean(plan) && !planBusy && plan.options.ferries === false`, 60),
    await text("#panel .error"));
  await clickOn(`[...document.querySelectorAll(".trip-ask label")].find(l => /Ferries may be taken/.test(l.textContent)).querySelector("input")`);
  await clickOn(`document.querySelector(".plan-auto input")`);
  await until(`planAsk.auto === true && !planBusy && !planStale`, 60);
  // Fords gone round by a bridge: asked for in the form.
  await until(`!planBusy && !heightsBusy && !planStale`, 60);
  await clickOn(`[...document.querySelectorAll(".trip-ask label")].find(l => /Go round fords/.test(l.textContent)).querySelector("input")`);
  check("fords on paths can be gone round by a bridge", await until(`planAsk.fords === false && Boolean(plan) && !planBusy && !planStale && plan.options.fords === false && Array.isArray(plan.crossings)`, 60), await text("#panel .error"));
  await clickOn(`[...document.querySelectorAll(".trip-ask label")].find(l => /Go round fords/.test(l.textContent)).querySelector("input")`);
  await until(`planAsk.fords === true && !planBusy && !planStale`, 60);
  check("the address says the planner is open, so that it can be linked to", (await js("location.search")).includes("view=planner"));
  await clickOn(`[...document.querySelectorAll("#panel .actions .btn")].find(b => b.textContent === "Start again")`);
  check("it can be started again", await until(`planAsk.points.length === 0 && !plan && planMarkers.length === 0`, 10));
  await toPart("lens-routes");
  check("out of the planner, back in the area one was in", await until(`view === "routes" && area === ${JSON.stringify(areaBefore)}`, 15), await js("location.search"));
} else check("the planner is not there for one who does not have the feature", await js(`document.getElementById("tab-planner").hidden`));

// Who else is at work now (the feature `presence`): the page says where it is, and shows the others at the foot.
const others = async (who, shown) => {
  pretend = [who];
  await js(`document.dispatchEvent(new Event("visibilitychange"))`);
  const ok = await until(`!document.getElementById("present").hidden && document.getElementById("present").textContent === ${JSON.stringify(shown)}`, 10);
  pretend = null;
  return ok;
};
if (await js(`(me.features || []).includes("presence")`)) {
  check("the page says where it is, and shows nobody while nobody else is here", asked.includes("/api/v1/here") && await js(`document.getElementById("present").hidden`));
  check("somebody else at work is shown at the foot: where, and how much is not saved",
    await others({ name: "Another Editor", page: "signs", area: "jot", site: "Gjendesheim", route: null, unsaved: 2 }, "Also here now: Another Editor (signs, jot, Gjendesheim, 2 not saved)"), await text("#present"));
} else check("nobody else is shown to one who does not have the feature", !asked.includes("/api/v1/here") && await js(`document.getElementById("present").hidden`));

// Issues and notices (the feature `issues`): reported by a click on a route, changed, taken back, and told in the route.
if (await js(`hasIssues()`)) {
  await toPart("tab-issues");
  check("the issues tab lists what is open in the area", await until(`view === "issues" && Array.isArray(entries) && /Issues in/.test(document.querySelector("#panel h2").textContent)`, 20), await text("#panel h2"));
  check("the address says the issues are open", (await js("location.search")).includes("view=issues"));
  const spot = EDIT && await onMap(`(() => { const f = lines.features.find(f => f.properties.areas.includes("|" + area + "|") && f.properties.ids.split("|").filter(Boolean).length === 1); if (!f) return null;
    window.trialLine = f.geometry.coordinates; const c = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)]; map.jumpTo({ center: c, zoom: 14 }); return c; })()`);
  if (spot) {
    editing = true;
    const trial = `Trial birch ${Date.now() % 100000}`;
    await clickOn(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Report an issue")`);
    await sleep(1500);
    await click(await onMap(`map.getCenter().toArray()`));
    check("Report, then a click on a route, asks what it is", await until(`Boolean(entryAsk) && Boolean(document.querySelector("form.entry-form"))`, 10), await text("#panel"));
    await js(`(() => { const f = document.querySelector("form.entry-form"), t = f.querySelector("textarea"); t.value = ${JSON.stringify(trial)}; t.dispatchEvent(new Event("input")); f.requestSubmit(); })()`);
    check("reported, it is open in the side and a point on the map", await until(`Boolean(entryOpen) && document.getElementById("panel").textContent.includes(${JSON.stringify(trial)})
      && map.queryRenderedFeatures({ layers: ["entries"] }).some(f => f.properties.chosen)`, 15), await text("#panel"));
    check("its history says who reported it", await until(`/Test Editor: reported it on/.test(document.querySelector("#panel ol.history") ? document.querySelector("#panel ol.history").textContent : "")`, 10), await text("#panel ol.history"));
    check("its state is one row, with Reported pressed", (await js(`[...document.querySelectorAll("#panel .states button")].map(b => b.textContent + (b.getAttribute("aria-pressed") === "true" ? "*" : "")).join(" ")`)) === "Reported* Planned Done Dismissed",
      await text("#panel .states"));
    await clickOn(`[...document.querySelectorAll("#panel .states button")].find(b => b.textContent === "Done")`);
    check("a click on Done: it is done, and no longer open", await until(`entries.some(e => e.id === entryOpen && e.state === "done") && document.querySelector("#panel .states button[aria-pressed=true]").textContent === "Done"`, 10), await text("#panel .states"));
    check("and Undo offers to take that back", await until(`!document.getElementById("undo").hidden && /Undo: what was said of an issue/.test(document.getElementById("undo").textContent)`, 10), await text("#undo"));
    await clickOn(`document.querySelector("#undo button.link")`);
    check("Undo takes it back: it is open again", await until(`entries.some(e => e.id === entryOpen && e.state === "reported")`, 10));
    // Along part of the route instead, from one bend to another: two clicks on the route's line.
    await until(`Boolean(entryShown && entryShown.told && entryShown.told.history.length >= 3)`, 10);       // the side drawn for the last time
    await js(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Change it…").click()`);
    await until(`Boolean(document.querySelector("form.entry-form input[value=part]"))`, 10);
    await js(`document.querySelector("form.entry-form input[value=part]").click()`);
    check("choosing part of the route asks where it begins", await until(`Boolean(entryAsk && entryAsk.picking) && !document.getElementById("hint").hidden`, 10), await text("#hint"));
    const ends = await js(`(() => { const c = window.trialLine, m = Math.floor(c.length / 2), a = c[Math.max(0, m - 4)], b = c[Math.min(c.length - 1, m + 4)]; return JSON.stringify([a, b]); })()`);
    for (const [i, end] of JSON.parse(ends).entries()) {
      await click(await onMap(JSON.stringify(end)));
      if (!i) await until(`Boolean(entryAsk && entryAsk.picking && entryAsk.picking.from)`, 10);
    }
    check("two clicks make a part, measured from the route's start and drawn", await until(`entryAsk && entryAsk.draft.parts.length === 1 && /km, \\d+ m/.test(document.querySelector("form.entry-form .parts").textContent)
      && map.queryRenderedFeatures({ layers: ["entry-pick"] }).length > 0`, 10), await text("form.entry-form .parts"));
    await js(`document.querySelector("form.entry-form").requestSubmit()`);
    check("saved, the issue is along that part", await until(`!entryAsk && entries.some(e => e.id === entryOpen && e.about === "part" && e.parts.length === 1)
      && /Along.* km \\(/.test(document.querySelector("#panel dl.facts").textContent) && map.queryRenderedFeatures({ layers: ["entry-lines"] }).some(f => f.properties.chosen)`, 10), await text("#panel dl.facts"));
    // Changed into a notice for the whole route: the route says it is closed, and so does the area's list.
    await clickOn(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Change it…")`);
    await js(`(() => { const f = document.querySelector("form.entry-form"); f.querySelector("select").value = "closed"; f.querySelector("select").dispatchEvent(new Event("change"));
      f.querySelector("input[value=route]").click(); })()`);
    await until(`Boolean(entryAsk && entryAsk.draft.about === "route" && document.querySelector("form.entry-form"))`, 10);
    await js(`document.querySelector("form.entry-form").requestSubmit()`);
    check("changed into a notice for the whole route, it is in force", await until(`!entryAsk && Boolean(document.querySelector("#panel dl.facts")) && entries.some(e => e.id === entryOpen && e.kind === "closed" && e.about === "route" && e.active)`, 10), await text("#panel"));
    const closed = await js(`entries.find(e => e.id === entryOpen).routes[0].code`);
    await clickOn(`[...document.querySelectorAll("#panel dl.facts button.link")].find(b => b.textContent === ${JSON.stringify(closed)})`);
    check("the route it closes says so at the top", await until(`route === ${JSON.stringify(closed)} && /Closed until further notice/.test(document.querySelector("#panel .notice-box") ? document.querySelector("#panel .notice-box").textContent : "")`, 10), await text("#panel .notice-box"));
    check("the route opens among the issues, its issues first, and its back button goes back to the issue", await js(`view === "issues" && document.querySelector("#panel h2.next").textContent === "Issues"
      && document.querySelector("#panel .backbar .btn").textContent === "‹ The issue"`), await text("#panel .backbar"));
    const theIssue = await js(`routeFrom.entry`);
    await clickOn(`document.querySelector("#panel .backbar .btn")`);
    check("and back, it is the issue again", await until(`view === "issues" && route === null && entryOpen === ${JSON.stringify(theIssue)}`, 10));
    await js(`visit({ view: "routes", route: null, entry: null })`);
    check("and the area's list of routes marks it closed", await until(`[...document.querySelectorAll("#panel li.hit")].some(li => li.querySelector(".code").textContent === ${JSON.stringify(closed)} && /Closed/.test(li.textContent))`, 10));
    await js(`(() => { const e = entries.find(x => x.text === ${JSON.stringify(trial)}); openEntry(e.id); })()`);
    await until(`view === "issues" && Boolean(entryOpen)`, 10);
    await clickOn(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Take it away")`);
    check("taken away, the trial is gone", await until(`entryOpen === null && !entries.some(e => e.text === ${JSON.stringify(trial)})`, 10), await text("#panel"));
    editing = false;
  } else if (EDIT) console.log("    (no stretch here is walked by one route only: reporting an issue is not tried)");
  else console.log("    (not asked to change anything: reporting an issue is not tried)");
  await toPart("lens-routes");
  await until(`view === "routes"`, 10);
} else check("the issues tab is not there for one who does not have the feature", await js(`document.getElementById("tab-issues").hidden`));

// Photos (the feature `photos`): one made in the page, with nothing from a camera, waits to be placed; a click places it.
await js(`(() => { if (typeof photosShown !== "undefined") { photosShown = true; drawPhotos(); } })()`);
if (await js(`hasPhotos()`)) {
  if (EDIT) {
    editing = true;
    await toPart("tab-photos");
    check("the photos part offers to add photos, by dropping them, choosing them or a folder", await until(`view === "photos" && /Photos in/.test(document.querySelector("#panel h2").textContent)
      && Boolean(document.querySelector("#panel .photo-drop")) && /Choose a folder/.test(document.querySelector("#panel .photo-drop").textContent)`, 10), await text("#panel"));
    await js(`(async () => { const c = document.createElement("canvas"); c.width = 640; c.height = 480; const g = c.getContext("2d"); g.fillStyle = "#4a7"; g.fillRect(0, 0, 640, 480);
      g.fillStyle = "#fff"; g.fillText(String(Date.now()), 20, 40);
      const blob = await new Promise(r => c.toBlob(r, "image/jpeg", 0.9)); await addPhotos([new File([blob], "trial.jpg", { type: "image/jpeg" })], { area }); })()`);
    check("a photo with no position is added, it says so, and the photo waits to be placed", await until(`(photosAll || []).some(p => p.lon === null)
      && /1 photo added; 1 with no position from the camera/.test(document.getElementById("panel").textContent) && /1 photo waits to be placed/.test(document.getElementById("panel").textContent)`, 15), await text("#panel"));
    await js(`document.querySelector("#panel .photo-row .photo-thumb").click()`);
    check("it is shown large, with a way to place it", await until(`document.getElementById("photo").open && document.querySelector("#photo img.photo-large").complete && document.querySelector("#photo img.photo-large").naturalWidth === 640`, 10), await text("#photo"));
    await js(`[...document.querySelectorAll("#photo button")].find(b => b.textContent === "Place it on the map").click()`);
    await js(`map.jumpTo({ center: map.getCenter().toArray(), zoom: 14 })`);
    await sleep(800);
    await click(await onMap(`map.getCenter().toArray()`));
    check("placed by a click, it is a point on the map", await until(`(photosAll || []).some(p => p.placed === "hand") && map.queryRenderedFeatures({ layers: ["photos"] }).length > 0`, 15));
    const shot = await js(`photosAll.find(p => p.placed === "hand").id`);
    await js(`openPhoto(${JSON.stringify(shot)})`);
    await until(`document.getElementById("photo").open`, 5);
    await js(`[...document.querySelectorAll("#photo button")].find(b => b.textContent === "Take it away").click()`);
    check("taken away, the trial photo is gone", await until(`!(photosAll || []).some(p => p.id === ${JSON.stringify(shot)})`, 10));
    await js(`document.getElementById("photo").open && document.getElementById("photo").close()`);
    // Three taken at one spot are one circle with their number; clicked, it comes nearer, and then shows the three together.
    const trio = await js(`(async () => { const c = map.getCenter().toArray(), files = [];
      for (const i of [1, 2, 3]) { const k = document.createElement("canvas"); k.width = 320; k.height = 240; const g = k.getContext("2d"); g.fillStyle = "#" + (i * 3) + "a7"; g.fillRect(0, 0, 320, 240);
        g.fillStyle = "#fff"; g.fillText(i + " " + Date.now(), 20, 40); files.push(new File([await new Promise(r => k.toBlob(r, "image/jpeg", 0.9))], "t" + i + ".jpg", { type: "image/jpeg" })); }
      await addPhotos(files, { area, lon: c[0], lat: c[1] }); map.jumpTo({ center: c, zoom: 12 }); return JSON.stringify(c); })()`);
    check("photos at one spot are one circle with their number", await until(`map.queryRenderedFeatures({ layers: ["photo-groups"] }).some(f => f.properties.point_count === 3)`, 15));
    for (let i = 0; i < 8 && !(await js(`document.getElementById("photo").open`)); i++) { await click(await onMap(trio)); await sleep(1200); }
    check("clicked, the circle comes nearer and then shows the three in the box", await js(`document.getElementById("photo").open && photoList.length === 3`), await text("#photo .photo-about"));
    await js(`(async () => { document.getElementById("photo").close(); for (const p of photoList) await send("DELETE", "photos/" + p.id); await loadPhotos(); })()`);
    editing = false;
  } else {
    check("the photos of the area are on the map", await until(`photosFor === area`, 15));
    await toPart("tab-photos");
    check("the photos part shows the area's photos, the newest first", await until(`view === "photos" && document.querySelectorAll("#panel .photo-thumb").length === Math.min(photosAll.length, photoMore)`, 15), await text("#panel h2"));
  }
  // A button among the map's own hides them, and shows them again; the choice is remembered.
  const pressed = await js(`photoSwitch.getAttribute("aria-pressed")`);
  await clickOn(`document.querySelector("button.photo-switch")`);
  check("the photo button hides the photos on the map, and says so", await until(`map.getLayoutProperty("photos", "visibility") === "none" && photoSwitch.getAttribute("aria-pressed") === "false" && localStorage.getItem("stiflyt2.photos") === "off"`, 5));
  await clickOn(`document.querySelector("button.photo-switch")`);
  check("and shows them again", await until(`map.getLayoutProperty("photos", "visibility") === "visible" && photoSwitch.getAttribute("aria-pressed") === "true"`, 5), pressed);
  await toPart("lens-routes");
  await until(`view === "routes"`, 10);
  // Among the signs too, to see which side of the path a post stands on; a click on a site still opens the site.
  await clickOn(`document.getElementById("tab-signs")`);
  check("the photos are on the map among the signs too, with the button that hides them", await until(`view === "signs" && photosFor === area && !photoControl.box.hidden
    && map.getLayoutProperty("photos", "visibility") === (photosShown ? "visible" : "none")`, 15));
  const siteAt = await onMap(`(() => { const s = found && found.sites.find(s => s.lon !== null && s.lon !== undefined); return s ? [s.lon, s.lat] : null; })()`);
  if (siteAt) {
    await js(`(() => { const s = found.sites.find(s => s.lon !== null && s.lon !== undefined); map.jumpTo({ center: [s.lon, s.lat], zoom: 16 }); })()`);
    await until(`!map.isMoving() && map.loaded()`, 10);
    const at = await onMap(`(() => { const s = found.sites.find(s => s.lon !== null && s.lon !== undefined); return [s.lon, s.lat]; })()`);
    await click(at);
    check("a click on a sign site among the photos opens the site", await until(`view === "signs" && Boolean(open) && !photoBox.open`, 10), await text("#panel h2"));
    await js(`visit({ site: null })`);
  }
  await toPart("lens-routes");
  await until(`view === "routes"`, 10);
} else check("no photos are shown to one who does not have the feature", await js(`photosAll === null && map.getLayoutProperty("photos", "visibility") === "none"`));

// Reports (the feature `reports`): how long since each route was inspected, as a list and on the map; a report made in the form.
if (await js(`hasReports()`)) {
  await toPart("tab-reports");
  check("the reports part lists the routes, those longest since first, with what the colours mean, and others' routes apart", await until(`view === "reports" && Array.isArray(upkeep)
    && document.querySelectorAll("#panel li.hit .age-dot").length === upkeep.filter(r => r.ours !== false).length && /due/.test(document.querySelector("#panel .since-legend").textContent)`, 20), await text("#panel h2"));
  if (await js(`hasPhotos()`)) check("the photos are on the map among the reports too, with the button that hides them", await until(`photosFor === area && !photoControl.box.hidden
    && map.getLayoutProperty("photos", "visibility") === (photosShown ? "visible" : "none")`, 10));
  // A route opens among the reports: the map keeps their colours, what they say of it comes first, and back is back to the list.
  const listed = JSON.parse(await js(`JSON.stringify(upkeepListed().rows.map(r => r.code))`));
  if (listed.length) {
    await clickOn(`document.querySelector("#panel li.hit")`);
    check("a route clicked in the reports list opens among the reports, its upkeep before its description", await until(`view === "reports" && route === ${JSON.stringify(listed[0])}
      && document.getElementById("tab-reports").getAttribute("aria-current") === "page" && /view=reports/.test(location.search) && /route=/.test(location.search)
      && (() => { const h = [...document.querySelectorAll("#panel h2")].map(x => x.textContent); return h.includes("Upkeep") && h.indexOf("Upkeep") < h.indexOf("Description"); })()`, 15), await text("#panel"));
    check("the map keeps the colours of the reports, and the open route is an outline round them", await js(`map.getPaintProperty("routes", "line-color") === AGE_COLOURS.never
      && JSON.stringify(map.getPaintProperty("route-open", "line-gap-width")) === JSON.stringify(SINCE_WIDTH)`));
    check("its back button says it goes back to the reports", /^‹ Reports in /i.test(await text("#panel .backbar .btn")), await text("#panel .backbar .btn"));
    if (listed.length > 1) {
      await clickOn(`document.querySelector("#panel .backbar .step[aria-label='The next route in the list']")`);
      check("› is the next route in the reports list", await until(`view === "reports" && route === ${JSON.stringify(listed[1])}`, 10), await js("route"));
    }
    await clickOn(`document.querySelector("#panel .backbar .btn")`);
    check("back, the reports list is there again", await until(`view === "reports" && route === null && Boolean(document.querySelector("#panel .since-legend"))`, 10), await text("#panel h2"));
    const ours = await js(`(() => { const mine = new Set(upkeepListed().rows.map(r => r.id)); const f = lines.features.filter(f => { const ids = f.properties.ids.split("|").filter(Boolean); return ids.length === 1 && mine.has(ids[0]); })
      .sort((a, b) => b.geometry.coordinates.length - a.geometry.coordinates.length)[0]; if (!f) return null; const c = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)]; map.jumpTo({ center: c, zoom: 13 }); return JSON.stringify(c); })()`);
    if (ours) {
      const shown = await js(`typeof photosShown !== "undefined" && photosShown`);
      await js(`(() => { if (typeof photosShown !== "undefined") { photosShown = false; drawPhotos(); } })()`);       // a photo there would take the click
      await sleep(1500);
      await click(await onMap(ours));
      check("a click on a route on the map opens it among the reports", await until(`view === "reports" && Boolean(route)`, 10), `${await js("view")} ${await js("route")}`);
      await js(`visit({ route: null }); if (typeof photosShown !== "undefined") { photosShown = ${shown}; drawPhotos(); }`);
    } else console.log("    (no stretch here is walked by one of our routes only: the click on the map among the reports is not checked)");
  }
  if (EDIT) {
    editing = true;
    const target = await js(`upkeep[0].code`);
    await js(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Report").click()`);
    await until(`Boolean(reportAsk) && Boolean(document.querySelector("form.entry-form select"))`, 10);
    await js(`(() => { const s = [...document.querySelectorAll("form.entry-form select")].find(x => x.getAttribute("aria-label") === "Add a route"); s.value = upkeep[0].id; s.dispatchEvent(new Event("change")); })()`);
    await until(`reportAsk.covers.length === 1`, 5);
    check("the form shows the days photos were taken near the route, or says there are none", await until(`Boolean(photoDays) && /photos/i.test(document.querySelector("form.entry-form").textContent)`, 10), await text("form.entry-form .photo-days"));
    await js(`document.querySelector("form.entry-form input[name=condition]").click()`);
    await js(`(() => { const t = document.querySelector("form.entry-form textarea"); t.value = "Trial inspection"; t.dispatchEvent(new Event("input")); document.querySelector("form.entry-form").requestSubmit(); })()`);
    check("a report is made in the form, and opens", await until(`Boolean(reportOpen) && /Inspection/.test(document.querySelector("#panel h2").textContent) && /Trial inspection/.test(document.getElementById("panel").textContent)`, 15), await text("#panel"));
    check("the route it covered is no longer never inspected, and is green on the map", await until(`upkeep.find(r => r.code === ${JSON.stringify(target)}).inspection !== null
      && map.getSource("since") && sinceLines.length > 0`, 10));
    await js(`showRoute(${JSON.stringify(target)})`);
    check("the route says when it was last inspected", await until(`/Last inspected: \\d/.test(document.getElementById("panel").textContent)`, 15), await text("#panel"));
    await js(`openReport(reportsAll.find(r => r.text === "Trial inspection").id)`);
    await until(`Boolean(reportOpen) && Boolean([...document.querySelectorAll("#panel button")].find(b => b.textContent === "Take it away"))`, 10);
    await js(`[...document.querySelectorAll("#panel button")].find(b => b.textContent === "Take it away").click()`);
    check("taken away, the trial report is gone", await until(`!reportOpen && !(reportsAll || []).some(r => r.text === "Trial inspection")`, 10));
    editing = false;
  }
  await toPart("lens-routes");
  await until(`view === "routes"`, 10);
} else check("the reports part is not there for one who does not have the feature", await js(`document.getElementById("tab-reports").hidden`));

// Who owns the ground (the feature `landowners`): a route's properties in order, and the property at a click on the map.
// Who owns them is asked of the register only on the test viewer, which has no account for it: never of the real one.
if (await js(`hasLandowners()`)) {
  await toPart("tab-landowners");
  check("the owners tab lists the area's routes to open", await until(`view === "landowners" && document.querySelectorAll("#panel li.hit").length === over.routes.length
    && /Landowners in/.test(document.querySelector("#panel h2").textContent)`, 15), await text("#panel h2"));
  await clickOn(`document.querySelector("#panel li.hit")`);
  check("a route opens among the owners, with the properties it crosses first", await until(`view === "landowners" && Boolean(route)
    && document.querySelector("#panel h2.next").textContent === "Landowners" && ownedAlong && Boolean(ownedAlong.found)
    && /along its .* km, in the order it crosses them/.test(document.getElementById("panel").textContent)`, 20), await text("#panel"));
  if (await js(`ownedAlong.found.properties.length > 0`)) {
    await mouse("mouseMoved", ...Object.values(await centre(`document.querySelector("#panel .owned-along li.hit")`)));
    check("pointing at a property lights up where the route crosses it", await until(`map.getSource("owned").serialize().data.features.some(f => f.geometry.type === "LineString")`, 5));
  } else console.log("    (the route crosses no parcel here: pointing at one is not tried)");
  await clickOn(`document.querySelector("#panel .backbar .btn")`);
  await until(`view === "landowners" && route === null`, 10);
  // A click off the routes: the property there, or that none is known there.
  const off = await js(`(() => { const c = map.getCenter(); for (let dx = 0; dx < 400; dx += 40) for (const sx of [1, -1]) {
      const p = map.project(c); p.x += sx * dx; p.y += 60; if (!routesAt(p).length) { const r = document.getElementById("map").getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + p.x), y: Math.round(r.top + p.y) }); } } return "null"; })()`);
  await click(JSON.parse(off));
  check("a click off the routes asks what property lies there, and says it in a box on the map", await until(`Boolean(ownedAt) && (Boolean(ownedAt.found) || Boolean(ownedAt.problem))
    && Boolean(document.querySelector(".owned-box")) && !/Looking it up/.test(document.querySelector(".owned-box").textContent)`, 15), await text(".owned-box"));
  // A second click while the box is open: the box moves there and says what is there, not "Looking it up…" for ever.
  const first = await js(`ownedAt.lon`), again = JSON.parse(off);
  await click({ x: again.x + 25, y: again.y + 25 });
  check("a second click moves the box and says what lies there", await until(`Boolean(ownedAt) && ownedAt.lon !== ${first} && (Boolean(ownedAt.found) || Boolean(ownedAt.problem))
    && Boolean(document.querySelector(".owned-box")) && !/Looking it up/.test(document.querySelector(".owned-box").textContent)`, 15), await text(".owned-box"));
  if (!TOKEN && await js(`Boolean(ownedAt.found && ownedAt.found.units.some(u => u.id))`)) {
    await clickOn(`[...document.querySelectorAll(".owned-box button")].find(b => b.textContent === "Who owns it")`);
    check("with no account for the register, asking who owns it says so", await until(`/no account for Kartverket's property register/.test(document.querySelector(".owned-box").textContent)`, 15), await text(".owned-box"));
  }
  await js(`ownedPopup.remove()`);
  await toPart("lens-routes");
  await until(`view === "routes"`, 10);
} else check("the owners tab is not there for one who does not have the feature", await js(`document.getElementById("tab-landowners").hidden`));

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
if (await js(`get("auth/me").then(m => (m.features || []).includes("presence"))`))
  check("the editor too shows who else is at work, at its foot",
    await others({ name: "Another Editor", page: "editor", area: "bre", route: "bre18", site: null, unsaved: 0 }, "Also here now: Another Editor (route editor, bre, bre18)"), await text("#present"));
if (await js(`Boolean(openRoute ? openRoute.height.known_m : document.querySelector("#selected .profile"))`))
  check("and draws its height profile with the same code", await until(`Boolean(document.querySelector("#selected .profile svg"))`, 25));

check("the way back to the front page is at the top of the side", await js(`(() => { const a = document.querySelector("header a.home"); return Boolean(a) && new URL(a.href).pathname === "/" && a.checkVisibility() && a.getBoundingClientRect().top < 60; })()`), await text("header h1"));
check("the foot of the side is one line until it is asked for", await js(`!document.getElementById("foot").open && /Signed in as|Changes are recorded as|Read-only/.test(document.getElementById("footwho").textContent) && !document.getElementById("editing").checkVisibility()`), await text("#foot summary"));
if (await js("Boolean(EDITOR)")) check("the tools of a route are at the top of its panel", await js(`(() => { const b = [...document.querySelectorAll("#selected button")].find(x => x.textContent === "Fix the line itself"); return Boolean(b) && b.getBoundingClientRect().top < 430; })()`), await js(`String(([...document.querySelectorAll("#selected button")].find(x => x.textContent === "Fix the line itself") || { getBoundingClientRect: () => ({ top: "not there" }) }).getBoundingClientRect().top)`));
check("the editor has read the lines of your routes by itself, when the page opened", await until(`ownedLines.features.length > 0`, 30));

// A stretch of the open route: its popup says how long it is, and offers to divide it without fixing the line.
const own = await js(`(() => { const f = ownedLines.features.filter(f => f.properties.routes.some(r => r.id === openRoute.id)).sort((a, b) => b.geometry.coordinates.length - a.geometry.coordinates.length)[0];
  if (!f) return null; const at = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)]; map.jumpTo({ center: at, zoom: 14 }); return JSON.stringify(at); })()`);
if (own) {
  await until(`map.loaded() && !map.isMoving()`, 15); await sleep(900);
  await click(await onMap(own));
  check("a stretch's popup says how long it is" + (await js("Boolean(EDITOR)") ? ", and offers to divide it there" : ""), await until(`/Stretch of .*\\d (m|km)/.test((document.querySelector(".maplibregl-popup") || {}).textContent || "") && (!EDITOR || [...document.querySelectorAll(".maplibregl-popup button")].some(b => b.textContent === "Divide it here"))`, 10), await text(".maplibregl-popup"));
  await js("closePopups()");
}
// Choosing an area shows it on the map.
// Both an area with routes of yours and one with none yet, where only the spots its routes end at say where it is.
const yoursIn = a => `ownedLines.features.some(f => f.properties.codes.split(", ").some(c => c.toLowerCase().startsWith(${JSON.stringify(a)})))`;
const areas = JSON.parse(await js(`JSON.stringify([...areaSelect.options].map(x => x.value).filter(Boolean))`)), kinds = [];
for (const a of areas) kinds.push([a, await js(yoursIn(a.toLowerCase()))]);
for (const [what, pick] of [["with routes of yours", kinds.find(k => k[1])], ["with none of yours yet", kinds.find(k => !k[1])]]) {
  if (!pick) { console.log(`    (no area ${what}: choosing one is not tried)`); continue; }
  await js(`(() => { map.jumpTo({ center: [18.9, 69.6], zoom: 9 }); areaSelect.value = ${JSON.stringify(pick[0])}; areaSelect.dispatchEvent(new Event("change")); })()`);
  check(`choosing an area ${what} moves the map to it (${pick[0]})`, await until(`map.getCenter().lat < 66 && !map.isMoving()`, 40), await js("JSON.stringify(map.getCenter().toArray().map(v => Number(v.toFixed(2))))"));
}

// The list of an area follows the choice of whose routes: with a maintainer chosen, only theirs, the rest a click away.
const whoIs = await js(`(async () => { const all = [...whoSelect.options].map(o => o.value).filter(Boolean); if (!all.length || !areaSelect.value) return null;
  await chooseWho(""); await loadArea(true); const every = document.querySelectorAll("#routelist li.hit").length;
  for (const w of all) { await chooseWho(w); await loadArea(true); const n = document.querySelectorAll("#routelist li.hit").length; if (n && n < every) return JSON.stringify({ w, n, every }); }
  return JSON.stringify({ w: all[0], n: document.querySelectorAll("#routelist li.hit").length, every }); })()`);
if (whoIs) {
  const { w, n, every } = JSON.parse(whoIs), more = `[...document.querySelectorAll("#arearoutes button.link")].find(b => /List them too|Leave them out/.test(b.textContent))`;
  if (n < every) {
    check(`with a maintainer chosen, the list of the area has only their routes (${n} of ${every})`, await js(`/not maintained by .*not listed/.test(document.getElementById("arearoutes").textContent) && Boolean(${more})`), w);
    const held = await js("openRoute && openRoute.id");        // the list is put aside while a route is open: it is closed for the click
    await js("clearRoute()"); await sleep(300);
    await clickOn(more);
    check("and the others are listed too when asked for", await until(`document.querySelectorAll("#routelist li.hit").length === ${every} && (${more} || {}).textContent === "Leave them out"`, 30), await js(`document.querySelectorAll("#routelist li.hit").length + " listed, of " + ${every} + "; the button says: " + ((${more} || {}).textContent || "nothing") + "; others too: " + othersToo`));
    if (held) { await js(`openOwnedRoute(${JSON.stringify(held)})`); await until(`Boolean(openRoute) && openRoute.id === ${JSON.stringify(held)}`, 20); }
  } else console.log(`    (every route of the area is maintained by each who can be chosen: the narrowing of the list is not tried)`);
  await js(`chooseWho(${JSON.stringify(await js("localStorage.getItem(WHOSE) || ''"))})`);
}

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
  const fullReads = asked.filter(u => u.endsWith("/owned/routes.geojson")).length, markReads = asked.filter(u => u.endsWith("/owned/routes.marks")).length;
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
    // (Stretches that only went out of use are dropped by their marks alone: then nothing more is read.)
    check("after the change the marks were read, and not every line anew", asked.filter(u => u.endsWith("/owned/routes.marks")).length > markReads && asked.filter(u => u.endsWith("/owned/routes.geojson")).length === fullReads, `${asked.filter(u => u.endsWith("/owned/routes.geojson")).length} readings of all lines, ${fullReads} before`);
    await until(`/end of .* moved/.test(document.getElementById("undo").textContent)`, 10);
    await clickOn(`document.getElementById("undo")`);
    check("and Undo puts the end back", await until(`Boolean(openRoute) && openRoute.length.legs === ${legs}`, 20), await text("#undobar"));
    // The line itself: held still, a pulled line says what it would become. Escape lets go.
    await js("setFixing(true)"); await sleep(500);
    const pull = await js(`(() => { const f = ownedLines.features.filter(f => f.properties.routes.some(r => r.id === openRoute.id) && !FREE.includes(f.properties.kind)).sort((a, b) => b.geometry.coordinates.length - a.geometry.coordinates.length)[0];
      if (!f) return null; const at = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)]; map.jumpTo({ center: at, zoom: 15 }); return JSON.stringify(at); })()`);
    if (pull) {
      await until(`map.loaded() && !map.isMoving()`, 15); await sleep(900);
      const g = await onMap(pull);
      await mouse("mouseMoved", g.x, g.y); await sleep(150); await mouse("mousePressed", g.x, g.y, 1);
      for (let i = 1; i <= 8; i++) { await mouse("mouseMoved", g.x + 5 * i, g.y - 4 * i, 1); await sleep(30); }
      check("while the line itself is fixed, a pulled line says what it would become", await until(`/^(Let go: the line (follows|runs straight)|Not here: )/.test(document.getElementById("hint").textContent)`, 15), await text("#hint"));
      await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await mouse("mouseReleased", g.x + 40, g.y - 32); await sleep(300);
    }
    await js("setFixing(false)");
  } else console.log("    (no route of a few stretches in one line was found: dragging an end is not tried)");
  // Two places that are one: a place made where the route ends and one at the junction before it, by the
  // same name. From the panel of the first, the other is taken in; Undo takes that back, and then the two.
  const twice = await js(`(async () => { const taken = new Set(openRoute.ends.filter(e => e.place).map(e => e.id)), free = openRoute.nodes.filter(n => !taken.has(n.id));
    const apart = (p, q) => metresOf([[p.lon, p.lat], [q.lon, q.lat]]);       // the two junctions of the route that lie nearest each other
    const [end, inner] = free.flatMap((p, i) => free.slice(i + 1).map(q => [p, q])).sort((x, y) => apart(...x) - apart(...y))[0] || [];
    if (!end || !inner || apart(end, inner) > 2800) return null;
    const name = "Trial hut " + Date.now() % 100000, a = await send("POST", "places/at-junction", { node_id: end.id, name }), b = await send("POST", "places/at-junction", { node_id: inner.id, name });
    await refreshOwned(); await refreshUndo(); await openPlace(a.id); return JSON.stringify({ a: a.id, b: b.id }); })()`);
  if (twice && !twice.startsWith("EXCEPTION")) {
    const { a, b } = JSON.parse(twice), take = `[...document.querySelectorAll("#place button")].find(x => x.textContent === "They are one place: take it in")`;
    check("the panel of a place lists another of the same name near it", await until(`/Round this place/.test(document.getElementById("place").textContent) && Boolean(${take})`, 20), (await text("#place")).slice(250, 700));
    await clickOn(take);
    check("taken in, the two are one place with both arrival points", await until(`/Access points \\(2\\)/.test(document.getElementById("place").textContent) && !${take}`, 20), (await text("#place")).slice(0, 160));
    check("and the other is no longer a place", await js(`get("places/${b}").then(() => false, () => true)`));
    check("Undo offers to take that back", await until(`/Undo\\s*two places made one$/.test(document.getElementById("undo").textContent)`, 10), await text("#undo"));
    for (let i = 0; i < 3 && await js(`/made a place|two places made one/.test(document.getElementById("undo").textContent)`); i++) { await clickOn(`document.getElementById("undo")`); await until(`!document.getElementById("undo").disabled`, 10); await sleep(500); }
    check("and with the two places taken back as well, nothing of the trial is left", await js(`Promise.all([get("places/${a}").then(() => false, () => true), get("places/${b}").then(() => false, () => true)]).then(gone => gone.every(Boolean))`));
  } else console.log(`    (no two junctions without a place within 3 km of each other on the open route: making two places one is not tried) ${twice || ""}`);
  // Save or discard: what has been changed since the last save is counted, kept or taken back as a whole.
  const stays = `Stays ${Date.now() % 100000}`, goes = `Goes ${Date.now() % 100000}`, here = await js("openRoute.id"), found = await js("openRoute.name");
  const rename = name => js(`(async () => { await send("PATCH", "owned/routes/${here}", { version: openRoute.version, name: ${JSON.stringify(name)} }); await reloadOpen(); return true; })()`);
  const press = label => clickOn(`[...document.querySelectorAll("#unsaved button")].find(b => b.textContent === ${JSON.stringify(label)})`);
  await js(`saveAll()`); await until(`document.getElementById("unsaved").hidden`, 10);
  const code = await js("openRoute.code");
  await rename(stays);
  check("a change is seen to take: it is counted as not saved, and to which route", await until(`document.getElementById("unsaved").textContent.startsWith("1 change not saved, to " + ${JSON.stringify(code)}) && !document.getElementById("unsaved").hidden`, 15), await text("#unsaved"));
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
    const asks = `[...document.querySelectorAll("#leave button")]`;
    check("with a change not saved, it asks first what is to become of it", await until(`document.getElementById("leave").open && /1 change not saved, to /.test(document.getElementById("leave").textContent) && ${asks}.length === 4`, 15), await text("#leave"));
    check("and the route is still the one that was open", await js(isOpen(b)));
    await clickOn(`${asks}.find(x => x.textContent === "Same job: keep going")`);
    check("Same job, keep going: the other route is open, and the change is still not saved", await until(`${isOpen(a)} && unsaved.changes.length === 1 && !document.getElementById("leave").open`, 20), await text("#unsaved"));
    // A change to the second route is part of the same item of work: one count, both routes named.
    const wasA = await js("openRoute.name");
    await js(`(async () => { await send("PATCH", "owned/routes/${a.id}", { version: openRoute.version, name: ${JSON.stringify(trial)} }); await reloadOpen(); return true; })()`);
    check("what is changed on the next route belongs to the same item of work", await until(`unsaved.changes.length === 2 && unsaved.routes.length === 2 && /^2 changes not saved, to .+ and .+/.test(document.getElementById("unsaved").textContent)`, 15), await text("#unsaved"));
    check("and it is kept on the server, not in the browser", await js(`get("work").then(w => w.changes.length === 2 && w.routes.length === 2)`));
    await clickOn(`[...document.querySelectorAll("#selected .backbar button")].find(x => x.textContent.includes("All routes"))`);
    check("back to the list asks the same", await until(`document.getElementById("leave").open`, 10), await text("#leave"));
    await clickOn(`${asks}.find(x => x.textContent.startsWith("Stay on "))`);
    check("Stay: the route is open as it was, with the changes", await until(`!document.getElementById("leave").open`, 5) && await js(`${isOpen(a)} && unsaved.changes.length === 2`));
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 }); await sleep(300);
    check("Escape does not close the route", await js(isOpen(a)));
    await clickOn(`[...document.querySelectorAll("#unsaved button")].find(x => x.textContent === "Discard")`);
    check("Discard takes back the whole item: both routes are as they were", await until(`unsaved.changes.length === 0`, 20) && await js(`Promise.all([get("owned/routes/${a.id}"), get("owned/routes/${b.id}")]).then(([x, y]) => x.name === ${JSON.stringify(wasA)} && y.name === ${JSON.stringify(was)})`));
    check("the panel has carrying on along another line as a tool of its own", await js(`[...document.querySelectorAll("#selected button")].some(x => x.textContent === "Carry it on along another line…")`));
  } else console.log("    (no two routes with a stretch of their own each: going from one to the other is not tried)");
  // The signs: what is decided at a sign site is an item of work too. A sign is put on the order with real
  // clicks; leaving the site asks; Discard takes it back; and saved, leaving asks nothing.
  const among = await js(`localStorage.getItem("stiflyt2.area")`);
  await open(`?area=${among}&view=signs`);
  await until(`typeof view !== "undefined" && view === "signs" && Boolean(found) && me.operates && document.querySelectorAll("#panel li.hit").length > 0`, 40);
  await clickOn(`document.querySelector("#panel li.hit")`);
  const plate = `document.querySelector("#panel .plate:not([disabled])")`, onOrder = () => js(`found.sites.flatMap(s => s.posts.flatMap(q => q.signs)).filter(g => g.to_order).length`);
  if (await until(`Boolean(open) && Boolean(${plate}) && pendingRead`, 20)) {
    const before = await onOrder(), add = async () => { await clickOn(plate); await until(`chosen.size === 1`, 5); await clickOn(`[...document.querySelectorAll("#chosen button")].find(x => x.textContent.startsWith("Add to the order"))`); };
    await add();
    check("a decision about a sign is counted as not saved", await until(`/^1 change not saved/.test((document.querySelector("#undo .unsaved") || {}).textContent || "")`, 20) && await onOrder() === before + 1, await text("#undo"));
    await clickOn(`document.querySelector("#panel .backbar button")`);
    check("leaving the sign site asks first what is to become of it", await until(`document.getElementById("leave").open && document.querySelectorAll("#leave button").length === 4`, 10), await text("#leave"));
    await clickOn(`[...document.querySelectorAll("#leave button")].find(x => x.textContent.startsWith("Stay at "))`);
    check("Stay: the site is open as it was", await js(`Boolean(open) && !document.getElementById("leave").open && pending.changes.length === 1`));
    await clickOn(`[...document.querySelectorAll("#undo .unsaved button")].find(x => x.textContent === "Discard")`);
    check("Discard takes it back: the sign is off the order, and nothing waits", await until(`pending.changes.length === 0 && !document.querySelector("#undo .unsaved")`, 20) && await onOrder() === before && await js(`get("signs/work").then(w => w.changes.length === 0)`), await text("#undo"));
    await add();
    await until(`pending.changes.length === 1`, 20);
    await clickOn(`[...document.querySelectorAll("#undo .unsaved button")].find(x => x.textContent === "Save")`);
    check("Save says it stays", await until(`pending.changes.length === 0 && !document.querySelector("#undo .unsaved")`, 20) && await onOrder() === before + 1 && await js(`get("signs/work").then(w => w.changes.length === 0)`));
    await clickOn(`document.querySelector("#panel .backbar button")`);
    check("and saved, the site is left without a question", await until(`!open && !document.getElementById("leave").open`, 10));
    await clickOn(`document.querySelector("#undo button.link")`);
    check("Undo still takes a saved decision back: the trial leaves the order as it was", await until(`Boolean(found)`, 5) && await (async () => { for (let i = 0; i < 40; i++) { if (await onOrder() === before) return true; await sleep(250); } return false; })());
  } else console.log("    (no sign that can be chosen at the first sign site: the item of work among the signs is not tried)");
  editing = false;
} else console.log("    (not asked to change anything: undo, redo, dragging, places, saving, changing route and signs are not tried)");

check("the pages threw nothing", thrown.length === 0, thrown.join(" | "));
const boxes = said.filter(s => !/^Take .* into |^Take this (issue|photo|report) away/.test(s));       // those ask before two places are made one, or an issue is taken away, and are answered yes
check("and put up no box to say that something went wrong", boxes.length === 0, boxes.join(" | "));
const unasked = refused.filter(r => !r.startsWith("403 /api/v1/signs/undo") && !(EDIT && /^404 \/api\/v1\/places\/[0-9a-f-]{36}$/.test(r))      // that one was asked for as a reader, to be refused
  && !(TOKEN && r === "403 /api/v1/here"));            // and a script is not somebody at work in the pages
check("the service answered every request of its own", unasked.length === 0, unasked.join(" | "));
check("nothing was changed", stopped.length === 0, stopped.join(" | "));
console.log(failed.length ? `\n${failed.length} failed` : "\nall passed");
ws.close(); chrome.kill(); await sleep(300); rmSync(profile, { recursive: true, force: true });
process.exit(failed.length ? 1 : 0);
