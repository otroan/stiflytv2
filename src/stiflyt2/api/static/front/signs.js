// The signs of an area: the list of sites, one site with its posts and signs, the order and its basket.
const kmText = km => km === null ? "" : `${km < 10 ? km.toFixed(1).replace(".", ",") : Math.round(km)} km`;      // as a sign says it
const count = n => `${n} ${n === 1 ? "sign" : "signs"}`;
// What is on a sign beside its words: the T in its ring, the arrow, and for a green sign the walker.
const tMark = () => svg("svg", { width: 24, height: 24, viewBox: "0 0 24 24", "aria-hidden": "true" },
  svg("circle", { cx: 12, cy: 12, r: 10.5, fill: "none", stroke: "#5d6166", "stroke-width": 1.2, "stroke-dasharray": "1.6 1.3" }),
  svg("path", { d: "M6.5 6.5h11v3.6h-3.6V18h-3.8v-7.9H6.5z", fill: css("--t") }));
const arrowMark = (direction, colour) => svg("svg", { width: 13, height: 13, viewBox: "0 0 12 12", "aria-hidden": "true" },
  svg("path", { d: { venstre: "M11 1L1 6l10 5z", "høyre": "M1 1l10 5L1 11z", "rett fram": "M1 11L6 1l5 10z" }[direction] || "", fill: colour }));
const walkerMark = () => svg("svg", { width: 22, height: 22, viewBox: "0 0 22 22", "aria-hidden": "true" },
  svg("rect", { x: 1, y: 1, width: 20, height: 20, rx: 2.5, fill: "#fff" }),
  svg("circle", { cx: 12, cy: 5.3, r: 2, fill: css("--green") }),
  svg("path", { d: "M11.5 8.2l-3 2.6v3.2M11.5 8.2l.6 4.8 2.6 2v3.6M12 13l-2.2 2.4-1.6 3.2M12 9.6l3.4 1.6", fill: "none", stroke: css("--green"), "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round" }));
// A site with no name is told by its routes, and by what it lies near, where something near has a name.
const siteLabel = s => s.name || `${s.free ? "On" : "Junction of"} ${s.routes.join(", ")}${s.near ? `, by ${s.near.name}` : ""}`;

