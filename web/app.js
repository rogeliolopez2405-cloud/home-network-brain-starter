// Home Network BRAIN Starter — web app.
// Renders one shape (HomeView, from GET /api/home) and sends every change to
// the API. No simulated state and no demo special-cases: the demo provider is
// just another provider behind the same endpoints.
(() => {
"use strict";

const S = { session: null, home: null, devices: [], rooms: [], room: null, wizard: 3, busy: new Set(),
  system: null, providers: { providers: [], available: [] }, providerConfig: {}, backups: null, openForm: null };
const $ = (sel, el = document) => el.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

// ── API ──────────────────────────────────────────────────────────────
class ApiError extends Error { constructor(status, message, body) { super(message); this.status = status; this.body = body; } }

async function api(method, url, body) {
  const headers = { "Accept": "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && S.session && S.session.csrf_token) headers["X-CSRF-Token"] = S.session.csrf_token;
  const r = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (r.status === 401 && !url.startsWith("/api/auth/")) { S.session = { authenticated: false }; render(); }
  if (!r.ok) throw new ApiError(r.status, (data && (data.error || data.detail)) || `Request failed (${r.status})`, data);
  return data;
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 2600);
}

async function loadSession() { S.session = await api("GET", "/api/session"); }
async function loadHome() { S.home = await api("GET", "/api/home"); }
async function loadDevices() { S.devices = (await api("GET", "/api/devices")).devices; }
async function loadRooms() { S.rooms = (await api("GET", "/api/rooms")).rooms; }

// ── shared bits ──────────────────────────────────────────────────────
const ICON = {
  bulb: '<svg class="icon-bulb" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V17h5v-1.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z"/></svg>',
  switch: '<svg class="icon-bulb" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="3" width="12" height="18" rx="3"/><rect x="9.5" y="7" width="5" height="10" rx="1.5"/></svg>',
  dots: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>',
  mute: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5L6 9H3v6h3l5 4z"/><path d="M22 9l-6 6M16 9l6 6"/></svg>',
};
const ctl = (dev, cap) => dev.controls.find(c => c.capability === cap);
const powerCtl = dev => ctl(dev, "POWER") || ctl(dev, "SWITCH");
const devById = id => S.home.devices.find(d => d.id === id);
const roomName = id => (S.home.rooms.find(r => r.id === id) || S.rooms.find(r => r.id === id) || {}).name || "";

function freshBadge(dev, c) {
  if (!dev.reachable || (c && c.freshness === "UNKNOWN")) return '<span class="fresh unknown">Can’t reach</span>';
  if (c && c.freshness === "ASSUMED") return '<span class="fresh assumed">Not confirmed yet</span>';
  if (c && c.freshness === "STALE") return '<span class="fresh stale">Last known</span>';
  return "";
}

// ── actions: API → service → adapter → state read-back → audit → refresh ──
async function act(id, capability, value, confirm = false) {
  const key = id + capability;
  if (S.busy.has(key)) return;
  S.busy.add(key);
  try {
    const r = await api("POST", `/api/devices/${encodeURIComponent(id)}/actions`, { capability, value, confirm });
    if (r.outcome === "FAILED") toast(r.detail || "The device didn’t answer.");
    else if (r.outcome === "ACCEPTED" && r.detail) toast(r.detail);
  } catch (e) {
    toast(e.body && e.body.detail ? e.body.detail : e.message);
  } finally {
    S.busy.delete(key);
    await refresh();
  }
}

// ── home ─────────────────────────────────────────────────────────────
function clock() {
  const d = new Date();
  return `<strong id="clock-time">${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</strong><span>${d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })}</span>`;
}

function lightTile(dev) {
  const p = powerCtl(dev), b = ctl(dev, "BRIGHTNESS");
  const known = dev.on !== null;
  const on = dev.on === true;
  const detail = !known ? freshBadge(dev, p) : on ? (b && b.value != null ? `<b>${b.value}%</b>` : "<b>On</b>") : "Off";
  const hasMore = dev.controls.some(c => c.writable && !["POWER", "SWITCH"].includes(c.capability));
  return `<article class="tile${on ? " on" : ""}${known ? "" : " dim"}">
    <div class="tile-top">${dev.kind === "SWITCH" ? ICON.switch : ICON.bulb}
      ${p ? `<button class="toggle" role="switch" aria-checked="${known ? on : "mixed"}" aria-label="${esc(dev.name)}" data-act="power" data-id="${dev.id}" data-cap="${p.capability}"></button>` : ""}
    </div>
    <div><div class="tile-name">${esc(dev.name)}</div><div class="tile-room">${esc(roomName(dev.room_id))}</div></div>
    <div class="tile-state"><span>${detail}</span>${hasMore ? `<button class="more" aria-label="More for ${esc(dev.name)}" data-open="${dev.id}">${ICON.dots}</button>` : ""}</div>
  </article>`;
}

function tvCard(dev) {
  const p = ctl(dev, "POWER"), v = ctl(dev, "VOLUME"), m = ctl(dev, "MUTE"), a = ctl(dev, "APP_LAUNCH");
  const known = dev.on !== null, on = dev.on === true, off = !on;
  return `<article class="tile tv${known ? "" : " dim"}">
    <div class="tile-top"><div><div class="tile-name">${esc(dev.name)}</div><div class="tile-room">${known ? (on ? "On" : "Off") : freshBadge(dev, p)}</div></div>
      ${p ? `<button class="toggle" role="switch" aria-checked="${known ? on : "mixed"}" aria-label="${esc(dev.name)} power" data-act="power" data-id="${dev.id}" data-cap="POWER"></button>` : ""}</div>
    ${v || m ? `<div class="row">
      ${m ? `<button class="icon-btn" aria-label="Mute" aria-pressed="${!!m.value}" data-act="mute" data-id="${dev.id}" ${off ? "disabled" : ""}>${ICON.mute}</button>` : ""}
      ${v ? `<input class="vol" type="range" min="0" max="100" value="${v.value ?? 0}" aria-label="${esc(dev.name)} volume" id="vol-${dev.id}" data-act="volume" data-id="${dev.id}" ${off ? "disabled" : ""}>` : ""}
    </div>` : ""}
    ${a ? `<div class="apps">${(a.options.apps || []).map(app => `<button class="app-chip" data-act="app" data-id="${dev.id}" data-app="${esc(app)}" ${off ? "disabled" : ""}>${esc(app)}</button>`).join("")}</div>` : ""}
  </article>`;
}

function camCard(dev) {
  const ev = ctl(dev, "CAMERA_EVENT"), live = ctl(dev, "CAMERA_LIVE");
  const last = ev && ev.value && ev.value.last ? `Last ${esc(ev.value.last)}${ev.value.minutes_ago != null ? ` ${esc(ev.value.minutes_ago)} min ago` : ""}` : "No recent events";
  return `<article class="tile cam">
    <div class="cam-view"><div><b>${esc(dev.name)}</b><small>${live ? "Live video needs a camera provider module that supports streaming." : "Events only — this camera offers no live video."}</small></div></div>
    <div class="cam-foot"><div><div class="tile-name">${esc(dev.name)}</div><span>${dev.reachable ? last : "Can’t reach"}</span></div></div>
  </article>`;
}

// Locks, thermostats, sensors, anything else: only the controls the device declares.
const THERMO_MODES = { OFF: "Off", HEAT: "Heat", COOL: "Cool", AUTO: "Auto" };
const lockWord = v => v === "LOCKED" ? "Locked" : v === "UNLOCKED" ? "Unlocked" : null;
function moreCard(dev) {
  const p = powerCtl(dev), lock = ctl(dev, "LOCK"), th = ctl(dev, "THERMOSTAT");
  const known = c => dev.reachable && c && c.value != null && c.freshness !== "UNKNOWN";
  let body = "";
  if (lock) {
    const v = known(lock) ? lock.value : null;
    body += `<div class="tile-state"><span>${v ? `<b>${lockWord(v)}</b> ${freshBadge(dev, lock)}` : freshBadge(dev, lock) || "State unknown"}</span>
      ${lock.writable && dev.reachable ? `<button class="btn" data-lockask="${dev.id}" data-v="${v === "LOCKED" ? "UNLOCKED" : "LOCKED"}">${v === "LOCKED" ? "Unlock…" : "Lock…"}</button>` : ""}</div>`;
  }
  if (th) {
    const v = known(th) ? th.value : null;
    const lo = th.options.min_c ?? 7, hi = th.options.max_c ?? 32, modes = th.options.modes || Object.keys(THERMO_MODES);
    body += `<div class="thermo"><div class="thermo-read"><b>${v ? `${v.setpoint_c.toFixed(1)} °C` : "–"}</b>
        <small>${v ? `${esc(THERMO_MODES[v.mode] || v.mode)} · set to ${Math.round(v.setpoint_c * 9 / 5 + 32)} °F ${freshBadge(dev, th)}` : freshBadge(dev, th) || "State unknown"}</small></div>
      ${th.writable && v ? `<div class="row"><button class="icon-btn" aria-label="${esc(dev.name)} cooler" data-act="thermo" data-id="${dev.id}" data-d="-0.5" ${v.setpoint_c <= lo ? "disabled" : ""}>−</button>
        <button class="icon-btn" aria-label="${esc(dev.name)} warmer" data-act="thermo" data-id="${dev.id}" data-d="0.5" ${v.setpoint_c >= hi ? "disabled" : ""}>+</button></div>` : ""}</div>
      ${th.writable && v ? `<div class="apps" role="group" aria-label="${esc(dev.name)} mode">${modes.map(m => `<button class="app-chip" aria-pressed="${v.mode === m}" data-act="thermomode" data-id="${dev.id}" data-mode="${esc(m)}">${esc(THERMO_MODES[m] || m)}</button>`).join("")}</div>` : ""}`;
  }
  if (!body) body = `<div class="tile-state"><span>${!dev.reachable ? freshBadge(dev) : p ? (dev.on === null ? freshBadge(dev, p) : dev.on ? "<b>On</b>" : "Off") : "Responding"}</span></div>`;
  return `<article class="tile${dev.reachable ? "" : " dim"}">
    <div class="tile-top"><div><div class="tile-name">${esc(dev.name)}</div><div class="tile-room">${esc(roomName(dev.room_id))}</div></div>
      ${p ? `<button class="toggle" role="switch" aria-checked="${dev.on === null ? "mixed" : dev.on}" aria-label="${esc(dev.name)}" data-act="power" data-id="${dev.id}" data-cap="${p.capability}"></button>` : ""}</div>
    ${body}
  </article>`;
}
const cardFor = d => d.section === "lights" ? lightTile(d) : d.section === "entertainment" ? tvCard(d) : d.section === "cameras" ? camCard(d) : moreCard(d);

// A HIGH-sensitivity write (LOCK) always goes through an explicit confirmation; the server refuses it otherwise.
function openLockSheet(id, value) {
  const dev = devById(id), sh = $("#sheet"), unlocking = value === "UNLOCKED";
  sh.innerHTML = `<div class="sheet-body">
    <div class="sheet-head"><div><h3 id="sheet-title">${unlocking ? "Unlock" : "Lock"} ${esc(dev.name)}?</h3>
      <div class="tile-room">${unlocking ? "Anyone at this door will be able to open it." : "The door will lock if it’s closed."}</div></div></div>
    <div class="row"><button class="btn" data-close>Cancel</button>
      <button class="btn primary" data-lockgo="${dev.id}" data-v="${value}">${unlocking ? "Unlock" : "Lock"}</button></div>
  </div>`;
  if (!sh.open) sh.showModal();
}

function section(title, key, render, wide = false, count = true) {
  const sec = S.home.sections[key];
  const ids = sec.device_ids.filter(id => !S.room || devById(id).room_id === S.room);
  if (!ids.length) return "";
  const note = sec.degraded ? `<p><span class="fresh unknown">${sec.unreachable ? plural(sec.unreachable, "device") + " can’t be reached" : "Some can’t be reached"}</span></p>` : (count ? `<p>${ids.length}</p>` : "");
  return `<section class="section"><div class="section-head"><h2>${title}</h2>${note}</div>
    <div class="grid${wide ? " wide" : ""}">${ids.map(id => render(devById(id))).join("")}</div></section>`;
}

function renderHome() {
  const h = S.home, st = h.status, sys = h.system;
  const backup = sys.backup || {};
  return `
  ${S.session.setup_complete === false ? `<div class="notice"><span><b>Setup isn’t finished.</b> A few steps are left: services, rooms, favorites and a first backup.</span><a class="btn" href="#setup">Continue setup</a></div>` : ""}
  ${st.needs_review ? `<div class="notice"><span><b>${plural(st.needs_review, "device")}</b> found and waiting for you to say whether ${st.needs_review === 1 ? "it’s" : "they’re"} yours.</span><a class="btn" href="#setup">Review</a></div>` : ""}
  <header class="hero">
    <div><h1>${esc(h.house.name)}</h1>
      <p class="status-line"><span class="dot ${st.level === "ok" ? "" : "warn"}"></span>${esc(st.summary)}</p>
      <p class="hero-facts"><span>${st.lights_on || !st.lights_unknown ? `<b>${st.lights_on}</b> ${st.lights_on === 1 ? "light" : "lights"} on${st.lights_unknown ? ` · <b>${st.lights_unknown}</b> unknown` : ""}` : `<b>${st.lights_unknown}</b> ${st.lights_unknown === 1 ? "light" : "lights"} unknown`}</span><span><b>${h.rooms.length}</b> rooms</span><span><b>${st.devices}</b> devices</span></p>
    </div>
    <div class="clock">${clock()}</div>
  </header>

  ${h.rooms.length ? `<section class="section" aria-label="Rooms">
    <div class="section-head"><h2>Rooms</h2>${S.room ? '<button class="link-btn" data-room="">Show all</button>' : ""}</div>
    <div class="rooms">${h.rooms.map(r => `<button class="room-chip" aria-pressed="${S.room === r.id}" data-room="${r.id}"><b>${esc(r.name)}</b><span>${r.degraded ? "Can’t reach some · " : ""}${r.lights_on ? `${r.lights_on} on · ` : ""}${plural(r.device_count, "device")}</span></button>`).join("")}</div>
  </section>` : ""}

  ${!S.room ? section("Favorites", "favorites", cardFor,
      S.home.sections.favorites.device_ids.some(id => devById(id).section !== "lights"), false) : ""}
  ${section("Lights &amp; switches", "lights", lightTile)}
  ${section("TV &amp; entertainment", "entertainment", tvCard, true, false)}
  ${section("Cameras", "cameras", camCard, true, false)}
  ${section("Locks, climate &amp; more", "more", moreCard, true, false)}

  ${!S.room ? `<section class="section"><div class="section-head"><h2>Home system</h2></div>
    <div class="tile sys">
      ${h.sections.network.device_ids.map(id => devById(id)).map(d => `<div class="sys-row"><b>${esc(d.name)}</b><span class="pill ${d.reachable ? "" : "bad"}">${d.reachable ? "Responding" : "Can’t reach"}</span></div>`).join("")}
      <div class="sys-row"><b>Devices responding</b><span class="pill ${sys.devices_responding.ok < sys.devices_responding.total ? "bad" : ""}">${sys.devices_responding.ok} of ${sys.devices_responding.total}</span></div>
      ${sys.services.map(s => `<div class="sys-row"><b>${esc(s.name)}</b><span class="pill ${s.status === "OK" ? "" : s.status === "AUTH_REQUIRED" ? "warn" : "bad"}">${esc(s.label)}</span></div>`).join("")}
      <div class="sys-row"><b>Waiting for your review</b><span class="pill ${sys.needs_review ? "warn" : "idle"}">${sys.needs_review || "None"}</span></div>
      <div class="sys-row"><b>Last backup</b><span class="pill ${backup.status === "ok" ? "" : backup.status === "never" ? "idle" : "warn"}">${backup.status === "never" ? "Not yet" : backup.at ? new Date(backup.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : esc(backup.label)}</span></div>
    </div></section>` : ""}
  ${st.devices === 0 && !st.needs_review ? `<div class="card"><h2>No devices yet</h2><p>Connect a service in Settings, then tell the hub which devices are yours.</p></div>` : ""}`;
}

// Add, rename (saved when the field changes) and delete rooms. Deleting a room keeps its devices, unassigned.
function roomEditor() {
  return `<form class="room-add" id="room-add" novalidate>
      <input class="text-input" id="new-room" maxlength="120" placeholder="e.g. Garage" aria-label="New room name" autocomplete="off">
      <button class="btn" type="submit">Add room</button></form>
    <div class="review-list">${S.rooms.map(r => `<div class="review room-edit">
      <input class="text-input" id="rn-${r.id}" value="${esc(r.name)}" maxlength="120" aria-label="Rename ${esc(r.name)}" data-rename="${r.id}">
      <button class="btn" data-delroom="${r.id}" aria-label="Delete ${esc(r.name)}">${S.confirmDel === r.id ? "Tap again to delete" : "Delete"}</button></div>`).join("")
      || '<p class="hint">No rooms yet — add one above.</p>'}</div>`;
}

function renderRooms() {
  const rooms = S.home.rooms;
  return `<header class="section-head"><h2 class="h-page">Rooms</h2><button class="btn" data-editrooms aria-pressed="${!!S.editRooms}">${S.editRooms ? "Done" : "Edit rooms"}</button></header>
  ${S.editRooms ? `<div class="card">${roomEditor()}</div>` : ""}
  ${rooms.length ? `<div class="grid wide">${rooms.map(r => {
    const devs = S.home.devices.filter(d => d.room_id === r.id);
    return `<article class="tile"><div class="tile-top"><div><div class="tile-name big">${esc(r.name)}</div><div class="tile-room">${plural(devs.length, "device")}</div></div>
      <button class="btn" data-room="${r.id}" data-go="home">Open</button></div>
      <div class="tile-state"><span>${devs.map(d => esc(d.name)).join(" · ")}</span></div></article>`;
  }).join("")}</div>` : '<p class="hint">Rooms appear once you’ve claimed devices and put them in rooms.</p>'}`;
}

// ── device sheet ─────────────────────────────────────────────────────
const HUES = [[0, 75], [32, 60], [50, 80], [120, 60], [200, 70], [270, 60]];
function openSheet(id) {
  const dev = devById(id), sh = $("#sheet");
  const b = ctl(dev, "BRIGHTNESS"), ct = ctl(dev, "COLOR_TEMPERATURE"), col = ctl(dev, "COLOR");
  sh.innerHTML = `<div class="sheet-body">
    <div class="sheet-head"><div><h3 id="sheet-title">${esc(dev.name)}</h3><div class="tile-room">${esc(roomName(dev.room_id))} ${freshBadge(dev, b || ct || col)}</div></div><button class="btn" data-close>Done</button></div>
    ${b ? `<div class="field"><label for="br-${id}">Brightness <span id="brv">${b.value ?? "–"}%</span></label><input type="range" id="br-${id}" min="1" max="100" value="${b.value ?? 50}" data-act="brightness" data-id="${id}"></div>` : ""}
    ${ct ? `<div class="field"><label for="ct-${id}">Warmth <span id="ctv">${ct.value ?? "–"} K</span></label><input class="ct" type="range" id="ct-${id}" min="${ct.options.min ?? 2000}" max="${ct.options.max ?? 6500}" step="100" value="${ct.value ?? 3000}" data-act="ct" data-id="${id}"></div>` : ""}
    ${col ? `<div class="field"><label>Color</label><div class="hues">${HUES.map(([h, s]) => `<button class="hue" data-hue="${h}" aria-label="Hue ${h}" aria-pressed="${!!(col.value && col.value.h === h)}" data-act="color" data-id="${id}" data-h="${h}" data-s="${s}"></button>`).join("")}</div></div>` : ""}
  </div>`;
  sh.querySelectorAll(".hue").forEach(el => { el.style.background = `hsl(${el.dataset.h} ${el.dataset.s}% 55%)`; });
  if (!sh.open) sh.showModal();
}

// ── devices (review, rooms, names, favorites) ───────────────────────
const REVIEW = [["MINE", "This is mine"], ["NOT_MINE", "Not mine"], ["IGNORED", "Ignore"]];
function reviewRow(d, withRoom) {
  const rooms = S.rooms;
  return `<div class="review"><div><b>${esc(d.name)}</b><small>${esc(d.technical.manufacturer || "Unknown maker")}${d.technical.model ? " " + esc(d.technical.model) : ""}${d.room ? " · " + esc(d.room) : ""}</small></div>
    <div class="choice" role="group" aria-label="${esc(d.name)}">${REVIEW.map(([v, l]) => `<button data-v="${v}" data-rev="${d.id}" aria-pressed="${d.review === v}">${l}</button>`).join("")}</div>
    ${withRoom && d.review === "MINE" ? `<select class="text-input" id="room-${d.id}" data-roomfor="${d.id}" aria-label="Room for ${esc(d.name)}"><option value="">No room</option>${rooms.map(r => `<option value="${r.id}" ${d.room_id === r.id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select>
      <button class="btn" data-fav="${d.id}" aria-pressed="${d.favorite}">${d.favorite ? "★ Favorite" : "☆ Favorite"}</button>` : ""}
  </div>`;
}

function renderDevices() {
  const groups = [["UNREVIEWED", "Waiting for review"], ["MINE", "Yours"], ["NOT_MINE", "Not yours"], ["IGNORED", "Ignored"]];
  return `<div class="card"><h2>Devices</h2><p>Everything the hub can see. Only devices marked as yours appear on the Home Hub and can be controlled.</p>
    <div class="actions start"><button class="btn" data-all="MINE" ${S.devices.some(d => d.review === "UNREVIEWED") ? "" : "disabled"}>Mark the rest as mine</button></div></div>
    ${groups.map(([k, title]) => { const list = S.devices.filter(d => d.review === k); return list.length ? `<section class="section"><div class="section-head"><h2>${title}</h2><p>${list.length}</p></div><div class="review-list">${list.map(d => reviewRow(d, true)).join("")}</div></section>` : ""; }).join("")}`;
}

// ── first run: create owner, then a short guided setup ──────────────
function renderBootstrap(err) {
  return `<div class="auth"><form class="card" id="bootstrap-form" novalidate>
    <span class="eyebrow">Set up · steps 1–3 of 12</span>
    <h2>One app for the whole house</h2>
    <p>Lights, TVs, cameras and your network in one place, whatever brand or app runs them today. First, create the owner account and name your home.</p>
    <label class="field" for="bs-code"><span>Setup code</span><input class="text-input" id="bs-code" autocomplete="one-time-code" autocapitalize="characters" placeholder="XXXX-XXXX-XXXX" required>
      <small>On the hub, run <span class="code-line">sudo /opt/hnb/bin/setup-code</span> to see it. It works once.</small></label>
    <label class="field" for="bs-house"><span>Name your home</span><input class="text-input" id="bs-house" value="My Home" maxlength="60"></label>
    <label class="field" for="bs-user"><span>Your sign-in name</span><input class="text-input" id="bs-user" autocomplete="username" autocapitalize="none" placeholder="e.g. alex" required></label>
    <label class="field" for="bs-pass"><span>Passphrase</span><input class="text-input" id="bs-pass" type="password" autocomplete="new-password" minlength="10" required><small>At least 10 characters. A few unrelated words works well.</small></label>
    <label class="field" for="bs-pass2"><span>Passphrase again</span><input class="text-input" id="bs-pass2" type="password" autocomplete="new-password" required></label>
    ${err ? `<p class="form-error" role="alert">${esc(err)}</p>` : ""}
    <div class="actions"><span></span><button class="btn primary" type="submit">Create owner</button></div>
    <p class="hint">Nothing leaves this house unless you add a service that needs the internet.</p>
  </form></div>`;
}

function renderLogin(err) {
  return `<div class="auth"><form class="card" id="login-form" novalidate>
    <span class="eyebrow">${esc(S.session.house || "Home")}</span>
    <h2>Sign in</h2>
    <label class="field" for="li-user"><span>Sign-in name</span><input class="text-input" id="li-user" autocomplete="username" autocapitalize="none" required></label>
    <label class="field" for="li-pass"><span>Passphrase</span><input class="text-input" id="li-pass" type="password" autocomplete="current-password" required></label>
    ${err ? `<p class="form-error" role="alert">${esc(err)}</p>` : ""}
    <div class="actions"><span></span><button class="btn primary" type="submit">Sign in</button></div>
  </form></div>`;
}

// A provider's settings + credentials, rendered from what the module declares.
function providerForm(p, mode) {
  const settings = (p.settings || []).map(f => {
    const cur = mode === "update" && S.providerConfig[p.id] ? S.providerConfig[p.id][f.name] : undefined;
    const val = cur !== undefined ? cur : f.default;
    const input = f.choices && f.choices.length
      ? `<select class="text-input" id="set-${esc(p.id)}-${esc(f.name)}" data-field="set:${esc(f.name)}">${f.choices.map(c => `<option ${c === val ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`
      : `<input class="text-input" id="set-${esc(p.id)}-${esc(f.name)}" data-field="set:${esc(f.name)}" value="${esc(val)}" autocomplete="off">`;
    return `<label class="field" for="set-${esc(p.id)}-${esc(f.name)}"><span>${esc(f.label)}</span>${input}${f.help ? `<small>${esc(f.help)}</small>` : ""}</label>`;
  }).join("");
  const secrets = (p.secrets || []).map(f => `<label class="field" for="sec-${esc(p.id)}-${esc(f.name)}"><span>${esc(f.label)}</span>
      <input class="text-input" type="password" id="sec-${esc(p.id)}-${esc(f.name)}" data-field="sec:${esc(f.name)}" autocomplete="new-password" placeholder="${mode === "update" ? "Leave blank to keep the saved one" : ""}">
      <small>${f.help ? esc(f.help) + " " : ""}Stored encrypted on the hub; never shown again.</small></label>`).join("");
  if (!settings && !secrets && mode === "update") return "";
  return `<form class="prov-form" data-provider="${esc(p.id)}" data-mode="${mode}" novalidate>${settings}${secrets}
    <div class="actions"><span></span><button class="btn primary" type="submit">${mode === "connect" ? "Connect" : "Save"}</button></div></form>`;
}

function providerRows(avail, connected, opts = {}) {
  const byId = Object.fromEntries(connected.map(p => [p.id, p]));
  const pill = s => `<span class="pill ${s === "OK" ? "" : s === "AUTH_REQUIRED" ? "warn" : "bad"}">`;
  return avail.map(p => {
    const c = byId[p.id];
    if (!p.installed) return `<div class="provider muted"><div><b>${esc(p.name)}</b><small>${esc(p.description)}</small></div><span class="pill idle">${esc(p.status_text)}</span></div>`;
    if (c) return `<div class="provider col"><div class="provider-head"><div><b>${esc(p.name)}</b><small>${plural(c.devices, "device")}${c.credentials.length ? " · sign-in stored encrypted" : ""}${c.detail ? " · " + esc(c.detail) : ""}</small></div>
        <div class="choice">${opts.discover !== false ? `<button class="btn" data-discover="${esc(p.id)}">Find devices</button>` : ""}${p.supports_pairing ? `<button class="btn" data-pair="${esc(p.id)}" data-pair-name="${esc(p.name)}">Add a device</button>` : ""}${pill(c.status)}${esc(c.label)}</span></div></div>
        ${opts.forms !== false ? providerForm(p, "update") : ""}</div>`;
    return `<div class="provider col"><div class="provider-head"><div><b>${esc(p.name)}</b><small>${esc(p.description)}</small></div>
        ${(p.settings.length || p.secrets.length) ? `<button class="btn" data-openform="${esc(p.id)}">Set up</button>` : `<button class="btn primary" data-connect="${esc(p.id)}">Connect</button>`}</div>
        ${S.openForm === p.id ? providerForm(p, "connect") : ""}</div>`;
  }).join("");
}

async function loadProviders() {
  const r = await api("GET", "/api/providers");
  S.providers = r;
  S.providerConfig = Object.fromEntries(r.providers.map(p => [p.id, p.settings || {}]));
}

const STEPS = ["Welcome", "Create owner", "Name home", "Network", "Services", "Find devices", "Is this yours?", "Rooms", "Favorites", "Remote access", "Backup", "Finish"];
const FIRST_WIZARD_STEP = 3;          // 0–2 happen on the create-owner screen

async function loadSetupData(i) {
  if (i === 3 || i === 11) S.system = await api("GET", "/api/system");
  if (i === 4 || i === 5) await loadProviders();
  if (i >= 5) await Promise.all([loadDevices(), loadRooms()]);
  if (i === 10) S.backups = await api("GET", "/api/backups");
}

function renderSetup() {
  const i = Math.max(S.wizard, FIRST_WIZARD_STEP), pct = Math.round((i / (STEPS.length - 1)) * 100);
  const unreviewed = S.devices.filter(d => d.review === "UNREVIEWED").length;
  const mine = S.devices.filter(d => d.review === "MINE");
  let body = "";
  if (i === 3) {
    const n = (S.system && S.system.network) || {}, v6 = n.ipv6 || {};
    const yesno = x => x === true ? '<span class="pill">Yes</span>' : x === false ? '<span class="pill warn">No</span>' : '<span class="pill idle">Unknown</span>';
    body = `<h2>Your network</h2><p>What the hub can see about itself. Nothing here is guessed.</p><div class="review-list">
      <div class="provider"><div><b>Hub name</b><small>${esc(n.hostname || "—")}</small></div></div>
      <div class="provider"><div><b>Address on your network</b><small>${n.lan_address ? esc(n.urls[0]) + " — open this from a phone or computer at home" : "The hub couldn’t find its network address"}</small></div>${n.lan_address ? '<span class="pill">Found</span>' : '<span class="pill warn">Not found</span>'}</div>
      <div class="provider"><div><b>IPv6</b><small>Matter devices need it on the home network.</small></div>${yesno(v6.enabled)}</div>
      <div class="provider"><div><b>IPv6 link-local address</b><small>How Matter talks to devices next to it.</small></div>${yesno(v6.link_local)}</div></div>`;
  } else if (i === 4) {
    body = `<h2>Services</h2><p>Connect the services your devices use. Services marked “Provider module not installed” can be added by a developer — see <span class="code-line">ADDING_A_PROVIDER.md</span>.</p>
      <div class="review-list">${providerRows(S.providers.available, S.providers.providers, { discover: false, forms: false })}</div>`;
  } else if (i === 5) {
    const connected = S.providers.providers;
    body = `<h2>Find devices</h2><p>The hub asks each connected service what it can see. Every device starts as <b>unreviewed</b> — nothing appears on your Home Hub until you say it’s yours.</p>
      <div class="review-list">${connected.map(p => `<div class="provider"><div><b>${esc(p.name)}</b><small>${plural(p.devices, "device")} found · ${esc(p.label)}</small></div><button class="btn" data-discover="${esc(p.id)}">Look again</button></div>`).join("") || '<p class="hint">No services connected yet — go back a step.</p>'}</div>
      <p>${plural(S.devices.length, "device")} known, ${unreviewed} waiting for review.</p>`;
  } else if (i === 6) {
    body = `<h2>Is this yours?</h2><p>${unreviewed ? `${plural(unreviewed, "device")} left to review.` : "All reviewed."} Neighbours’ TVs and guests’ phones can show up too — mark those “Not mine”.</p>
      ${unreviewed ? '<div class="actions start"><button class="btn" data-all="MINE">Mark the rest as mine</button></div>' : ""}
      <div class="review-list">${S.devices.map(d => reviewRow(d, false)).join("")}</div>`;
  } else if (i === 7) {
    body = `<h2>Rooms</h2><p>We used the room names your other apps already had. Change anything that’s wrong.</p>
      <div class="review-list">${mine.map(d => `<div class="review"><div><b>${esc(d.name)}</b></div>
        <select class="text-input" id="room-${d.id}" data-roomfor="${d.id}" aria-label="Room for ${esc(d.name)}"><option value="">No room</option>${S.rooms.map(r => `<option value="${r.id}" ${d.room_id === r.id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></div>`).join("") || '<p class="hint">No devices marked as yours yet.</p>'}</div>
      <h3 class="h-sub">Your rooms</h3><p class="hint">Missing a room, or a name you don’t use? Add or rename it here.</p>${roomEditor()}`;
  } else if (i === 8) {
    body = `<h2>Favorites</h2><p>Star what you use every day. Favorites sit at the top of your Home Hub.</p>
      <div class="review-list">${mine.map(d => `<div class="review"><div><b>${esc(d.name)}</b><small>${esc(d.room || "")}</small></div>
        <button class="btn" data-fav="${d.id}" aria-pressed="${d.favorite}">${d.favorite ? "★ Favorite" : "☆ Add to favorites"}</button></div>`).join("") || '<p class="hint">No devices marked as yours yet.</p>'}</div>`;
  } else if (i === 9) {
    body = `<h2>Remote access</h2>
      <div class="provider"><div><b>Off — and not part of this release</b><small>Your hub answers only on your home network and is never opened to the internet. Private remote access (Tailscale) is planned; until then, use the hub at home.</small></div><span class="pill idle">Off</span></div>`;
  } else if (i === 10) {
    const st = (S.backups && S.backups.status) || {};
    body = `<h2>Backup</h2><p>The hub backs itself up every night and checks each backup. Make the first one now.</p>
      <div class="provider"><div><b>${esc(st.label || "Not backed up yet")}</b><small>${st.at ? new Date(st.at).toLocaleString() : "Nightly at about 3:30 AM"}</small></div>
        <button class="btn primary" data-backup-now>Back up now</button></div>
      <p class="hint">Also save the encryption key somewhere safe, once: <span class="code-line">sudo /opt/hnb/bin/export-key /media/usb/hnb-vault.key</span></p>`;
  } else {
    const s = S.system || {};
    body = `<h2>You’re all set, ${esc(S.session.username)}</h2><p>${esc(S.session.house)} is ready with ${plural(mine.length, "device")} on your Home Hub${s.devices ? ` (${s.devices.UNREVIEWED || 0} still waiting for review)` : ""}.</p>`;
  }
  const last = i === STEPS.length - 1;
  return `<div class="wizard">
    <ol class="steps">${STEPS.map((s, n) => `<li class="${n < i ? "done" : n === i ? "here" : ""}"><span class="n">${n < i ? "✓" : n + 1}</span>${s}</li>`).join("")}</ol>
    <div class="card"><div class="progress" aria-hidden="true"><i data-pct="${pct}"></i></div>
      <span class="eyebrow">Step ${i + 1} of ${STEPS.length}</span>${body}
      <div class="actions">${i === FIRST_WIZARD_STEP ? "<span></span>" : '<button class="btn" data-step="-1">Back</button>'}
        ${last ? '<button class="btn primary" data-finish>Open my Home Hub</button>'
               : `<button class="btn primary" data-step="1" ${i === 6 && unreviewed ? "disabled" : ""}>Continue</button>`}</div>
    </div></div>`;
}

// ── settings: health, services, backups, activity ─────────────────────
function fmtBytes(n) { const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; }
// A requested change in words: "turn on", "brightness 40%", "cool to 21 °C" — never raw JSON.
function say(cap, v) {
  const onOff = v === true ? "turn on" : v === false ? "turn off" : String(v);
  switch (cap) {
    case "POWER": case "SWITCH": return onOff;
    case "MUTE": return v ? "mute" : "unmute";
    case "BRIGHTNESS": return `brightness ${v}%`;
    case "VOLUME": return `volume ${v}`;
    case "COLOR_TEMPERATURE": return `warmth ${v} K`;
    case "COLOR": return v && typeof v === "object" ? `colour (hue ${v.h}°, ${v.s}%)` : "colour";
    case "APP_LAUNCH": return `open ${v}`;
    case "LOCK": return v === "LOCKED" ? "lock" : v === "UNLOCKED" ? "unlock" : String(v);
    case "THERMOSTAT": return v && typeof v === "object" ? (v.mode === "OFF" ? "thermostat off" : `${(THERMO_MODES[v.mode] || v.mode).toLowerCase()} to ${v.setpoint_c} °C`) : "thermostat";
    default: return `${String(cap || "").toLowerCase().replaceAll("_", " ")} → ${typeof v === "object" ? JSON.stringify(v) : v}`;
  }
}
const AUDIT_WORDS = { COMMAND_REQUESTED: "Asked", COMMAND_CONFIRMED: "Done", COMMAND_ACCEPTED: "Sent", COMMAND_FAILED: "Failed", COMMAND_REJECTED: "Refused" };

async function renderSettings() {
  const [health, audit, backups, system] = await Promise.all([api("GET", "/api/health"), api("GET", "/api/audit?limit=60"),
    api("GET", "/api/backups"), api("GET", "/api/system"), loadProviders()]);
  const c = health.components;
  const pill = s => `<span class="pill ${s === "ok" || s === "OK" ? "" : s === "never" ? "idle" : ["warn", "stale", "degraded", "AUTH_REQUIRED", "DEGRADED"].includes(s) ? "warn" : "bad"}">`;
  const theme = document.documentElement.dataset.theme || "system";
  const names = Object.fromEntries([...S.devices.map(d => [d.id, d.name]), ...S.rooms.map(r => [r.id, r.name])]);
  const item = e => e.action === "ROOM_RENAMED" && e.data && e.data.before ? `${e.data.before} → ${e.requested}`
    : names[e.target] || (e.data && e.data.name) || (typeof e.requested === "string" ? e.requested : "");
  const errors = system.provider_load_errors || [];
  return `<div class="card"><h2>Settings</h2><p>Technical details live here, away from the Home Hub. Version ${esc(health.version)}.</p>
      <div class="actions start">${system.setup_complete ? "" : '<a class="btn primary" href="#setup">Finish setting up</a>'}<button class="btn" data-logout>Sign out</button></div></div>
    <section class="section"><div class="section-head"><h2>System health</h2>${pill(health.status)}${health.status === "ok" ? "All good" : health.status === "degraded" ? "Needs attention" : "Problem"}</span></div>
    <div class="health-grid">
      <div class="tile"><b>Hub</b>${pill("ok")}Running</span><span>Up ${Math.round(c.api.uptime_s / 60)} min · ${esc(system.network.urls[0] || system.network.hostname)}</span></div>
      <div class="tile"><b>Database</b>${pill(c.database.status)}${c.database.status === "ok" ? "Healthy" : "Problem"}</span><span>${fmtBytes(c.database.size_bytes)} · ${c.database.audit_entries} history entries · checks ${esc(c.database.checks.quick_check || "")}</span></div>
      <div class="tile"><b>Saved sign-ins</b>${pill(c.vault.status)}${c.vault.status === "ok" ? "Locked away" : "Can’t be read"}</span><span>${plural(c.vault.entries, "credential")}, encrypted${c.vault.sealed_with_other_key ? ` · ${c.vault.sealed_with_other_key} need reconnecting` : ""}</span></div>
      <div class="tile"><b>Backup</b>${pill(c.backup.status)}${esc(c.backup.label)}</span><span>${c.backup.at ? new Date(c.backup.at).toLocaleString() : "Runs nightly"}</span></div>
      <div class="tile"><b>Storage</b>${pill(c.storage.status)}${c.storage.status === "ok" ? "Plenty of room" : "Running low"}</span><span>${fmtBytes(c.storage.free_bytes)} free of ${fmtBytes(c.storage.total_bytes)}</span></div>
    </div></section>
    <section class="section"><div class="section-head"><h2>Services</h2></div>
      ${errors.map(e => `<div class="notice"><span><b>Provider module didn’t load:</b> ${esc(e.source)} — ${esc(e.reason)}</span></div>`).join("")}
      <div class="review-list">${providerRows(S.providers.available, S.providers.providers)}</div></section>
    <section class="section"><div class="section-head"><h2>Backups</h2><button class="btn" data-backup-now>Back up now</button></div>
      <div class="review-list">${backups.backups.slice(0, 6).map(b => `<div class="provider"><div><b>${esc(new Date(b.created_at).toLocaleString())}</b><small>${esc(b.file)} · ${fmtBytes(b.bytes)}</small></div><button class="btn" data-verify="${esc(b.file)}">Check</button></div>`).join("") || '<p class="hint">No backups yet.</p>'}</div>
      <p class="hint">Restoring replaces everything, so it runs on the hub: <span class="code-line">sudo /opt/hnb/bin/restore &lt;file&gt;</span></p></section>
    <section class="section"><div class="section-head"><h2>Appearance</h2></div>
      <div class="choice" role="group" aria-label="Theme">${["system", "light", "dark"].map(t => `<button data-theme-set="${t}" aria-pressed="${theme === t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div></section>
    <section class="section"><div class="section-head"><h2>Activity</h2><p>Every change, who made it, and what the device said</p></div>
      <div class="tile"><div class="table-wrap"><table><thead><tr><th>When</th><th>What</th><th>Device / item</th><th>Who</th><th>Result</th></tr></thead><tbody>
      ${audit.entries.map(e => `<tr><td class="num">${new Date(e.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" })}</td>
        <td>${esc(AUDIT_WORDS[e.action] && e.requested ? `${AUDIT_WORDS[e.action]}: ${say(e.requested.capability, e.requested.value)}` : e.action.toLowerCase().replaceAll("_", " "))}</td>
        <td>${esc(item(e))}</td><td>${esc(e.actor.replace("owner:", ""))}</td>
        <td>${e.result ? `<span class="pill ${["CONFIRMED", "OK", "VERIFIED", "MINE", "CREATED", "CONNECTED", "CHANGED", "DONE", "ADDED"].includes(e.result) ? "" : ["FAILED", "BAD_SETUP_CODE", "DOWN"].includes(e.result) ? "bad" : "idle"}">${esc(e.result.toLowerCase().replaceAll("_", " "))}</span>` : ""}</td></tr>`).join("")}
      </tbody></table></div></div></section>`;
}

// ── routing / render ─────────────────────────────────────────────────
const ROUTES = ["home", "rooms", "devices", "settings", "setup"];
const route = () => { const r = (location.hash || "#home").slice(1); return ROUTES.includes(r) ? r : "home"; };

async function render() {
  const view = $("#view"), nav = $("#nav");
  if (!S.session) return;
  if (S.session.setup_required) { nav.hidden = true; view.innerHTML = renderBootstrap(); return; }
  if (!S.session.authenticated) { nav.hidden = true; view.innerHTML = renderLogin(); return; }
  nav.hidden = false;
  const r = route();
  document.querySelectorAll(".nav a").forEach(a => a.setAttribute("aria-current", a.dataset.route === r || (r === "setup" && a.dataset.route === "devices") ? "page" : "false"));
  const focusId = document.activeElement && document.activeElement.id;
  try {
    // Home and Rooms always show the server's current view — never a copy
    // cached from before an outage ended (or began).
    if (!S.home || r === "home" || r === "rooms") await loadHome();
    if (["devices", "setup", "settings", "rooms"].includes(r)) await Promise.all([loadDevices(), loadRooms()]);
    if (r === "setup") await loadSetupData(Math.max(S.wizard, FIRST_WIZARD_STEP));
    view.innerHTML = r === "rooms" ? renderRooms() : r === "devices" ? renderDevices() : r === "setup" ? renderSetup()
      : r === "settings" ? await renderSettings() : renderHome();
  } catch (e) {
    if (e.status !== 401) view.innerHTML = `<div class="card"><h2>Couldn’t load your home</h2><p>${esc(e.message)}</p><div class="actions"><span></span><button class="btn" data-retry>Try again</button></div></div>`;
  }
  view.querySelectorAll("[data-pct]").forEach(el => { el.style.width = el.dataset.pct + "%"; });
  if (focusId && document.getElementById(focusId)) document.getElementById(focusId).focus();
  const sh = $("#sheet");
  if (sh.open && sh.dataset.id && devById(sh.dataset.id)) openSheet(sh.dataset.id);
}

async function refresh() {
  const r = route();
  try {
    if (!["home", "rooms"].includes(r)) await loadHome();      // Home and Rooms: render() fetches it — once
    if (["devices", "setup"].includes(r)) await loadDevices();
  } catch { /* render shows the error */ }
  await render();
}

// ── events ───────────────────────────────────────────────────────────
// ── adding a device (any provider that supports pairing) ────────────
function openPairSheet(pid, name) {
  const sh = $("#sheet");
  sh.dataset.id = "";
  sh.innerHTML = `<form class="sheet-body" id="pair-form" data-provider="${esc(pid)}" novalidate>
    <div class="sheet-head"><div><h3 id="sheet-title">Add a ${esc(name)} device</h3><div class="tile-room">It will appear under Devices for you to review.</div></div><button class="btn" type="button" data-close>Close</button></div>
    ${pid === "matter" ? `<div class="hint"><b>Sharing a device you control with Alexa:</b>
      <ol class="steps-list"><li>Open the Alexa app → <b>Devices</b> → pick the device.</li>
      <li>Tap the gear → <b>Other Assistants and Apps</b> → <b>Add Another</b>.</li>
      <li>Type the 11-digit code below within 15 minutes. Alexa keeps working.</li></ol>
      <small>No “Other Assistants and Apps” option? The device isn’t Matter — it can’t be shared this way.</small></div>` : ""}
    <label class="field" for="pair-code"><span>Setup code</span>
      <input class="text-input" id="pair-code" inputmode="numeric" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="1234-567-8901" required>
      <small>The 11-digit code, or the text starting with MT: from a QR code.</small></label>
    <p class="form-error" id="pair-error" role="alert" hidden></p>
    <p class="hint" id="pair-busy" hidden>Adding… this can take up to two minutes. Keep this page open.</p>
    <div class="actions"><span></span><button class="btn primary" type="submit" id="pair-submit">Add device</button></div>
  </form>`;
  if (!sh.open) sh.showModal();
  $("#pair-code").focus();
}

async function submitPair(f) {
  const btn = $("#pair-submit"), err = $("#pair-error"), busy = $("#pair-busy"), input = $("#pair-code");
  err.hidden = true; busy.hidden = false; btn.disabled = true; input.disabled = true;
  try {
    const r = await api("POST", `/api/providers/${encodeURIComponent(f.dataset.provider)}/pair`, { code: input.value });
    input.value = "";
    const names = r.added.map(d => esc(d.name)).join(", ");
    f.innerHTML = `<div class="sheet-head"><div><h3 id="sheet-title">Added ${plural(r.added.length, "device")}</h3>
      <div class="tile-room">${names}</div></div><button class="btn" type="button" data-close>Close</button></div>
      <p class="hint">New devices wait for you to say they’re yours before they appear on the Home Hub.</p>
      <div class="actions"><span></span><a class="btn primary" href="#devices" data-close-go>Review now</a></div>`;
    S.home = null;
  } catch (e2) {
    err.textContent = e2.message; err.hidden = false; busy.hidden = true; btn.disabled = false; input.disabled = false;
    input.select();
  }
}

document.addEventListener("submit", async e => {
  if (e.target.id === "pair-form") { e.preventDefault(); return submitPair(e.target); }
  if (e.target.id === "room-add") {
    e.preventDefault();
    const input = $("#new-room"), name = input.value.trim();
    if (!name) { toast("Type a name for the room first."); input.focus(); return; }
    try { const r = await api("POST", "/api/rooms", { name }); toast(`${r.name} is ready`); input.value = ""; }
    catch (err) { toast(err.message); }
    await Promise.all([loadRooms(), loadHome()]); render();
    return;
  }
  if (e.target.classList.contains("prov-form")) {
    e.preventDefault();
    const f = e.target, body = { settings: {}, secrets: {} };
    f.querySelectorAll("[data-field]").forEach(el => {
      const [kind, name] = el.dataset.field.split(":");
      body[kind === "sec" ? "secrets" : "settings"][name] = el.value;
    });
    const btn = f.querySelector("button[type=submit]"); btn.disabled = true;
    try {
      const p = f.dataset.mode === "connect"
        ? await api("POST", "/api/providers", { provider_id: f.dataset.provider, ...body })
        : await api("PATCH", `/api/providers/${encodeURIComponent(f.dataset.provider)}`, body);
      toast(`${p.name}: ${p.label}`); S.openForm = null; S.home = null;
    } catch (err) { toast(err.message); }
    f.querySelectorAll('input[type=password]').forEach(el => { el.value = ""; });
    return render();
  }
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    if (f.id === "bootstrap-form") {
      const p1 = $("#bs-pass").value, p2 = $("#bs-pass2").value;
      if (p1 !== p2) throw new Error("The two passphrases don’t match.");
      const r = await api("POST", "/api/auth/bootstrap", { setup_code: $("#bs-code").value, house_name: $("#bs-house").value, username: $("#bs-user").value, passphrase: p1 });
      await loadSession(); S.session.csrf_token = r.csrf_token;
      S.wizard = FIRST_WIZARD_STEP; location.hash = "#setup"; await refresh();
    } else if (f.id === "login-form") {
      const r = await api("POST", "/api/auth/login", { username: $("#li-user").value, passphrase: $("#li-pass").value });
      await loadSession(); S.session.csrf_token = r.csrf_token;
      S.home = null; await render();
    }
  } catch (err) {
    const which = f.id === "bootstrap-form" ? renderBootstrap : renderLogin;
    const keep = [...f.querySelectorAll("input")].map(i => [i.id, i.type === "password" ? "" : i.value]);
    $("#view").innerHTML = which(err.message);
    keep.forEach(([id, v]) => { const el = document.getElementById(id); if (el && v) el.value = v; });
  }
});

document.addEventListener("click", async e => {
  const t = e.target.closest("button, a"); if (!t) return;
  const d = t.dataset;
  if (d.act === "power") { const dev = devById(d.id); act(d.id, d.cap, !(dev.on === true)); }
  else if (d.act === "mute") { const m = ctl(devById(d.id), "MUTE"); act(d.id, "MUTE", !(m && m.value)); }
  else if (d.act === "app") { act(d.id, "APP_LAUNCH", d.app); toast(`Opening ${d.app}`); }
  else if (d.act === "color") { act(d.id, "COLOR", { h: +d.h, s: +d.s }); }
  else if (d.act === "thermo" || d.act === "thermomode") {
    const th = ctl(devById(d.id), "THERMOSTAT"), v = th.value;
    const lo = th.options.min_c ?? 7, hi = th.options.max_c ?? 32;
    act(d.id, "THERMOSTAT", d.act === "thermo"
      ? { mode: v.mode, setpoint_c: Math.min(hi, Math.max(lo, v.setpoint_c + +d.d)) }
      : { mode: d.mode, setpoint_c: v.setpoint_c });
  }
  else if ("editrooms" in d) { S.editRooms = !S.editRooms; S.confirmDel = null; render(); }
  else if (d.delroom) {
    if (S.confirmDel !== d.delroom) { S.confirmDel = d.delroom; render(); return; }
    S.confirmDel = null;
    try { await api("DELETE", `/api/rooms/${encodeURIComponent(d.delroom)}`); toast("Room deleted — its devices are kept, without a room"); }
    catch (err) { toast(err.message); }
    await Promise.all([loadRooms(), loadDevices(), loadHome()]); render();
  }
  else if (d.lockask) { openLockSheet(d.lockask, d.v); }
  else if (d.lockgo) { $("#sheet").close(); act(d.lockgo, "LOCK", d.v, true); }
  else if (d.open) { $("#sheet").dataset.id = d.open; openSheet(d.open); }
  else if ("close" in d) { $("#sheet").close(); }
  else if ("room" in d) { S.room = d.room || null; if (d.go) location.hash = "#home"; render(); }
  else if (d.rev) {
    try { await api("POST", `/api/devices/${encodeURIComponent(d.rev)}/review`, { status: d.v }); } catch (err) { toast(err.message); }
    await Promise.all([loadDevices(), loadHome()]); render();
  }
  else if (d.all) {
    for (const dev of S.devices.filter(x => x.review === "UNREVIEWED")) {
      try { await api("POST", `/api/devices/${encodeURIComponent(dev.id)}/review`, { status: d.all }); } catch (err) { toast(err.message); break; }
    }
    await Promise.all([loadDevices(), loadHome()]); render();
  }
  else if (d.fav) {
    const dev = S.devices.find(x => x.id === d.fav);
    try { await api("PATCH", `/api/devices/${encodeURIComponent(d.fav)}`, { favorite: !dev.favorite }); } catch (err) { toast(err.message); }
    await Promise.all([loadDevices(), loadHome()]); render();
  }
  else if (d.step) {
    S.wizard = Math.min(STEPS.length - 1, Math.max(FIRST_WIZARD_STEP, Math.max(S.wizard, FIRST_WIZARD_STEP) + +d.step));
    render();
  }
  else if ("finish" in d) {
    try { await api("POST", "/api/setup/complete"); } catch (err) { toast(err.message); }
    S.session.setup_complete = true; S.home = null; location.hash = "#home"; toast("Setup complete");
  }
  else if (d.discover) {
    t.disabled = true;
    try { const r = await api("POST", `/api/providers/${encodeURIComponent(d.discover)}/discover`); toast(`Found ${plural(r.found, "device")}${r.new.length ? `, ${r.new.length} new — review them` : ""}`); }
    catch (err) { toast(err.message); }
    S.home = null; render();
  }
  else if ("backupNow" in d) {
    t.disabled = true; t.textContent = "Backing up…";
    try { const r = await api("POST", "/api/backups"); toast(r.verified ? "Backed up and verified" : "Backup made"); }
    catch (err) { toast(err.message); }
    render();
  }
  else if (d.verify) {
    try { const r = await api("POST", `/api/backups/${encodeURIComponent(d.verify)}/verify`); toast(r.checks.integrity_check === "ok" ? "Backup checked: good" : "Backup has problems"); }
    catch (err) { toast(err.message); }
  }
  else if (d.openform) { S.openForm = S.openForm === d.openform ? null : d.openform; render(); }
  else if (d.pair) { openPairSheet(d.pair, d.pairName); }
  else if ("closeGo" in d) { $("#sheet").close(); }
  else if (d.connect) {
    t.disabled = true;
    try { const p = await api("POST", "/api/providers", { provider_id: d.connect }); toast(`${p.name} connected: ${p.label}`); }
    catch (err) { toast(err.message); }
    S.home = null; render();
  }
  else if ("logout" in d) { try { await api("POST", "/api/auth/logout"); } catch { /* already signed out */ } S.home = null; await loadSession(); render(); }
  else if ("retry" in d) { S.home = null; render(); }
  else if (d.themeSet) {
    if (d.themeSet === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = d.themeSet;
    try { localStorage.setItem("hnb-theme", d.themeSet); } catch { /* private mode */ }
    render();
  }
});

document.addEventListener("change", async e => {
  const t = e.target, d = t.dataset;
  if (d.act === "volume") act(d.id, "VOLUME", +t.value);
  else if (d.act === "brightness") act(d.id, "BRIGHTNESS", +t.value);
  else if (d.act === "ct") act(d.id, "COLOR_TEMPERATURE", +t.value);
  else if (d.rename) {
    const room = S.rooms.find(r => r.id === d.rename), name = t.value.trim();
    if (!room || name === room.name) return;
    if (!name) { t.value = room.name; toast("A room needs a name."); return; }
    try { await api("PATCH", `/api/rooms/${encodeURIComponent(d.rename)}`, { name }); toast(`Renamed to ${name}`); }
    catch (err) { t.value = room.name; toast(err.message); }
    await Promise.all([loadRooms(), loadDevices(), loadHome()]); render();
  }
  else if (d.roomfor) {
    try { await api("PATCH", `/api/devices/${encodeURIComponent(d.roomfor)}`, t.value ? { room_id: t.value } : { clear_room: true }); toast("Room saved"); }
    catch (err) { toast(err.message); }
    await Promise.all([loadDevices(), loadHome()]); render();
  }
});
document.addEventListener("input", e => {
  const t = e.target;
  if (t.dataset.act === "brightness") $("#brv").textContent = t.value + "%";
  if (t.dataset.act === "ct") $("#ctv").textContent = t.value + " K";
});
$("#sheet").addEventListener("close", () => { delete $("#sheet").dataset.id; });
window.addEventListener("hashchange", () => { if (route() === "setup") S.wizard = S.wizard || 0; render(); });

// Keep the Hub current: the poller updates the server every ~30 s.
setInterval(() => {
  const c = $("#clock-time"); if (c) c.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}, 15000);
setInterval(() => { if (document.visibilityState === "visible" && S.session && S.session.authenticated && route() === "home" && !$("#sheet").open) refresh(); }, 20000);

try { const th = localStorage.getItem("hnb-theme"); if (th && th !== "system") document.documentElement.dataset.theme = th; } catch { /* private mode */ }

loadSession().then(render).catch(err => {
  $("#view").innerHTML = `<div class="card"><h2>Can’t reach the hub</h2><p>${esc(err.message)}</p></div>`;
});
})();