// Every sign there is to choose among: the signs on the posts, and the name signs on top of them.
const every = () => !found ? [] : found.sites.flatMap(s => s.posts.filter(p => !p.taken_out).flatMap(p => [...(p.name_sign ? [{ ...p.name_sign, site: s }] : []), ...p.signs.filter(g => !g.taken_out).map(g => ({ ...g, site: s }))]));
const markedNow = () => every().filter(g => g.to_order);
const onOrder = s => s.posts.reduce((n, p) => n + (p.taken_out ? 0 : (p.name_sign && p.name_sign.to_order ? 1 : 0) + p.signs.filter(g => g.to_order && !g.taken_out).length), 0);
const waiting = s => s.posts.reduce((n, p) => n + (p.taken_out ? 0 : (p.name_sign && p.name_sign.ordered && !p.name_sign.ordered.up ? 1 : 0) + p.signs.filter(g => g.ordered && !g.ordered.up && !g.taken_out).length), 0);
// The sites the list shows: of the state chosen, and with what is typed in their name, code, routes or what they lie by.
function listed() {
  const words = sought.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return found.sites.filter(s => (shown === "all" || (shown === "order" ? onOrder(s) : shown === "waiting" ? waiting(s) : s.no_signs))
    && words.every(w => [siteLabel(s), s.code || "", ...s.routes].join(" ").toLowerCase().includes(w)));
}
// The signs on the order, each with the way to its site and the way off the order.
function orderItems(marked) {
  return el("ul", {}, ...marked.map(g => el("li", { style: "display:flex;gap:8px;align-items:baseline;padding:2px 0" },
    el("span", { class: "grow" }, el("button", { class: "link", onclick: () => openSite(g.site.key) }, siteLabel(g.site)), `: ${g.text || "no name"}${g.km === undefined || g.km === null ? "" : `, ${kmText(g.km)}`}`),
    me.operates ? el("button", { class: "link", title: "Take it off the order", onclick: () => change(() => send("POST", "signs/to-order", { keys: [g.key], to_order: false })) }, "off") : "")));
}
// Taking the order out: it is kept as it is now, and the list for whoever makes the signs is downloaded.
function takeOut() {
  const n = markedNow().length;
  change(async () => {
    const note = prompt(`Take out the order for ${count(n)}? What each sign says now is kept with the order, and the list for the maker is downloaded.\n\nA note for the order, if you like:`, "");
    if (note === null) return;
    const done = await send("POST", "signs/orders", { area, note });
    location.href = new URL(`signs/orders/${done.id}.xlsx`, API).href;
  });
}
const basket = document.getElementById("basket"), basketBox = document.getElementById("basketbox");
function renderBasket() {
  const marked = found ? markedNow() : [];
  document.getElementById("basketcount").textContent = marked.length;
  basket.classList.toggle("none", !marked.length);
  basket.setAttribute("aria-label", `The order: ${count(marked.length)}`);
  if (basketBox.hidden) return;
  basketBox.replaceChildren(el("h2", {}, "The order"),
    marked.length ? el("div", { class: "sub" }, `${count(marked.length)} to be made.`)
      : el("div", { class: "sub" }, "Nothing is on the order. Click the signs that are to be replaced, at one site and the next, and add them to the order."),
    orderItems(marked),
    me.operates && marked.length ? el("div", { class: "actions" }, el("button", { class: "btn main", onclick: takeOut }, "Take out the order and download the list")) : "");
}
basket.addEventListener("click", () => { basketBox.hidden = !basketBox.hidden; basket.setAttribute("aria-expanded", String(!basketBox.hidden)); renderBasket(); });
function point(post, bearing) {
  const next = post === null || bearing === null ? null : { post, bearing };
  if (JSON.stringify(next) === JSON.stringify(hot)) return;
  hot = next;
  for (const plate of document.querySelectorAll("#panel .plate[data-post]")) plate.classList.toggle("hot", Boolean(hot) && plate.dataset.post === hot.post && Number(plate.dataset.bearing) === hot.bearing);
  // Only the lines are drawn anew: the posts stay as they are, so that one being dragged is not let go of.
  for (const ray of drawn) ray.properties.hot = Boolean(hot) && ray.properties.post === hot.post && ray.properties.bearing === hot.bearing;
  if (map.getSource("rays")) map.getSource("rays").setData({ type: "FeatureCollection", features: drawn });
}
function choose(key) { if (chosen.has(key)) chosen.delete(key); else chosen.add(key); render(); }
// The signs clicked so far, in this site and the others: one press puts them all on the order.
function renderChosen() {
  const bar = document.getElementById("chosen"), here = new Set(every().map(g => g.key));
  for (const key of [...chosen]) if (!here.has(key)) chosen.delete(key);
  bar.hidden = !chosen.size;
  if (!chosen.size) return;
  bar.replaceChildren(el("span", { class: "grow" }, `${count(chosen.size)} chosen`),
    el("button", { class: "btn main", onclick: () => { const keys = [...chosen]; chosen.clear(); change(() => send("POST", "signs/to-order", { keys })); } }, "Add to the order"),
    every().some(g => chosen.has(g.key) && g.ordered && !g.ordered.up) ? el("button", { class: "btn", title: "Of the chosen, those that have been ordered and are not up yet", onclick: () => { const keys = [...chosen]; chosen.clear(); change(() => send("POST", "signs/up", { keys })); } }, "They are up") : "",
    el("button", { class: "btn", onclick: () => { chosen.clear(); render(); } }, "Clear"));
}

function renderList() {
  const T = found.totals;
  const chip = (value, label) => el("button", { "aria-pressed": String(shown === value), onclick: () => { shown = value; render(); } }, label);
  const marked = markedNow();
  const sites = listed();
  const orders = el("div", {});
  get("signs/orders", { area }).then(list => { if (list.length) orders.replaceChildren(el("div", { class: "sub", style: "margin-top:10px" }, "Orders taken out. Each is kept as it was."),
    el("ul", {}, ...list.map(o => el("li", {}, el("a", { href: new URL(`signs/orders/${o.id}.xlsx`, API).href }, `Order ${o.id}`),
      el("span", { class: "sub" }, `, ${count(o.signs)}, ${new Date(o.at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}, ${o.by}. `),
      el("a", { href: new URL(`signs/sheet?area=${area}&order=${o.id}`, API).href, target: "_blank", rel: "noopener", title: "Field sheets for the sites this order has signs at, to print" }, "Field sheets"))))); }).catch(() => {});
  panel.replaceChildren(
    el("select", { "aria-label": "Area", onchange: e => visit({ area: e.target.value, site: null }) },
      ...areas.map(a => el("option", { value: a.area, selected: a.area === area }, `${a.area}, ${many(a.routes, "route", "routes")}`))),
    problem ? el("div", { class: "error", role: "alert", style: "margin-top:8px" }, `That was not done: ${problem}`) : "",
    adrift.length ? el("div", { style: "margin-top:8px" }, el("span", { class: "tag warn" }, `${adrift.length} adrift`),
      el("span", { class: "sub" }, ` ${adrift.length === 1 ? "Something was" : "Things were"} decided about a sign that is no longer there. `),
      el("button", { class: "link", onclick: () => { lookingAtAdrift = true; render(); } }, "Look at " + (adrift.length === 1 ? "it" : "them"))) : "",
    el("div", { class: "sub", style: "margin-top:8px" }, `${T.sites} sign sites with ${count(T.signs)}, worked out from the routes as they are now. `
      + "A sign points to the next hut or parking place on its route, or to where the route ends."),
    T.onward ? el("div", { class: "sub", style: "margin-top:4px" }, `${count(T.onward)} point on past a place under ${found.near_km} km off, to where its signs point, up to ${found.onward_km} km in all.`) : "",
    T.unnamed ? el("div", { style: "margin-top:6px" }, el("span", { class: "tag warn" }, `${T.unnamed} without a name`), el("span", { class: "sub" }, " point to the end of a route that is at no place. Name the place under Routes.")) : "",
    el("div", { class: "filter" }, chip("all", `All ${T.sites}`),
      marked.length ? chip("order", `On the order ${marked.length}`) : "", T.waiting ? chip("waiting", `Ordered, not up ${T.waiting}`) : "",
      T.no_signs ? chip("none", `No signs ${T.no_signs}`) : ""),
    el("input", { id: "find", type: "text", value: sought, placeholder: "Find a site: name, code or route", "aria-label": "Find a sign site",
      oninput: e => { sought = e.target.value; const at = e.target.selectionStart; renderList(); const box = document.getElementById("find"); box.focus(); box.setSelectionRange(at, at); } }),
    el("ul", {}, ...sites.map(s => { const n = s.posts.reduce((sum, p) => sum + (p.taken_out ? 0 : p.signs.filter(g => !g.taken_out).length), 0);
      return el("li", { class: "hit", tabindex: 0, onclick: () => openSite(s.key), onkeydown: e => { if (e.key === "Enter") openSite(s.key); } },
        el("span", { class: "grow" }, siteLabel(s), s.code ? el("span", { class: "sub" }, ` ${s.code}`) : ""),
        el("span", { class: "sub" }, count(n)), onOrder(s) ? el("span", { class: "tag warn" }, `${onOrder(s)} to order`) : "",
        waiting(s) ? el("span", { class: "tag" }, `${waiting(s)} not up`) : "",
        s.no_signs ? el("span", { class: "tag" }, "no signs") : ""); })),
    sites.length ? "" : el("div", { class: "sub" }, "None."),
    me.operates ? el("div", { class: "actions" }, el("button", { class: "btn", title: "For a sign that stands where no routes meet: where a path leaves a road, at a bridge. Click a route on the map.", onclick: () => place("site") }, "A sign site at another spot")) : "",
    el("h2", {}, "The order"),
    marked.length ? el("div", {}, `${count(marked.length)} to be made:`) : el("div", { class: "sub" }, "Nothing is on the order. Most signs hang where they are to hang: open a site, click the signs that are to be replaced, and add them to the order."),
    orderItems(marked),
    me.operates && marked.length ? el("div", { class: "actions" }, el("button", { class: "btn main", onclick: takeOut }, `Take out the order for ${count(marked.length)}`)) : "",
        orders);
}

// What was decided about a sign, a post or a sign site that the routes no longer give. Nothing is done about
// it by itself: a person says which sign it is about now, or that it can be forgotten.
function renderAdrift() {
  const can = me.operates, settle = (entry, request) => change(async () => { await request(); adrift = adrift.filter(a => a.id !== entry.id); });
  const what = { sign: "A sign", post: "A post", site: "A sign site" };
  panel.replaceChildren(
    el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => { lookingAtAdrift = false; render(); } }, "‹ All sites")),
    el("h2", {}, "Adrift"),
    problem ? el("div", { class: "error", role: "alert" }, `That was not done: ${problem}`) : "",
    el("div", { class: "sub" }, "What was decided here was about a sign, a post or a sign site that is no longer there, in this area or another. "
      + "The routes have been changed since. Nothing is lost and nothing is done by itself: say which sign it is about now, or that it can be forgotten."),
    ...adrift.map(entry => el("div", { class: "post" },
      el("h3", {}, `${what[entry.kind]}${entry.at ? ` at ${entry.at}` : ""}${entry.kind === "sign" && entry.to ? ` to ${entry.to}` : ""}`),
      el("div", {}, entry.said.join(", "), "."),
      el("div", { class: "sub" }, `Adrift because ${entry.why}.`, entry.by ? ` Last said by ${entry.by}, ${new Date(entry.last_said).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}.` : ""),
      el("div", { class: "actions" },
        entry.lon !== null ? el("button", { class: "btn", onclick: () => map.easeTo({ center: [entry.lon, entry.lat], zoom: Math.max(map.getZoom(), 15), animate: !calm }) }, "Show where") : "",
        ...(can ? entry.moves.map(m => el("button", { class: "btn", title: "What was decided becomes that sign's",
          onclick: () => settle(entry, () => send("POST", `signs/adrift/${entry.id}/move`, { key: m.key })) }, `It is “${m.text || "no name"}${m.km === null ? "" : ` ${kmText(m.km)}`}” now`)) : []),
        can ? el("button", { class: "btn", title: "It is taken away. Undo brings it back.", onclick: () => settle(entry, () => send("POST", `signs/adrift/${entry.id}/forget`)) }, "Forget it") : ""))));
}
// Your latest decision about signs, and the way to take it back. An order that has been taken out stands.
const undoBar = document.getElementById("undo");
// And what you have decided since you last saved: your item of work, usually one sign site. Save says it
// stays; Discard takes all of it back. Every decision is kept the moment it is made, for others too.
const countOf = n => `${n} change${n === 1 ? "" : "s"}`;
function renderUndo() {
  // Among the issues: your latest decision in operations, whatever it was, and the way to take it back.
  if (view === "issues" || view === "reports") {
    const last = view === "issues" ? entryUndo : reportUndo;
    undoBar.hidden = !last;
    undoBar.replaceChildren(last ? el("button", { class: "link", title: last.in_the_way ? `${last.in_the_way} came after, and is about the same thing` : "Takes back your latest decision",
      onclick: view === "issues" ? undoEntry : undoReport }, `Undo: ${last.what}`) : "");
    return;
  }
  const n = pending.changes.length;
  undoBar.hidden = !(view === "signs" && me.operates && (undoable || n));
  if (undoBar.hidden) { undoBar.replaceChildren(); return; }
  const box = n ? el("div", { class: pendingGrew ? "unsaved new" : "unsaved", role: "status" },
    el("strong", {}, `${countOf(n)} not saved`), el("span", { class: "sub" }, `latest: ${pending.changes[0].what}`),
    el("button", { class: "btn main", title: "Says these decisions are to stay, and ends this item of work. They are kept already, for others too.", onclick: saveSigns }, "Save"),
    pending.in_the_way ? el("span", { class: "sub" }, `Cannot be discarded as a whole: ${pending.in_the_way} came after.`)
      : el("button", { class: "btn", title: "Takes all of them back, in one go", onclick: discardSigns }, "Discard")) : "";
  pendingGrew = false;
  undoBar.replaceChildren(box, undoable ? el("button", { class: "link", title: undoable.in_the_way ? `${undoable.in_the_way} came after, and is about the same sign` : "Takes back your latest decision about signs",
    onclick: undoLast }, `Undo: ${undoable.what}`) : "");
}
async function saveSigns() {
  try { await send("POST", "signs/work/save"); pending = { changes: [], in_the_way: null }; problem = null; }
  catch (err) { problem = err.message; }
  render();
  return !problem;
}
async function discardSigns() {
  const n = pending.changes.length;
  if (n > 3 && !confirm(`Discard your ${n} decisions about signs since you last saved?`)) return false;
  await change(() => send("POST", "signs/work/discard"));
  return !problem;
}
// Leaving the sign site that is open, with decisions not saved: save, discard, the same job, or stay.
const leaveBox = document.getElementById("leave");
function beforeLeavingSite(go) {
  const n = pending.changes.length, site = found && found.sites.find(s => s.key === open), left = (site && site.name) || "this sign site", close = () => leaveBox.close();
  leaveBox.replaceChildren(
    el("h2", { style: "margin:0 0 4px;font-size:17px" }, `${countOf(n)} not saved`),
    el("div", { class: "sub" }, `You are leaving ${left}. The latest: ${pending.changes[0].what}.`),
    el("div", { class: "actions", style: "display:flex;flex-direction:column;align-items:stretch;gap:6px;margin-top:12px" },
      el("button", { class: "btn main", title: "The decisions stay, and this item of work is done", onclick: async () => { close(); if (await saveSigns()) go(); } }, "Save, and go on"),
      pending.in_the_way ? "" : el("button", { class: "btn", title: "All of them are taken back, in one go", onclick: async () => { close(); if (await discardSigns()) go(); } }, "Discard, and go on"),
      el("button", { class: "btn", title: "What you do next belongs to the same piece of work, and is saved or discarded with this", onclick: () => { close(); go(); } }, "Same job: keep going"),
      el("button", { class: "btn quiet", onclick: close }, `Stay at ${left}`)));
  leaveBox.showModal();
}
function undoLast() {
  if (!undoable) return;
  const txid = undoable.txid;
  undoable = null;
  change(() => send("POST", "signs/undo", { txid }));
}

function openSite(key) {
  visit({ view: "signs", site: key });
  const s = found && found.sites.find(x => x.key === key);
  if (s) map.easeTo({ center: [s.lon, s.lat], zoom: Math.max(map.getZoom(), 17), animate: !calm });
  document.getElementById("scroll").scrollTop = 0;
}

function renderSite(site) {
  const can = me.operates, patch = fields => change(() => send("PATCH", `signs/sites/${site.key}`, { area, ...fields }));
  const name = el("input", { type: "text", value: site.name_by_hand ? site.name : "", placeholder: site.name || "What the place is called", maxlength: 120, "aria-label": "Name of the sign site" });
  const back = el("textarea", { rows: 2, maxlength: 400, "aria-label": "Text on the back of the signs", placeholder: site.back_text }, site.back_text_by_hand ? site.back_text : "");
  const toName = el("input", { type: "text", value: site.send_to_name || "", maxlength: 160, placeholder: "Name" });
  const toAddress = el("textarea", { rows: 2, maxlength: 400, placeholder: "Address" }, site.send_to_address || "");
  const about = el("form", { hidden: true, onsubmit: e => { e.preventDefault(); patch({ name: name.value, back_text: back.value, send_to_name: toName.value, send_to_address: toAddress.value }); } },
    el("label", { class: "field" }, "Name. Left empty, the place's own.", name),
    el("label", { class: "field" }, "On the back of every sign here. Left empty, the name and the position.", back),
    el("label", { class: "field" }, "The signs are sent to", toName), toAddress,
    el("div", { class: "actions" }, el("button", { class: "btn main" }, "Save"), el("button", { class: "btn", type: "button", onclick: () => { about.hidden = true; } }, "Cancel")));
  const posts = site.posts.map(post => {
    const others = site.posts.filter(p => p.node === post.node && !p.taken_out);
    // What hangs here and on another post of the place as well: the same destination, signed twice within the site.
    const elsewhere = new Set(site.posts.filter(p => p !== post && !p.taken_out).flatMap(p => p.signs.filter(g => !g.taken_out).map(g => g.to.id)));
    const doubles = post.taken_out ? [] : post.signs.filter(g => !g.taken_out && elsewhere.has(g.to.id));
    return el("div", { class: `post${post.taken_out ? " out" : ""}` },
      el("h3", {}, `Post${site.posts.length > 1 ? ` ${site.posts.indexOf(post) + 1}` : ""}`,
        el("span", { class: "sub", style: "font-weight:400" }, post.taken_out ? ", taken out" : post.assumed ? ", where it is assumed to stand" : ", placed by hand")),
      post.name_sign ? el("div", {}, el("button", { class: "plate top trehvit", "aria-pressed": String(chosen.has(post.name_sign.key)), disabled: !can, title: "Click to choose it for the order",
            "aria-label": `Name sign: ${post.name_sign.text || "no name"}`, onclick: () => choose(post.name_sign.key) }, tMark(), el("span", { class: "name" }, post.name_sign.text || "No name")),
        el("div", { class: "sub below" }, "the name of the place, on top", post.name_sign.ordered ? (post.name_sign.ordered.changed ? `; says something else than in order ${post.name_sign.ordered.order}` : `; in order ${post.name_sign.ordered.order}`) : "",
          post.name_sign.to_order ? onTheOrder(post.name_sign.key) : "")) : "",
      ...post.signs.map(sign => signRow(sign, others, post)),
      post.signs.length ? "" : el("div", { class: "sub" }, "No signs hang here."),
      can ? el("div", { class: "actions" },
        post.taken_out ? el("button", { class: "btn", onclick: () => change(() => send("PATCH", `signs/posts/${post.key}`, { taken_out: false })) }, "Put the post back") : "",
        !post.taken_out && !post.assumed ? el("button", { class: "btn", title: "Back to where a post is assumed to stand: beside the junction, in the widest opening between the paths", onclick: () => change(() => send("PATCH", `signs/posts/${post.key}`, { assumed: true })) }, "Where it is assumed to stand") : "",
        !post.taken_out ? el("button", { class: "btn", title: "A sign with the name of the place and no arrow, on top of the post: for where nothing else says where one is", onclick: () => change(() => send("PATCH", `signs/posts/${post.key}`, { name_sign: !post.name_sign })) }, post.name_sign ? "No name sign" : "A name sign on top") : "",
        !post.taken_out ? el("button", { class: "btn", title: "A sign to somewhere the routes do not give this post: a place nearer than the next hut, or further on. Click the place on the map, or a spot on a route.", onclick: () => place({ to: post.key }) }, "Another destination") : "",
        !post.taken_out ? el("button", { class: "btn", title: "For a place with several posts: click the map where the other one stands, then move signs onto it", onclick: () => place({ node: post.node }) }, "Another post here") : "",
        doubles.length ? el("button", { class: "btn", title: `Takes out of this post the ${count(doubles.length)} that ${doubles.length === 1 ? "hangs" : "hang"} on another post of the place as well: ${doubles.map(g => g.text).join(", ")}. They stay on the other post.`,
          onclick: () => change(async () => { for (const g of doubles) await send("PATCH", "signs", { key: g.key, taken_out: true }); }) }, `Take out the ${doubles.length} that hang elsewhere too`) : "",
        !post.taken_out && site.posts.filter(p => !p.taken_out).length > 1 ? el("button", { class: "btn", onclick: () => change(() => send("PATCH", `signs/posts/${post.key}`, { taken_out: true })) }, "Take the post out") : "") : "");
  });
  panel.replaceChildren(
    el("div", { class: "backbar" }, el("button", { class: "btn", onclick: () => visit({ site: null }) }, "‹ All sites"), el("span", { class: "sub grow" }, site.code || ""),
      ...(() => { const row = listed(), i = row.findIndex(s => s.key === site.key);       // the one before and the one after, in the list as it is shown
        const step = (to, label, title) => el("button", { class: "btn step", title, "aria-label": title, disabled: !to, onclick: () => to && openSite(to.key) }, label);
        return [step(row[i - 1], "‹", "The site before in the list"), step(i < 0 ? null : row[i + 1], "›", "The next site in the list")]; })()),
    el("h2", {}, siteLabel(site), site.no_signs ? " " : "", site.no_signs ? el("span", { class: "tag" }, "no signs") : ""),
    problem ? el("div", { class: "error", role: "alert" }, `That was not done: ${problem}`) : "",
    el("div", { class: "sub" }, `${site.routes.join(", ")}.`, site.other_areas.length ? ` Shared with ${site.other_areas.join(", ")}: agree on the signs with whoever looks after it.` : "",
      site.free ? " Put here by hand, on the route." : "", site.named ? "" : ` It has no name: a sign site ${site.free ? "at a spot on a route" : "at a junction that is no place"}. Give it one for the back of its signs.`,
      !site.named && site.near ? ` ${site.near.name} (${site.near.type}) lies ${site.near.metres} m from it.` : ""),
    can && !site.named && site.near ? el("div", { class: "actions" }, el("button", { class: "btn", title: "Gives the sign site this name, which goes on the back of its signs. It can be changed.", onclick: () => patch({ name: site.near.name }) }, `Call it ${site.near.name}`)) : "",
    site.no_signs ? el("div", { class: "sub" }, "Said to have no signs: nothing here is ordered.") : "",
    can ? el("div", { class: "actions" },
      site.free ? el("button", { class: "btn", onclick: () => { if (confirm("Take this sign site away? It was put here by hand.")) { open = null; change(() => send("DELETE", `signs/sites/${site.key}`)); } } }, "Take the site away")
        : el("button", { class: "btn", title: site.no_signs ? "" : "For a junction or a place where there are to be no signs: nothing of it is ordered", onclick: () => patch({ no_signs: !site.no_signs }) }, site.no_signs ? "There are signs here after all" : "No signs here")) : "",
    can ? el("div", { class: "sub" }, "Click the signs that are to be made, here and at other sites, then add them to the order. Drag the post on the map to where it stands: an arrow is read by someone standing at the junction and looking at the post, so a post on the other side of the path turns its arrows.") : "",
    el("div", { class: "actions" }, el("a", { class: "btn", href: new URL(`signs/sheet?area=${area}&site=${encodeURIComponent(site.key)}`, API).href, target: "_blank", rel: "noopener",
      title: "A page to print or save as PDF: the map, the posts and what hangs on each, with a box to tick" }, "Field sheet")),
    can ? el("div", { class: "actions" }, el("button", { class: "btn", title: "For a site that is new: chooses every sign here", onclick: () => { for (const p of site.posts.filter(p => !p.taken_out)) { if (p.name_sign) chosen.add(p.name_sign.key); for (const g of p.signs.filter(g => !g.taken_out)) chosen.add(g.key); } render(); } }, "Choose all its signs")) : "",
    ...posts,
    el("h2", {}, "On the back"), el("div", { style: "white-space:pre-wrap" }, site.back_text),
    site.send_to_name || site.send_to_address ? el("div", { class: "sub", style: "white-space:pre-wrap;margin-top:4px" }, `Sent to ${[site.send_to_name, site.send_to_address].filter(Boolean).join(", ")}`) : "",
    can ? el("div", { class: "actions" }, el("button", { class: "btn", onclick: () => { about.hidden = false; name.focus(); } }, "Change name, back text or who they are sent to")) : "", about);
}

// What is said under a sign that is on the order, with the way to take it off again.
const onTheOrder = key => el("span", {}, " ", el("span", { class: "tag warn" }, "on the order"), me.operates ? " " : "",
  me.operates ? el("button", { class: "link", onclick: () => change(() => send("POST", "signs/to-order", { keys: [key], to_order: false })) }, "take it off") : "");
// One sign, drawn as it hangs. A click chooses it for the order; "Change" says something else of it than what is worked out.
function signRow(sign, posts, post) {
  const can = me.operates, green = sign.colour === "grønn";
  const words = [el("span", { class: "km" }, kmText(sign.km)), el("span", { class: "name" }, sign.text || "No name")];
  const plate = el("button", { class: `plate ${sign.colour} ${sign.direction === "høyre" ? "høyre" : ""}${sign.taken_out ? " out" : ""}`, "aria-pressed": String(chosen.has(sign.key)), disabled: !can || sign.taken_out,
      "aria-label": `${sign.text || "No name"}${sign.km === null ? "" : `, ${kmText(sign.km)}`}, arrow ${sign.direction || "none"}, ${sign.colour}`,
      "data-post": post.key, "data-bearing": sign.bearing, onmouseenter: () => point(post.key, sign.bearing), onmouseleave: () => point(null, null),
      onfocus: () => point(post.key, sign.bearing), onblur: () => point(null, null),
      title: `${sign.direction || "no arrow"}, ${sign.colour}, ${sign.routes.join(", ")}. Click to choose it for the order.`, onclick: () => choose(sign.key) },
    ...(green ? [arrowMark(sign.direction, "#fff"), ...words, walkerMark()] : [tMark(), arrowMark(sign.direction, "#17191a"), ...words]));
  const notes = [sign.added ? `added by hand${sign.routes.length ? `, by ${sign.routes.join(", ")}` : ""}` : sign.onward ? `on past ${sign.onward}, by ${sign.routes.join(", ")}` : sign.routes.join(", "),
    sign.no_way ? "no way leads there over the routes" : "", sign.by_hand.length ? `by hand: ${sign.by_hand.map(f => ({ distance_m: "distance", post_no: "post" }[f] || f)).join(", ")}` : "",
    sign.unnamed ? "points to the end of a route at no place" : "",
    sign.ordered ? (sign.ordered.changed ? `says something else than in order ${sign.ordered.order}` : `in order ${sign.ordered.order}`) + (sign.ordered.up ? ", up" : ", not up yet") : ""].filter(Boolean).join("; ");
  const below = el("div", { class: "sub below" }, notes, sign.to_order ? onTheOrder(sign.key) : "",
    can && sign.ordered ? el("button", { class: "link", style: "margin-left:8px", onclick: () => change(() => send("POST", "signs/up", { keys: [sign.key], up: !sign.ordered.up })) }, sign.ordered.up ? "not up after all" : "it is up") : "",
    can ? el("button", { class: "link", style: "margin-left:8px", "aria-expanded": String(editing === sign.key), onclick: () => { editing = editing === sign.key ? null : sign.key; render(); } }, "Change") : "");
  if (editing !== sign.key || !can) return el("div", {}, plate, below);
  const out = el("button", { class: "btn", type: "button", onclick: () => { editing = null; change(() => send("PATCH", "signs", { key: sign.key, taken_out: !sign.taken_out })); } }, sign.taken_out ? "Put the sign back" : "Take the sign out");
  if (sign.no_way) return el("div", {}, plate, below, el("form", { class: "edit" }, el("div", { class: "actions" }, out)));
  const G = sign.generated;
  const way = el("select", { "aria-label": "Arrow" }, el("option", { value: "" }, `As worked out: ${G.direction || "none"}`), ...["venstre", "rett fram", "høyre"].map(d => el("option", { value: d, selected: sign.by_hand.includes("direction") && sign.direction === d }, d)));
  const colour = el("select", { "aria-label": "Colour" }, ...["trehvit", "grønn"].map(c => el("option", { value: c, selected: sign.colour === c }, c)));
  const text = el("input", { type: "text", maxlength: 120, value: sign.by_hand.includes("text") || sign.to.type === "point" ? sign.text : "", placeholder: G.text || "Text on the sign", "aria-label": "Text on the sign" });
  const km = el("input", { type: "number", min: 0, max: 500, step: 0.5, value: sign.by_hand.includes("distance_m") ? sign.km : "", placeholder: G.km === null ? "none" : String(G.km), "aria-label": "Kilometres on the sign" });
  const onPost = posts.length > 1 ? el("select", { "aria-label": "Which post it hangs on" }, ...posts.map((p, i) => el("option", { value: p.no, selected: p.signs.includes(sign) }, `Post ${i + 1}`))) : null;
  const form = el("form", { class: "edit", onsubmit: e => { e.preventDefault(); editing = null;
      change(() => send("PATCH", "signs", { key: sign.key, direction: way.value, colour: colour.value === "trehvit" ? "" : colour.value, text: text.value,
        distance_m: km.value === "" ? 0 : Number(km.value) * 1000, ...(onPost ? { post_no: Number(onPost.value) === 1 ? 0 : Number(onPost.value) } : {}) })); } },
    el("div", { class: "row" }, el("label", { class: "field" }, "Arrow", way), el("label", { class: "field" }, "Colour", colour)),
    el("label", { class: "field" }, "Text. Left empty, as worked out.", text),
    el("div", { class: "row" }, el("label", { class: "field" }, `Km. Left empty, ${G.km === null ? "none" : G.km}.`, km), onPost ? el("label", { class: "field" }, "Hangs on", onPost) : el("span", {})),
    el("div", { class: "sub", style: "margin-top:6px" }, `${sign.metres.toLocaleString("en")} m along ${sign.routes.join(", ")}, leaving towards ${sign.bearing === null ? "?" : `${sign.bearing}°`}.`),
    el("div", { class: "actions" }, el("button", { class: "btn main" }, "Save"), out,
      el("button", { class: "btn", type: "button", onclick: () => { editing = null; render(); } }, "Cancel")));
  return el("div", {}, plate, below, form);
}

// A further post, or a sign site at a spot: the next click on the map is where it stands.
function place(what) {
  placing = what;
  map.getCanvas().style.cursor = "crosshair";
  hint.hidden = false;
  hint.replaceChildren(what === "site" ? "Click a route where the sign site is." : what.to ? "Click the place the sign is to point to, or a spot on a route." : "Click the map where the other post stands.",
    el("button", { class: "btn", onclick: stopPlacing }, "Cancel"));
}
function stopPlacing() { placing = null; hint.hidden = true; map.getCanvas().style.cursor = ""; }
