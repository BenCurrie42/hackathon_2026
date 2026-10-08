/* Holy Sound front end. No framework, no build step: this file is served as-is. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const POLL_MS = 1500;
const POLL_PLAYING_MS = 600;   // the playhead and song states move while a song plays
const METER_GAP_MS = 30;       // between meter reads; Live itself samples meters about every 100 ms
const METER_FALL_PER_S = 1.6;  // how fast a meter drops after a peak, in meter-heights per second
const POLL_APPLYING_MS = 250;  // step states move while changes are being applied
const POLL_HIDDEN_MS = 5000;
const MINUS = "−";

let state = null;
let sending = null;           // text of the message in flight, shown optimistically
let sendingFrom = 0;          // how many chat entries there were when it was sent
let chatSig = "";
let songsSig = "";
let stockDevices = null;
let applyingId = null;        // the proposal whose Apply was just pressed
const exportNotes = new Map(); // proposal id -> what the saved file could not include
const strips = new Map();     // "t0" / "r1" -> strip element
const openThoughts = new Set(); // ids of chat entries whose thinking is expanded
const holding = new WeakSet(); // controls the user is touching: polling won't move them
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

// -- small helpers ----------------------------------------------------------

function el(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, className, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* The decimal point in a monospace face takes a whole cell, so wrap it and tighten it. */
function numHtml(text) {
  return escapeHtml(text).replace(".", '<i class="pt">.</i>');
}

/* Hyphen-minus is for hyphens. Numbers get a true minus. */
function trueMinus(text) {
  return text.replace(/(^|[\s(“])-(?=\d)/g, `$1${MINUS}`);
}

const ARROW_SVG = '<svg viewBox="0 0 14 9" aria-hidden="true"><path d="M0 4.5h12M8.5 1l3.5 3.5L8.5 8"/></svg>';

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function nextSundayLabel() {
  const d = new Date();
  const ahead = (7 - d.getDay()) % 7;
  if (ahead === 0) return "Today";
  d.setDate(d.getDate() + ahead);
  return `Sunday ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

function shortDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return iso || "";
  const month = MONTHS[parseInt(m[2], 10) - 1];
  const day = parseInt(m[3], 10);
  return parseInt(m[1], 10) === new Date().getFullYear() ? `${day} ${month}` : `${day} ${month} ${m[1]}`;
}

/* One sentence for screen readers, through the page's polite live region. */
function say(text) {
  const node = $("#sr-status");
  node.textContent = "";
  setTimeout(() => { node.textContent = text; }, 50);
}

// -- API --------------------------------------------------------------------

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Can't reach Holy Sound. Is it still running on the computer?");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Something went wrong. Try again.");
  return data;
}

async function liveCmd(cmd, args = {}) {
  try {
    const data = await api("/api/live", { cmd, args });
    if (state && data.live) {
      state.live = data.live;
      renderLive();
    }
    return data.result;
  } catch (e) {
    toast(e.message, "error");
    throw e;
  }
}

// -- motion -------------------------------------------------------------------
//
// Nothing the volunteer didn't move themselves jumps: a value Live or the assistant
// changed glides to where it is now, new things slide in, and things that change place
// travel there. Whatever the volunteer is holding follows their hand with no delay.
// With reduced motion asked for, everything lands at once.

const EASE = "cubic-bezier(0.22, 1, 0.36, 1)";
const GLIDE_MS = 420;
const motion = () => !reducedMotion.matches;
const tweens = new WeakMap(); // owner -> {key: animation frame id}

/* Move a number from one value to another over ms, calling apply(value, finished) each frame. */
function tween(owner, key, from, to, apply, ms = GLIDE_MS) {
  let running = tweens.get(owner);
  if (!running) tweens.set(owner, running = {});
  cancelAnimationFrame(running[key]);
  delete running[key];
  if (!motion() || !Number.isFinite(from) || !Number.isFinite(to) || from === to) {
    apply(to, true);
    return;
  }
  const start = performance.now();
  const frame = (now) => {
    const k = Math.min(1, (now - start) / ms);
    const eased = 1 - Math.pow(1 - k, 3);
    apply(from + (to - from) * eased, k === 1);
    if (k < 1) running[key] = requestAnimationFrame(frame);
    else delete running[key];
  };
  running[key] = requestAnimationFrame(frame);
}

function stopTween(owner, key) {
  const running = tweens.get(owner);
  if (running && running[key] !== undefined) {
    cancelAnimationFrame(running[key]);
    delete running[key];
  }
}

/* A range input set from Live: the thumb glides there. onFrame(value) keeps a readout in step. */
function glideInput(input, to, onFrame) {
  to = Number(to);
  const from = parseFloat(input.value);
  if (input._glideTo === to && tweens.get(input)?.value !== undefined) return;   // already on its way
  input._glideTo = to;
  tween(input, "value", from, to, (v) => {
    input.value = String(v);
    onFrame?.(v);
  });
}

/* Slide a newly shown element in. */
function enter(node, delay = 0, from = "translateY(8px)") {
  if (motion() && node) {
    node.animate([{ opacity: 0, transform: from }, { opacity: 1, transform: "none" }],
      { duration: 340, delay, easing: EASE, fill: "backwards" });
  }
  return node;
}

/* Fade an element out, then take it away. */
function leave(node) {
  if (!motion() || !node.isConnected) return node.remove();
  node.animate([{ opacity: 1 }, { opacity: 0, transform: "translateY(-4px) scale(0.98)" }],
    { duration: 200, easing: "ease-in", fill: "forwards" }).onfinish = () => node.remove();
}

/* Run a change to the page, then let every keyed element that moved travel from where it was.
   key(node) names an element across redraws, so rows that are rebuilt still glide. */
function flip(container, selector, key, change) {
  const before = new Map();
  if (motion()) {
    for (const node of container.querySelectorAll(selector)) {
      if (key(node) != null) before.set(key(node), node.getBoundingClientRect());
    }
  }
  change();
  if (!before.size) return;
  for (const node of container.querySelectorAll(selector)) {
    if (key(node) == null) continue;
    const was = before.get(key(node));
    if (!was) continue;
    const now = node.getBoundingClientRect();
    const dx = was.left - now.left;
    const dy = was.top - now.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
    node.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 460, easing: EASE });
  }
}

// -- polling ------------------------------------------------------------------

let pollTimer = null;
let mixPickedAt = 0; // when the page last changed the song mix; older answers don't know about it

async function poll() {
  clearTimeout(pollTimer);
  try {
    const asked = performance.now();
    const next = await api("/api/state");
    if (asked < mixPickedAt && state) next.song_mix = state.song_mix;
    render(next);
  } catch (e) {
    setStatus("bad", e.message);
  }
  const playing = state?.live.snapshot?.song.is_playing;
  const wait = document.hidden ? POLL_HIDDEN_MS
    : applyingId !== null || state?.applying ? POLL_APPLYING_MS
    : playing ? POLL_PLAYING_MS : POLL_MS;
  pollTimer = setTimeout(poll, wait);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); pollMeters(); } });

// -- meters: their own fast feed, drawn every frame --------------------------------

let meterTimer = null;
let meterFedAt = 0;              // when the feed last brought new levels
const aimedMeters = new Set();   // meter elements with a level to draw

const meterFeedLive = () => performance.now() - meterFedAt < 1000;

async function pollMeters() {
  clearTimeout(meterTimer);
  if (document.hidden) return;
  let wait = METER_GAP_MS;
  try {
    const { meters } = await api("/api/meters");
    if (meters) {
      meterFedAt = performance.now();
      meters.tracks.forEach((peak, i) => aimMeter(strips.get("t" + i)?._r.meter, peak ?? 0));
      meters.returns.forEach((peak, i) => aimMeter(strips.get("r" + i)?._r.meter, peak ?? 0));
      aimMeter($("#master-meter"), meters.master);
    } else if (!state?.live.connected) {
      wait = POLL_MS;
    }
  } catch {
    wait = POLL_MS;
  }
  meterTimer = setTimeout(pollMeters, wait);
}

/* A meter jumps straight up to a new peak and falls back smoothly, like a hardware meter. */
function aimMeter(meter, peak) {
  if (!meter) return;
  meter._aim = Math.min(1, Math.max(0, peak));
  if (meter._shown == null || meter._aim > meter._shown) meter._shown = meter._aim;
  aimedMeters.add(meter);
}

let meterFrameAt = 0;
function drawMeters(now) {
  const dt = meterFrameAt ? Math.min(0.1, (now - meterFrameAt) / 1000) : 0;
  meterFrameAt = now;
  for (const meter of aimedMeters) {
    if (!meter.isConnected) { aimedMeters.delete(meter); continue; }
    meter._shown = Math.max(meter._aim, meter._shown - METER_FALL_PER_S * dt);
    const lit = Math.round(meter._shown * 1000) / 1000;
    if (lit !== meter._lit) {
      meter._lit = lit;
      meter.style.setProperty("--lit", String(lit));
    }
  }
  requestAnimationFrame(drawMeters);
}
requestAnimationFrame(drawMeters);

function render(next) {
  state = next;
  noteActivity(next.activity);
  renderLive();
  renderAi();
  renderChat();
  renderUsage(state.usage);
}

// -- header ------------------------------------------------------------------

function setStatus(kind, text, detail = "") {
  const node = $("#live-status");
  if (node.className !== "status " + kind) node.className = "status " + kind;
  const t = $(".status-text", node), d = $(".status-detail", node);
  if (t.textContent !== text) t.textContent = text;
  if (d.textContent !== detail) d.textContent = detail;
}

function renderLive() {
  const live = state.live;
  const snap = live.snapshot;
  if (live.connected) {
    const n = snap.tracks.length;
    const detail = `${n} track${n === 1 ? "" : "s"}`;
    // The pretend Live looks exactly like the real one, so say which this is.
    if (state.demo) setStatus("demo", "Demo set, not Ableton", detail);
    else setStatus("ok", "Ableton connected", detail);
  } else {
    setStatus("bad", "Ableton isn’t connected");
  }

  $("#transport").hidden = !live.connected;
  if (snap) {
    const play = $("#play-btn");
    play.classList.toggle("playing", snap.song.is_playing);
    play.setAttribute("aria-label", snap.song.is_playing ? "Stop" : "Play");
    const tempo = $("#tempo-input");
    if (document.activeElement !== tempo) {
      const bpm = Math.round(snap.song.tempo * 100) / 100;
      if (tempo._glideTo !== bpm) {
        tempo._glideTo = bpm;
        tween(tempo, "value", parseFloat(tempo.value), bpm, (v, done) => { tempo.value = done ? bpm : Math.round(v); });
      }
    }
    paintMeter($("#master-meter"), snap.master?.meter);
    setCount("mixer", snap.tracks.length);
    setCount("songs", snap.scenes.length);
  }
  setCount("room", (state.room || []).length);

  renderRoom();
  $("#offline").hidden = live.connected || currentTab === "room" || !!state.demo;
  // The standard "can't reach Live" message just repeats the steps below it.
  const reason = live.message || "";
  $("#offline-reason").textContent = reason.includes("Control Surface") ? "" : reason;
  $("#panel-mixer").hidden = !live.connected || currentTab !== "mixer";
  $("#panel-songs").hidden = !live.connected || currentTab !== "songs";
  $("#panel-room").hidden = currentTab !== "room";
  $("#pane-session").dataset.tab = currentTab;
  if (snap) {
    renderMixer(snap);
    renderSongs(snap);
  }
}

function setCount(which, n) {
  for (const id of [`count-${which}`, `nav-count-${which}`]) {
    const node = document.getElementById(id);
    if (node) node.textContent = String(n);
  }
}

$("#play-btn").addEventListener("click", () => {
  const playing = state?.live.snapshot?.song.is_playing;
  liveCmd(playing ? "stop" : "play").catch(() => {});
});

$("#tempo-input").addEventListener("change", (e) => {
  const bpm = parseFloat(e.target.value);
  if (bpm >= 20 && bpm <= 999) liveCmd("set_tempo", { bpm }).catch(() => {});
});

// -- chat: a log, not a messenger ----------------------------------------------

function renderAi() {
  const banner = $("#ai-banner");
  banner.hidden = state.ai.ready;
  banner.textContent = state.ai.message || "";
}

function renderUsage(usage) {
  const total = usage ? usage.input + usage.output : 0;
  $("#reset-btn").title = total
    ? `${tokens(total)} tokens used in this conversation (${tokens(usage.input)} sent, ${tokens(usage.output)} written)`
    : "";
}

function tokens(n) {
  return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
}

/* The server saves a message to the chat the moment it arrives, but only
   answers /api/chat once the whole reply is written. Until then the page shows
   its own copy of the message; once the server's copy turns up in the chat
   (a poll, or the end of the stream, can bring it in first) that copy is
   dropped so the message never shows twice. */
function pendingMessage() {
  if (sending === null) return null;
  const saved = state.chat.slice(sendingFrom).some((entry) => entry.role === "user");
  return saved ? null : sending;
}

function startSending(text) {
  sending = text;
  sendingFrom = state ? state.chat.length : 0;
}

function turn(role, text) {
  const node = el("div", "turn " + role);
  node.append(el("p", "speaker", role === "user" ? "You" : "Holy Sound"));
  const body = el("div", "turn-text");
  if (role === "assistant") body.innerHTML = markdownLite(text);
  else body.textContent = text;
  node.append(body);
  return node;
}

function eventLine(role, text) {
  const node = el("div", "event");
  node.append(el("span", "event-label", role === "note" ? "Saved" : "Heard"), el("span", "event-text", text));
  if (role === "note") {
    node.classList.add("clickable");
    node.setAttribute("role", "button");
    node.tabIndex = 0;
    node.title = "See everything Holy Sound remembers";
    node.addEventListener("click", () => setView("room"));
    node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setView("room"); } });
  }
  return node;
}

function working(label) {
  const node = el("div", "working");
  const chase = el("span", "chase");
  chase.setAttribute("aria-hidden", "true");
  chase.append(el("i"), el("i"), el("i"), el("i"));
  node.append(chase, el("span", "", label));
  return node;
}

/* A proposal that still needs a decision (or is being applied) is docked above the
   composer; everything else sits in the log in a collapsed form. */
function dockedProposal() {
  let found = null;
  for (const entry of state.chat) {
    const p = entry.proposal;
    const running = state.applying && state.applying.id === p?.id;
    if (p && (p.status === "pending" || p.id === applyingId || running)) found = p;
  }
  return found;
}

const chatSeen = new Set();   // chat entries already drawn once, so only new ones slide in
let chatShown = false;

function renderChat() {
  const pendingText = pendingMessage();
  const docked = dockedProposal();
  const sig = JSON.stringify([
    state.chat, state.busy, sending, pendingText, state.live.connected, reply !== null,
    applyingId, state.applying, !!state.demo, $("#layout").dataset.view,
  ]);
  if (sig === chatSig) return;
  chatSig = sig;

  const box = $("#messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const welcome = $("#welcome");
  box.replaceChildren(welcome);
  // each entry slides in the first time it's drawn; what was there when the page opened just shows
  let fresh = 0;
  const once = (node, key) => {
    if (!chatSeen.has(key)) {
      chatSeen.add(key);
      if (chatShown) enter(node, 60 * fresh++);
    }
    return node;
  };
  const wasSending = chatSeen.has("sending");
  welcome.hidden = state.chat.length > 0 || sending !== null;
  if (!welcome.hidden) paintWelcome();

  for (const entry of state.chat) {
    if (entry.role === "note" || entry.role === "heard") {
      box.append(once(eventLine(entry.role, entry.text || ""), `e${entry.id}`));
      continue;
    }
    if (entry.text || entry.thinking) {
      const node = turn(entry.role, entry.text || "");
      if (entry.thinking) node.insertBefore(thoughts(entry.thinking, entry.id), $(".turn-text", node));
      // the volunteer's own message already slid in while it was being sent
      if (entry.role === "user" && wasSending) chatSeen.add(`t${entry.id}`);
      box.append(once(node, `t${entry.id}`));
    }
    if (entry.proposal && entry.proposal !== docked && entry.proposal.id !== docked?.id) {
      box.append(once(slipEl(entry.proposal, false), `p${entry.proposal.id}:${entry.proposal.status}`));
    }
  }
  if (pendingText !== null) {
    const node = turn("user", pendingText);
    node.classList.add("pending");
    box.append(once(node, "sending"));
  } else chatSeen.delete("sending");
  if (reply) {
    box.append(once(reply.el, "reply"));
  } else if (sending !== null || state.busy) {
    box.append(once(working("Working…"), "working"));
  }
  if (!reply) chatSeen.delete("reply");
  if (!(sending !== null || state.busy) || reply) chatSeen.delete("working");
  const dock = $("#slip-dock");
  const keepSteps = docked && dock._lastSlip === docked.id ? $(".steps", dock)?.scrollTop : null;
  dock.replaceChildren(...(docked ? [slipEl(docked, true)] : []));
  if (docked) {
    const steps = $(".steps", dock);
    if (steps && dock._lastSlip !== docked.id) {
      // the assistant's list rises into place, a step at a time
      enter(dock.firstElementChild, 0, "translateY(24px)");
      $$(".step", steps).forEach((row, i) => enter(row, 120 + i * 45, "translateX(-6px)"));
      steps.scrollTop = 0;
      const n = docked.steps.length;
      say(`Holy Sound suggests ${n} change${n === 1 ? "" : "s"}. Press Apply or Not now.`);
    } else if (steps && keepSteps !== null) {
      steps.scrollTop = keepSteps;   // the list stays where the volunteer left it while steps report
      $(".step.is-working", steps)?.scrollIntoView({ block: "nearest" });
    }
    dock._lastSlip = docked.id;
  }

  chatShown = true;
  if (!welcome.hidden) box.scrollTop = 0;
  else if (nearBottom || sending !== null || !box._shown) box.scrollTop = box.scrollHeight;
  box._shown = true;
  // the badge on the phone's Chat item while a slip waits
  const mark = $("#nav-chat-mark");
  const waiting = docked && docked.status === "pending" && $("#layout").dataset.view !== "chat";
  mark.hidden = !waiting;
  const chatNav = $(".bottom-nav [data-view=chat]");
  if (waiting) { mark.textContent = String(docked.steps.length); chatNav.setAttribute("aria-label", `Chat, ${docked.steps.length} change${docked.steps.length === 1 ? "" : "s"} waiting`); }
  else chatNav.removeAttribute("aria-label");

  $("#composer .send").disabled = sending !== null || state.busy;
  $("#import-btn").disabled = sending !== null || state.busy;
  $("#reset-btn").hidden = state.chat.length === 0 && sending === null;
  $(".chat-head").hidden = $("#reset-btn").hidden;
}

function paintWelcome() {
  $("#welcome-date").textContent = nextSundayLabel();
  const n = state.live.snapshot?.tracks.length || 0;
  $("#welcome-line").textContent = n
    ? `Your set has ${n} channel${n === 1 ? "" : "s"}. Tell me what is different this Sunday.`
    : "Nothing is set up yet. Say what is plugged in and which songs you are playing.";
  const facts = [...(state.room || [])].sort((a, b) => b.id - a.id).slice(0, 2);
  const line = $("#welcome-saved");
  line.hidden = facts.length === 0;
  if (facts.length) $(".welcome-saved-text", line).textContent = "Saved in Room: " + facts.map((f) => f.text).join(" ");
}

// Collapsed by default: a volunteer wants the answer, the reasoning is there if they're curious.
function thoughts(text, id) {
  const node = el("details", "thoughts");
  node.innerHTML = '<summary>Show how I worked it out</summary><div class="thought-text"></div>';
  $(".thought-text", node).textContent = text;
  const summary = $("summary", node);
  if (id !== undefined) node.open = openThoughts.has(id);
  summary.textContent = node.open ? "Hide how I worked it out" : "Show how I worked it out";
  node.addEventListener("toggle", () => {
    summary.textContent = node.open ? "Hide how I worked it out" : "Show how I worked it out";
    if (id !== undefined) node.open ? openThoughts.add(id) : openThoughts.delete(id);
  });
  return node;
}

// -- the reply being written -------------------------------------------------
// /api/events streams the assistant's reply as it's written. The finished turn
// arrives through /api/state as usual, so on "end" this entry just goes away.

const TOOL_LABELS = {
  listen_to_stems: "Listening to the song files…",
  propose_changes: "Writing up the changes…",
  remember: "Saving that for next week…",
};

let reply = null;  // { el, thinking, text, tool } while a reply is streaming

function startReply() {
  const node = el("div", "turn assistant streaming");
  node.append(el("p", "speaker", "Holy Sound"));
  const think = thoughts("");
  const text = el("div", "turn-text reply-text");
  const status = el("div", "reply-status");
  node.append(think, text, status);
  // Opening the thinking while it's written jumps to its newest line; paintReply keeps it there.
  think.addEventListener("toggle", (e) => {
    if (!e.target.open) return;
    const t = $(".thought-text", e.target);
    t.scrollTop = t.scrollHeight;
    e.target.scrollIntoView({ block: "nearest" });
  });
  reply = { el: node, thinking: "", text: "", tool: null };
  chatSig = "";
  if (state) renderChat();
  paintReply();
}

let paintQueued = false;
function paintReply() {
  if (paintQueued) return;
  paintQueued = true;
  requestAnimationFrame(() => {
    paintQueued = false;
    if (!reply) return;
    const think = $(".thoughts", reply.el);
    const text = $(".reply-text", reply.el);
    const status = $(".reply-status", reply.el);
    think.hidden = !reply.thinking;
    const summary = $("summary", think);
    if (!reply.text && !reply.tool) summary.textContent = "Working it out…";
    else summary.textContent = think.open ? "Hide how I worked it out" : "Show how I worked it out";
    // Follow the thinking as it's written, unless the volunteer scrolled up in it to read.
    const thought = $(".thought-text", think);
    const following = thought.scrollHeight - thought.scrollTop - thought.clientHeight < 24;
    thought.textContent = reply.thinking;
    if (following) thought.scrollTop = thought.scrollHeight;
    text.innerHTML = markdownLite(reply.text);
    text.hidden = !reply.text;
    status.replaceChildren();
    if (reply.tool) status.append(working(TOOL_LABELS[reply.tool] || "Working…"));
    else if (!reply.text && !reply.thinking) status.append(working("Working…"));
    status.hidden = !status.hasChildNodes();
    const box = $("#messages");
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
  });
}

function listen() {
  const events = new EventSource("/api/events");
  const on = (kind, fn) => events.addEventListener(kind, (e) => { fn(JSON.parse(e.data)); paintReply(); });
  on("start", startReply);
  on("step", () => {
    if (!reply) startReply();
    if (reply.text && !reply.text.endsWith("\n\n")) reply.text += "\n\n";
    reply.tool = null;
  });
  on("thinking", (t) => { if (reply) reply.thinking += t; });
  on("text", (t) => { if (reply) reply.text += t; });
  on("tool", (name) => { if (reply) reply.tool = name; });
  on("usage", renderUsage);
  on("end", () => {
    reply = null;
    chatSig = "";
    poll();
  });
}

/* Enough Markdown for chat replies: paragraphs, bullet lists, **bold**. */
function markdownLite(text) {
  const inline = (s) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  return text.split(/\n{2,}/).map((block) => {
    const lines = block.split("\n");
    if (lines.every((l) => /^\s*([-*•]|\d+\.)\s+/.test(l))) {
      return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*([-*•]|\d+\.)\s+/, ""))}</li>`).join("") + "</ul>";
    }
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
}

// -- the change slip ----------------------------------------------------------
// Paper means a person has to decide. A slip waiting for a decision is paper;
// once Apply is pressed it goes dark and each step reports as it runs.

function consequence(step) {
  if (step.destructive) {
    if (/^Delete the track/i.test(step.text)) return { text: "Removes this track from your set.", kind: "remove" };
    if (/^Delete the song/i.test(step.text)) return { text: "Removes this song from your set.", kind: "remove" };
    return { text: "Removes this effect from the track.", kind: "remove" };
  }
  if (step.audible) return { text: "You will hear this in the room.", kind: "audible" };
  return null;
}

const stepStates = new Map();   // "proposal:step" -> the state it was last drawn in

function stepRow(step, i, mode, p) {
  const li = el("li", "step");
  li.append(el("span", "step-num", String(i + 1)));
  const body = el("div", "step-body");
  body.append(el("p", "step-text", trueMinus(step.text)));
  const note = consequence(step);
  if (note && mode === "pending") body.append(el("p", "step-note " + note.kind, note.text));
  li.append(body);

  if (mode !== "done") return li;   // "pending" and "plain" rows carry no state

  // applying or finished: the state of this step, in words and a lamp
  const result = p.results?.[i];
  let stateName;
  let reason = "";
  if (result) {
    stateName = !result.ok ? "failed" : result.partial ? "check" : "done";
    if (stateName === "failed") reason = result.text;
    else if (stateName === "check") reason = result.text.replace(/^.*? But /, "But ");
  } else {
    const live = state.applying && state.applying.id === p.id ? state.applying.states[i] : null;
    stateName = live || "waiting";
  }
  const words = { waiting: "Waiting", working: "Working", done: "Done", check: "Check", failed: "Failed" };
  const status = el("span", "step-state " + stateName);
  const stepKey = `${p.id}:${i}`;
  const was = stepStates.get(stepKey);
  stepStates.set(stepKey, stateName);
  if (was && was !== stateName) {
    status.classList.add("just-changed");
    if (stateName !== "working") enter(status, 0, "scale(0.85)");
  }
  if (stateName !== "waiting") {
    const lamp = el("i", "lamp");
    lamp.style.background = { working: "var(--text)", done: "var(--green)", check: "var(--amber)", failed: "var(--red)" }[stateName];
    status.append(lamp);
  }
  status.append(document.createTextNode(words[stateName]));
  li.append(status);
  if (stateName === "working") li.classList.add("is-working");
  if (reason) li.append(el("p", "step-reason", trueMinus(reason)));
  return li;
}

function slipEl(p, docked) {
  const total = p.steps.length;
  const noun = `${total} change${total === 1 ? "" : "s"}`;

  // one line, with the steps a tap away
  const collapsedWords = {
    dismissed: `${noun}, not applied`,
    superseded: "Replaced by a newer list",
    exported: "Saved as an Ableton file",
  }[p.status];
  if (collapsedWords) {
    const node = el("div", "slip collapsed");
    const line = el("div", "slip-collapsed");
    line.append(el("span", "", collapsedWords));
    const list = el("ol", "steps");
    list.hidden = true;
    p.steps.forEach((step, i) => list.append(stepRow(step, i, "plain", p)));
    const toggle = button("Show steps", "text-btn", () => {
      list.hidden = !list.hidden;
      toggle.textContent = list.hidden ? "Show steps" : "Hide steps";
    });
    line.append(toggle);
    node.append(line, list);
    const notes = exportNotes.get(p.id);
    if (p.status === "exported" && notes?.length) node.append(el("p", "slip-notes", "Not in the file: " + notes.join(" ")));
    return node;
  }

  const applying = p.status === "pending" && (p.id === applyingId || (state.applying && state.applying.id === p.id));
  const finished = p.status === "applied";
  const node = el("div", "slip " + (p.status === "pending" && !applying ? "pending paper" : applying ? "applying" : "done-state"));
  const head = el("div", "slip-head");

  let headline;
  if (applying) {
    headline = `Applying ${noun}`;
    head.append(Object.assign(el("i", "lamp"), {}));
    head.firstChild.style.background = "var(--amber)";
  } else if (finished) {
    const failed = (p.results || []).filter((r) => !r.ok).length;
    const check = (p.results || []).filter((r) => r.ok && r.partial).length;
    const lamp = el("i", "lamp");
    if (failed === total) { headline = "Nothing was applied"; lamp.style.background = "var(--red)"; }
    else if (failed || check) { headline = `${total - failed - check} applied, ${failed + check} need${failed + check === 1 ? "s" : ""} a look`; lamp.style.background = "var(--amber)"; }
    else { headline = `${noun} applied`; lamp.style.background = "var(--green)"; }
    head.append(lamp);
  } else {
    headline = `${noun}, not applied yet`;
  }
  head.append(el("span", "", headline));
  head.tabIndex = -1;
  node.append(head);

  const list = el("ol", "steps");
  const mode = p.status === "pending" && !applying ? "pending" : "done";
  p.steps.forEach((step, i) => list.append(stepRow(step, i, mode, p)));
  node.append(list);

  if (mode === "done" && total > 4 && !docked) {
    const spare = [...list.children].slice(4).filter((r) => !r.querySelector(".step-state.failed, .step-state.check"));
    if (spare.length) {
      spare.forEach((r) => { r.hidden = true; });
      const more = button("Show all", "text-btn", () => {
        const hidden = spare[0].hidden;
        spare.forEach((r) => { r.hidden = !hidden; });
        more.textContent = hidden ? "Show fewer" : "Show all";
      });
      node.append(more);
    }
  }

  if (p.status === "pending" && !applying) {
    const connected = state.live.connected;
    const actions = el("div", "slip-actions");
    if (connected) {
      const apply = button(`Apply ${noun}`, "key big on-paper", () => act(p.id, "apply", apply));
      actions.append(apply);
    } else {
      if (p.exportable) {
        const dl = button("Save as an Ableton file", "key big on-paper", () => exportProposal(p.id, dl));
        actions.append(dl);
      }
      actions.append(el("p", "slip-line disabled", "Apply (Ableton isn’t open)"));
      actions.append(el("p", "slip-note", "Ableton isn’t open, so Apply is off." +
        (p.exportable ? " Save the new tracks as an Ableton file instead." : "")));
    }
    const sub = el("div", "sub-row");
    sub.append(el("span"));
    const later = button("Not now", "text-btn", () => act(p.id, "dismiss", later));
    sub.append(later);
    actions.append(sub);
    node.append(actions);
  }
  return node;
}

async function act(id, verb, btn) {
  btn.disabled = true;
  if (verb === "apply") {
    applyingId = id;   // the slip goes dark at once; the steps report as the server runs them
    chatSig = "";
    renderChat();
    poll();
  }
  try {
    render(await api(`/api/proposals/${id}/${verb}`, {}));
  } catch (e) {
    toast(e.message, "error");
    btn.disabled = false;
  } finally {
    if (verb === "apply") {
      applyingId = null;
      chatSig = "";
      if (state) renderChat();
      // read the outcome next, and leave the keyboard on it
      const done = [...document.querySelectorAll("#messages .slip-head")].pop();
      if (done) { done.focus({ preventScroll: true }); say(done.textContent); }
    } else {
      $("#message-input").focus();
    }
    poll();
  }
}

async function exportProposal(id, btn) {
  btn.disabled = true;
  try {
    const response = await fetch(`/api/proposals/${id}/export`);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Couldn't build the session file.");
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "Holy Sound.als";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    const { notes } = await api(`/api/proposals/export-notes?id=${id}`);
    exportNotes.set(id, notes);
    chatSig = "";
    toast("Saved. Double-click the file to open it in Ableton." + (notes.length ? " Some steps could not go in the file; they are listed under the slip." : ""));
    poll();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

async function send(text) {
  text = text.trim();
  if (!text || sending !== null) return;
  startSending(text);
  $("#message-input").value = "";
  autosize();
  renderChat();
  try {
    const next = await api("/api/chat", { message: text });
    sending = null;
    render(next);
  } catch (e) {
    sending = null;
    toast(e.message, "error");
    const input = $("#message-input");
    if (!input.value) input.value = text;
    autosize();
    chatSig = "";
    if (state) renderChat();
  }
}

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  send($("#message-input").value);
});

const coarse = matchMedia("(pointer: coarse)").matches;
$("#message-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !coarse && !e.isComposing) {
    e.preventDefault();
    send(e.target.value);
  }
});

function autosize() {
  const input = $("#message-input");
  input.style.height = "auto";
  input.style.height = Math.min(Math.max(input.scrollHeight, 76), 180) + "px";
}
$("#message-input").addEventListener("input", autosize);

$("#chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".starter");
  if (!chip) return;
  if (chip.dataset.action === "import") openImportDialog();
  else send(chip.textContent);
});

$("#open-room").addEventListener("click", () => setView("room"));

/* A yes-or-no question in the app's own sheet (the browser's box says "localhost says"). */
function ask(title, copy, yes) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-copy").textContent = copy;
  $("#confirm-ok").textContent = yes;
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
  });
}

$("#reset-btn").addEventListener("click", async () => {
  if (!await ask("Start over?", "The chat is cleared. Your Ableton set stays as it is.", "Start over")) return;
  try {
    render(await api("/api/reset", {}));
  } catch (e) {
    toast(e.message, "error");
  }
});

// -- fader law ----------------------------------------------------------------
// The cap, the dB rail and the 0 dB line all use this one table, so they agree.
// It only decides where the cap sits. The server still sets level in dB.

const LAW = [[-70, 0], [-40, 0.16], [-30, 0.28], [-20, 0.42], [-10, 0.6], [0, 0.8], [6, 1]];

function dbToPos(db) {
  if (db === null || db === undefined || db <= -70) return 0;
  if (db >= 6) return 1;
  for (let i = 1; i < LAW.length; i++) {
    if (db <= LAW[i][0]) {
      const [d0, p0] = LAW[i - 1];
      const [d1, p1] = LAW[i];
      return p0 + ((db - d0) / (d1 - d0)) * (p1 - p0);
    }
  }
  return 1;
}

function posToDb(pos, detent = true) {
  if (pos < 0.012) return -70;
  if (pos >= 1) return 6;
  for (let i = 1; i < LAW.length; i++) {
    if (pos <= LAW[i][1]) {
      const [d0, p0] = LAW[i - 1];
      const [d1, p1] = LAW[i];
      const db = d0 + ((pos - p0) / (p1 - p0)) * (d1 - d0);
      const half = Math.round(db * 2) / 2;
      return detent && Math.abs(half) <= 1 ? 0 : half;   // a detent at unity, for the hand not the keyboard
    }
  }
  return 6;
}

function percent(db) {
  return Math.round(dbToPos(db) * 100);
}

function sendText(db) {
  return db === null || db === undefined || db <= -70 ? "Off" : `${percent(db)}%`;
}

function dbText(db) {
  return db <= -70 ? "Off" : `${db > 0 ? "+" : db < 0 ? MINUS : ""}${Math.abs(db).toFixed(1)} dB`;
}

function panText(pan) {
  const amount = Math.round(Math.abs(pan) * 100);
  return amount === 0 ? "Centre" : `${amount}% ${pan < 0 ? "left" : "right"}`;
}

function panFromText(text) {
  const m = /^([LR])(\d+)$/.exec(text || "") || /^(\d+)([LR])$/.exec(text || "");
  if (!m) return 0;
  const letter = /[LR]/.test(m[1]) ? m[1] : m[2];
  const amount = parseInt(/\d/.test(m[1]) ? m[1] : m[2], 10);
  return (amount / 50) * (letter === "L" ? -1 : 1);
}

// -- the assistant at work ---------------------------------------------------
// While the assistant is changing a track, one crisp light runs round its channel.
// The server says which tracks and for how long (state.activity). The light runs for
// at least one full lap, so it is visible even when Live answers in 40 ms.

const litUntil = new Map(); // track name (lower case) -> performance.now() when its light should go out
let litTimer = null;

function noteActivity(activity) {
  const now = performance.now();
  for (const { track, ms } of activity || []) litUntil.set(track.toLowerCase(), now + ms);
}

function startRun(strip) {
  if (strip._running) return;
  strip._running = true;
  strip._runStart = performance.now();
  strip._heldDb = strip._stableDb;   // show the level as it was until the light stops
  strip._heldAt = strip._runStart;
  strip.classList.add("is-run");
  clearTimeout(strip._stopTimer);
  strip._stopTimer = null;
  paintNote(strip);
  const name = strip._row?.name || "channel";
  say(`Editing ${name}`);

  const rects = $$(".run rect", strip);
  const w = strip.offsetWidth, h = strip.offsetHeight;
  strip._runW = w;   // 0 while the mixer is hidden; paintActivity re-measures when it can be seen
  rects.forEach((r) => {
    r.getAnimations?.().forEach((a) => a.cancel());
    r.style.strokeDasharray = "";
    r.style.display = "";
  });
  const perimeter = w ? 2 * (w - 3 + h - 3) : 1000;
  strip._runDur = Math.min(2400, Math.max(1600, (perimeter / 520) * 1000));
  const seg = [[16, 0], [15, 16], [15, 31]];   // [dash length, distance behind the head] in px
  rects.forEach((r, i) => {
    const [len, behind] = seg[i];
    if (reducedMotion.matches) {
      // no motion: the head becomes a solid outline and the tails go
      if (i === 0) { r.style.strokeDasharray = "none"; r.style.strokeWidth = "2"; } else r.style.display = "none";
      return;
    }
    r.style.strokeDasharray = `${len} ${perimeter - len}`;
    r.animate(
      [{ strokeDashoffset: `${behind}px` }, { strokeDashoffset: `${behind - perimeter}px` }],
      { duration: strip._runDur, iterations: Infinity, easing: "linear" },
    );
  });
}

function stopRun(strip) {
  if (!strip._running) return;
  strip._running = false;
  strip.classList.remove("is-run");
  $$(".run rect", strip).forEach((r) => r.getAnimations?.().forEach((a) => a.cancel()));
  const name = strip._row?.name || "channel";
  say(`Changed ${name}`);

  // landing: the cap glides to where the change put it, and a tick marks where it was
  const was = strip._heldDb;
  strip._heldDb = undefined;
  const now = strip._lastDb;
  if (was !== undefined && was !== now && !reducedMotion.matches) {
    strip.classList.add("glide");
    setTimeout(() => strip.classList.remove("glide"), 460);
  }
  setFaderPosition(strip, now);
  if (was !== undefined && was !== now) {
    strip._r.ghost.style.bottom = `calc((var(--throw) - var(--cap-h)) * ${dbToPos(was)} + var(--cap-h) / 2)`;
    strip.classList.add("has-ghost");
    clearTimeout(strip._ghostTimer);
    strip._ghostTimer = setTimeout(() => strip.classList.remove("has-ghost"), 6000);
  }
  strip._changedUntil = performance.now() + 4000;
  clearTimeout(strip._changedTimer);
  strip._changedTimer = setTimeout(() => paintNote(strip), 4050);
  paintNote(strip);
}

function paintActivity() {
  const now = performance.now();
  let next = Infinity;
  let anyRun = false;
  for (const strip of strips.values()) {
    const until = litUntil.get((strip._row?.name || "").toLowerCase());
    const on = until !== undefined && until > now;
    if (strip._running && strip._runW === 0 && strip.offsetWidth) {   // started out of sight; size it now
      strip._running = false;
      startRun(strip);
    }
    if (on) {
      startRun(strip);
      if (strip._stopTimer) { clearTimeout(strip._stopTimer); strip._stopTimer = null; }
      next = Math.min(next, until);
    } else if (strip._running && !strip._stopTimer) {
      // never less than one whole lap since the light started
      const dur = strip._runDur || 2000;
      const laps = Math.max(1, Math.ceil((now - strip._runStart) / dur));
      const wait = strip._runStart + dur * laps - now;
      strip._stopTimer = setTimeout(() => { strip._stopTimer = null; stopRun(strip); paintActivity(); }, Math.max(0, wait));
    }
    if (strip._running) anyRun = true;
  }
  for (const [key, until] of litUntil) if (until <= now) litUntil.delete(key);
  clearTimeout(litTimer);
  if (next !== Infinity) litTimer = setTimeout(paintActivity, next - now + 30);
  $("#nav-chase").hidden = !anyRun;
}

window.addEventListener("resize", () => {
  for (const strip of strips.values()) if (strip._running) { stopRun(strip); strip._running = false; startRun(strip); }
});

// -- mixer -------------------------------------------------------------------
//
// Strips sit in folders by instrument: Vocals, Instruments and so on. The
// server picks each track's folder from its name, or from wherever the
// volunteer last moved it (row.folder), and sends the folder list in
// state.folders.
//
// Live can't reorder tracks or make group tracks from a Control Surface, so
// moving a strip to another folder also gives it that folder's colour in Live
// (the same set_track_color the colour picker uses). Colour is the one part of
// a folder Live can show, and it keeps the two screens saying the same thing.
const groupEls = new Map(); // folder key (or "returns") -> dom refs
let dragging = null;        // the strip being dragged to another folder
let selectedKey = null;     // "t0" / "r1": the channel shown in the drawer

let mixerShown = false;   // after the first drawing, new channels slide in rather than just appear

function renderMixer(snap) {
  flip($("#groups"), ".strip", (node) => node._key, () => drawMixer(snap));
  mixerShown = true;
}

function drawMixer(snap) {
  renderMixSong(snap);
  const groups = buildMixerGroups(snap);
  const groupsEl = $("#groups");
  const keepGroups = new Set();
  const keepStrips = new Set();

  // A folder is only moved when it is out of place: moving an element takes the focus away
  // from anything inside it, and this runs on every poll.
  let at = 0;
  groups.forEach((group) => {
    keepGroups.add(group.key);
    const g = ensureGroup(group.key);
    if (g.nameEl.textContent !== group.label) g.nameEl.textContent = group.label;
    const count = group.key === "returns" ? "" : String(group.rows.length);
    if (g.countEl.textContent !== count) g.countEl.textContent = count;
    g.section.classList.toggle("is-empty", group.rows.length === 0);
    if (groupsEl.children[at] !== g.section) groupsEl.insertBefore(g.section, groupsEl.children[at] || null);
    at += 1;
    placeStrips(g.stripsEl, group.rows, group.prefix, snap, keepStrips);
  });

  for (const [key, g] of groupEls) {
    if (!keepGroups.has(key)) {
      g.section.remove();
      groupEls.delete(key);
    }
  }
  for (const [key, strip] of strips) {
    if (!keepStrips.has(key)) {
      clearTimeout(strip._stopTimer);
      strips.delete(key);
      strip._key = null;
      leave(strip);
    }
  }

  $("#mixer-empty").hidden = snap.tracks.length > 0;
  keepSelection(groups);
  paintActivity();
}

/* Song mix: with a song picked, the mixer shows only the tracks that play in it, and
   each channel's drawer gets that song's own level (clip gain) and an on/off switch
   (Live's clip activator). Both are saved in the song's clips, so Live applies them when
   the song starts, with or without Holy Sound open. Faders, pan, mute and sends are kept
   per song by the server: picking a song (here, with Start, or by starting it in Live)
   puts its mix back, and every change after that is saved to it. The server says which song. */
let mixSong = null; // scene index, or null for every track

/* Left out of the picked song: muted there, or its clip switched off in Live. */
function leftOut(row) {
  return !!row.mute || songClip(row)?.active === false;
}

function songClip(row) {
  if (mixSong === null || row.is_return) return null;
  return (row.clips || []).find((c) => c.scene_index === mixSong && c.is_audio) || null;
}

function renderMixSong(snap) {
  const pick = $("#mix-song");
  if (!pick._busy) mixSong = state.song_mix?.scene_index ?? null;
  const sig = JSON.stringify(snap.scenes.map((s) => [s.index, s.name]));
  if (pick._sig !== sig) {
    pick._sig = sig;
    pick.replaceChildren(new Option("Every track", ""),
      ...snap.scenes.filter((s) => s.name).map((s) => new Option(`${s.index + 1}. ${s.name}`, String(s.index))));
  }
  if (document.activeElement !== pick && !pick._busy) pick.value = mixSong === null ? "" : String(mixSong);

  const info = state.song_mix || {};
  const saved = $("#mix-saved");
  saved.hidden = mixSong === null;
  saved.textContent = info.song ? `Changes save to ${info.song}` : "";
  const marks = $("#mix-checkpoints");
  marks.hidden = mixSong === null;
  marks.textContent = info.checkpoints?.length ? `Checkpoints (${info.checkpoints.length})` : "Checkpoints";
  if ($("#checkpoint-dialog").open) renderCheckpoints();
}

async function songMix(body) {
  try {
    const next = await api("/api/song-mix", body);
    mixPickedAt = performance.now();
    render(next);
    return true;
  } catch (e) {
    toast(e.message, "error");
    return false;
  }
}

$("#mix-song").addEventListener("change", async (e) => {
  const pick = e.target;
  const chosen = pick.value === "" ? null : Number(pick.value);
  // Before any redraw: a redraw takes the song from the server, which doesn't know this pick yet.
  // Putting the song's faders back takes a moment too; don't flick back meanwhile.
  pick._busy = true;
  mixSong = chosen;
  if (state?.live.snapshot) renderMixer(state.live.snapshot);
  try {
    await songMix({ action: "pick", scene_index: chosen });
  } finally {
    pick._busy = false;
    if (state?.live.snapshot) renderMixer(state.live.snapshot);
  }
});

function renderCheckpoints() {
  const info = state.song_mix || {};
  $("#checkpoint-song").textContent = info.song || "this song";
  const marks = info.checkpoints || [];
  $("#checkpoint-empty").hidden = marks.length > 0;
  $("#checkpoint-list").replaceChildren(...marks.map((m) => {
    const li = el("li", "checkpoint");
    const text = el("div", "checkpoint-text");
    const when = new Date(m.at * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
    text.append(el("strong", "", m.label), el("span", "checkpoint-when", when));
    const restore = button("Go back to this", "key", () => songMix({ action: "restore", id: m.id })
      .then((ok) => ok && toast(`${info.song} is back to “${m.label}”. The mix before it is saved as a checkpoint.`)));
    restore.type = "button";
    const remove = button("Delete", "text-btn", () => songMix({ action: "delete", id: m.id }));
    remove.type = "button";
    remove.setAttribute("aria-label", `Delete ${m.label}`);
    li.append(text, restore, remove);
    return li;
  }));
}

$("#mix-checkpoints").addEventListener("click", () => {
  $("#checkpoint-label").value = "";
  renderCheckpoints();
  $("#checkpoint-dialog").showModal();
});

$("#checkpoint-save").addEventListener("click", async () => {
  const label = $("#checkpoint-label").value.trim();
  await songMix({ action: "checkpoint", label });
  $("#checkpoint-label").value = "";
});
$("#checkpoint-label").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#checkpoint-save").click(); }
});

/* One entry per folder, in the server's order. Empty folders stay in the
   list (hidden until a drag starts) so there's somewhere to drop a track.
   Shared effects (return tracks) are a trailing folder you can't drop into. */
function buildMixerGroups(snap) {
  const folders = state.folders || [];
  const known = new Set(folders.map((f) => f.key));
  const byKey = new Map(folders.map((f) => [f.key, []]));
  for (const row of snap.tracks) {
    if (mixSong !== null && !songClip(row)) continue; // not in this song
    byKey.get(known.has(row.folder) ? row.folder : "other")?.push(row);
  }
  const groups = folders.map((f) => ({ key: f.key, label: f.label, rows: byKey.get(f.key), prefix: "t" }));
  if (snap.returns.length > 0) {
    groups.push({ key: "returns", label: "Shared effects", rows: snap.returns, prefix: "r" });
  }
  return groups;
}

function ensureGroup(key) {
  let g = groupEls.get(key);
  if (g) return g;

  const section = el("section", "track-group");
  section.dataset.group = key;

  const bus = el("button", "bus");
  bus.type = "button";
  const nameEl = el("span", "bus-name");
  const countEl = el("span", "bus-count");
  bus.append(nameEl, countEl, el("span", "fold"));

  const stripsEl = el("div", "strips");
  const slot = el("div", "drop-slot", "Drop here");
  section.append(bus, el("div", "bracket"), stripsEl, slot);

  const apply = (folded) => {
    section.classList.toggle("folded", folded);
    bus.setAttribute("aria-expanded", String(!folded));
    bus.setAttribute("aria-label", `${nameEl.textContent} folder`);
  };
  apply(loadCollapsedGroups().has(key));
  bus.addEventListener("click", () => {
    const folded = !section.classList.contains("folded");
    // the folders beside it slide over; opened strips fan back in
    flip($("#groups"), ".track-group", (node) => node.dataset.group, () => apply(folded));
    if (!folded) [...stripsEl.children].forEach((strip, i) => enter(strip, i * 35, "translateX(-10px)"));
    saveGroupCollapsed(key, folded);
  });
  // the label text arrives after the group is made
  new MutationObserver(() => apply(section.classList.contains("folded"))).observe(nameEl, { childList: true, characterData: true, subtree: true });

  if (key !== "returns") {
    section.addEventListener("dragover", (e) => {
      if (!dragging) return;
      e.preventDefault();
      section.classList.add("drop-target");
    });
    section.addEventListener("dragleave", (e) => {
      if (!section.contains(e.relatedTarget)) section.classList.remove("drop-target");
    });
    section.addEventListener("drop", (e) => {
      e.preventDefault();
      const strip = dragging;
      endDrag();
      if (strip) moveTrack(strip, key);
    });
  }

  g = { section, bus, stripsEl, nameEl, countEl };
  groupEls.set(key, g);
  return g;
}

function endDrag() {
  dragging?.classList.remove("is-dragging");
  dragging = null;
  $("#groups").classList.remove("dragging");
  $$(".track-group.drop-target").forEach((s) => s.classList.remove("drop-target"));
}

/* Move a strip to another folder: remember it on the server, then give the
   track that folder's colour in Live so Live shows the same grouping. */
async function moveTrack(strip, folderKey) {
  const row = strip._row;
  const folder = (state.folders || []).find((f) => f.key === folderKey);
  if (!row || row.is_return || !folder || row.folder === folderKey) return;
  try {
    render(await api("/api/track-folder", { track: row.name, folder: folderKey }));
  } catch (e) {
    toast(e.message, "error");
    return;
  }
  const recoloured = await liveCmd("set_track_color", { ...target(strip), rgb: parseInt(folder.color.slice(1), 16) })
    .then(() => true, () => false);
  if (recoloured) toast(`Moved ${row.name} to ${folder.label}.`);
}

/* Ableton can't show folders, so give every track its folder's colour there. */
async function matchColours() {
  const snap = state?.live.snapshot;
  if (!snap) return;
  const colours = new Map((state.folders || []).map((f) => [f.key, parseInt(f.color.slice(1), 16)]));
  let done = 0;
  for (const row of snap.tracks) {
    const rgb = colours.get(row.folder);
    if (rgb === undefined) continue;
    try {
      await liveCmd("set_track_color", { track_index: row.index, is_return: false, rgb });
      done += 1;
    } catch {
      return; // liveCmd already said why
    }
  }
  toast(`Copied ${done} folder colour${done === 1 ? "" : "s"} to Ableton.`);
}

/* Which folders this volunteer has folded -- a per-device convenience,
   not session state, so it's fine if it's empty on a fresh browser. */
function loadCollapsedGroups() {
  try {
    return new Set(JSON.parse(localStorage.getItem("holysound-collapsed-groups") || "[]"));
  } catch {
    return new Set();
  }
}

function saveGroupCollapsed(key, collapsed) {
  try {
    const set = loadCollapsedGroups();
    if (collapsed) set.add(key); else set.delete(key);
    localStorage.setItem("holysound-collapsed-groups", JSON.stringify([...set]));
  } catch {
    /* private browsing or storage disabled -- the fold just won't be remembered */
  }
}

function placeStrips(container, rows, prefix, snap, keep) {
  rows.forEach((row, i) => {
    const key = prefix + row.index;
    keep.add(key);
    let strip = strips.get(key);
    if (!strip) {
      strip = createStrip();
      strip._key = key;
      strips.set(key, strip);
      if (mixerShown) strip._entering = true;
    }
    if (container.children[i] !== strip) container.insertBefore(strip, container.children[i] || null);
    updateStrip(strip, row, snap);
    if (strip._entering) {
      strip._entering = false;
      enter(strip, 0, "translateY(14px) scale(0.96)");
    }
  });
}

function target(strip) {
  return { track_index: strip._row.index, is_return: strip._row.is_return };
}

// -- selecting a channel and the drawer ------------------------------------------

function keepSelection(groups) {
  const order = groups.flatMap((g) => g.rows.map((r) => g.prefix + r.index));
  const drawer = $("#drawer");
  drawer.hidden = order.length === 0;
  if (selectedKey && !strips.has(selectedKey)) selectedKey = null;
  for (const [key, strip] of strips) {
    const on = key === selectedKey;
    strip.classList.toggle("is-selected", on);
    strip._r.plate.setAttribute("aria-pressed", String(on));
  }
  if (!selectedKey) {
    if (!drawer.querySelector(".drawer-empty")) {
      drawer.replaceChildren(el("p", "drawer-empty", "Tap a channel to rename it, add an effect or change where it plays."));
    }
    return;
  }
  const strip = strips.get(selectedKey);
  if (drawer.firstElementChild !== strip._more) {
    drawer.replaceChildren(strip._more);
    enter(strip._more, 0, "translateY(6px)");
  }
}

function selectStrip(strip, scroll = false) {
  selectedKey = strip._key;
  keepSelection(buildMixerGroups(state.live.snapshot));
  if (scroll && matchMedia("(max-width: 820px), (max-height: 500px)").matches) {
    // bring the drawer's first section into view while the channel's own fader and Mute stay on screen
    const pane = $("#pane-session");
    const top = $("#drawer").getBoundingClientRect().top - pane.getBoundingClientRect().top;
    const want = top - (pane.clientHeight - 230);
    if (want > 0) pane.scrollBy({ top: want, behavior: reducedMotion.matches ? "auto" : "smooth" });
  }
}

// -- a channel strip ---------------------------------------------------------------

function buildMore(strip) {
  const more = el("div", "more");

  const channel = el("section", "m-channel");
  const title = el("h3", "", "Channel");
  const name = el("input", "drawer-name");
  name.setAttribute("aria-label", "Track name");
  name.maxLength = 24;
  const summary = el("p", "route-summary");
  const colourKey = el("button", "key colour-key");
  colourKey.type = "button";
  const chip = el("i", "colour-chip");
  colourKey.append(chip, document.createTextNode("Colour"));
  colourKey.addEventListener("click", () => openColorDialog(strip));
  channel.append(title, name, summary, colourKey);

  const sound = el("section", "m-sound");
  const keys = el("div", "sound-keys");
  const mute = el("button", "key mute-key", "Mute");
  const solo = el("button", "key solo", "Solo");
  for (const key of [mute, solo]) { key.type = "button"; key.setAttribute("aria-pressed", "false"); }
  keys.append(mute, solo);
  const soundHelp = el("p", "sound-help", "Mute silences it. Solo plays only this channel.");
  const balance = el("div", "control balance");
  balance.innerHTML = '<span class="control-label">Balance</span><input type="range" min="-1" max="1" step="0.02" aria-label="Balance, left to right"><output></output>';
  const song = el("div", "m-song");
  const songTitle = el("h3", "");
  const songOn = el("button", "key song-on");
  songOn.type = "button";
  const songLevel = el("div", "control song-level");
  songLevel.innerHTML = '<span class="control-label">Level</span><input type="range" min="-24" max="12" step="0.5"><output></output>';
  song.append(songTitle, songOn, songLevel);
  sound.append(el("h3", "", "Sound"), keys, soundHelp, balance, song);

  const effects = el("section", "m-effects");
  const sendsTitle = el("h3", "", "Reverb and delay");
  const sends = el("div", "sends");
  const devicesTitle = el("h3", "again", "Other effects");
  const devices = el("div", "devices");
  const addDevice = button("Add effect", "add-device", () => openDeviceDialog(strip));
  effects.append(sendsTitle, sends, devicesTitle, devices, addDevice);

  const routing = el("section", "m-routing");
  const folderLine = el("label", "folder-line");
  const folderSel = el("select", "folder-select");
  folderSel.setAttribute("aria-label", "Folder");
  folderLine.append(el("span", "", "Folder"), folderSel);
  const copyColours = button("Copy folder colours to Ableton", "text-btn copy-colours", matchColours);
  copyColours.title = "Ableton can’t show folders, so each folder gets its own colour there";
  const routeLine = el("div", "route-line");
  const clipGains = el("div", "clip-gains");
  const keyLine = el("label", "key-line");
  const followsKey = el("input", "follows-key");
  followsKey.type = "checkbox";
  keyLine.append(followsKey, el("span", "", "Changes with the song key"));
  routing.append(el("h3", "", "Where it plays"), folderLine, copyColours, routeLine, keyLine, clipGains);

  const tone = buildTone(strip);
  more.append(channel, sound, effects, routing, tone.section);
  strip._more = more;
  return {
    title, name, summary, colourChip: chip, mute, solo, soundHelp,
    balance, balanceInput: $("input", balance), balanceOut: $("output", balance),
    sendsTitle, sends, devicesTitle, devices, addDevice, folderLine, copyColours, folderSel, routeLine, clipGains,
    song, songTitle, songOn, songLevelInput: $("input", songLevel), songLevelOut: $("output", songLevel), keyLine, followsKey,
    tone,
  };
}

function createStrip() {
  const strip = $("#strip-template").content.firstElementChild.cloneNode(true);
  const m = buildMore(strip);
  const r = {
    plate: $(".plate", strip), name: $(".strip-name", strip), note: $(".strip-note", strip),
    mute: $(".mute", strip), solo: $(".solo", strip), pan: $(".pan", strip), panInput: $(".pan input", strip),
    zone: $(".fader-zone", strip), fader: $(".fader", strip),
    ghost: $(".ghost", strip), meter: $(".meter", strip), more: m,
  };
  strip._r = r;

  // selecting: the name plate is the button; a tap on the strip's own padding counts too
  r.plate.addEventListener("click", () => selectStrip(strip, true));
  strip.addEventListener("click", (e) => {
    if (e.target === strip || e.target === r.note) selectStrip(strip, true);
  });

  // naming happens in the drawer
  const rename = () => {
    const value = m.name.value.trim();
    if (value && value !== strip._row.name) liveCmd("set_track_name", { ...target(strip), name: value }).catch(() => {});
  };
  m.name.addEventListener("change", rename);
  m.name.addEventListener("keydown", (e) => { if (e.key === "Enter") m.name.blur(); });

  const toggleMute = () => {
    const on = !strip._row.mute;
    strip._row.mute = on;
    liveCmd("set_mute", { ...target(strip), on }).catch(() => { strip._row.mute = !on; });
  };
  r.mute.addEventListener("click", toggleMute);
  m.mute.addEventListener("click", toggleMute);
  const toggleSolo = () => {
    const on = !strip._row.solo;
    strip._row.solo = on;
    liveCmd("set_solo", { ...target(strip), on }).catch(() => { strip._row.solo = !on; });
  };
  r.solo.addEventListener("click", toggleSolo);
  m.solo.addEventListener("click", toggleSolo);
  slider(m.balanceInput, m.balanceOut, (pan) => ({ cmd: "set_pan", args: { ...target(strip), pan } }), panText);
  // the strip's own pan bar moves the same value; the dot follows it at once
  slider(r.panInput, m.balanceOut, (pan) => ({ cmd: "set_pan", args: { ...target(strip), pan } }), panText);
  r.panInput.addEventListener("input", () => r.pan.style.setProperty("--p", String((parseFloat(r.panInput.value) + 1) / 2)));
  r.panInput.addEventListener("pointerdown", () => r.pan.classList.add("is-down"));
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) r.panInput.addEventListener(ev, () => r.pan.classList.remove("is-down"));
  r.panInput.addEventListener("dblclick", () => { r.panInput.value = "0"; r.panInput.dispatchEvent(new Event("input", { bubbles: true })); r.panInput.dispatchEvent(new Event("change", { bubbles: true })); });

  wireFader(strip);

  slider(m.songLevelInput, m.songLevelOut,
    (db) => ({ cmd: "set_clip_gain", args: { track_index: strip._row.index, scene_index: mixSong, db } }), dbText);
  // In or out of the picked song is that song's mute: instant while it plays, saved per song.
  m.songOn.addEventListener("click", () => {
    const clip = songClip(strip._row);
    if (!clip) return;
    if (leftOut(strip._row)) {
      // a clip switched off in Live (the old way) won't play until it's back on
      if (clip.active === false) liveCmd("set_clip_active", { track_index: strip._row.index, scene_index: mixSong, on: true }).catch(() => {});
      if (strip._row.mute) toggleMute();
    } else toggleMute();
  });
  m.followsKey.addEventListener("change", async () => {
    try {
      render(await api("/api/track-key", { track: strip._row.name, follows: m.followsKey.checked }));
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // moving a track between folders: drag the name plate (computers), or pick from the list (drawer)
  r.plate.addEventListener("dragstart", (e) => {
    if (strip._row.is_return) return e.preventDefault();
    dragging = strip;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", strip._row.name);
    e.dataTransfer.setDragImage(strip, 24, 24);
    strip.classList.add("is-dragging");
    $("#groups").classList.add("dragging");
  });
  r.plate.addEventListener("dragend", endDrag);
  let folderTimer = null;
  const commitFolder = () => {
    clearTimeout(folderTimer);
    const chosen = m.folderSel._pending;
    m.folderSel._pending = null;
    if (chosen && chosen !== strip._row.folder) moveTrack(strip, chosen);
  };
  m.folderSel.addEventListener("change", () => {
    m.folderSel._pending = m.folderSel.value;   // arrow keys fire change on every step; only the last one counts
    clearTimeout(folderTimer);
    folderTimer = setTimeout(commitFolder, 700);
  });
  m.folderSel.addEventListener("blur", commitFolder);
  m.folderSel.addEventListener("keydown", (e) => { if (e.key === "Enter") commitFolder(); });
  return strip;
}

/* The fader: a drawn cap over an invisible native range input, so the keyboard,
   touch and screen readers get a real slider while the eye gets a console. */
function wireFader(strip) {
  const r = strip._r;
  const input = r.fader;
  let last = 0;
  let timer = null;
  let lastDb = null;

  const currentDb = () => posToDb(parseFloat(input.value) / 1000);
  const push = () => {
    liveCmd("set_volume", { ...target(strip), db: strip._lastDb ?? currentDb() }).catch(() => {});
    last = Date.now();
  };
  const show = (db) => {
    strip._lastDb = db;
    r.zone.style.setProperty("--p", String(dbToPos(db)));
    input.setAttribute("aria-valuetext", db <= -70 ? "Off" : `${percent(db)} percent`);
  };
  const apply = (db, vibrate) => {
    input.value = String(Math.round(dbToPos(db) * 1000));
    if (vibrate && db === 0 && lastDb !== 0) navigator.vibrate?.(5);
    lastDb = db;
    show(db);
    clearTimeout(timer);
    if (Date.now() - last > 150) push();
    else timer = setTimeout(push, 150);
  };

  input.addEventListener("pointerdown", () => { holding.add(input); strip.classList.add("is-down"); });
  const up = () => { strip.classList.remove("is-down"); setTimeout(() => holding.delete(input), 1200); };
  input.addEventListener("pointerup", up);
  input.addEventListener("pointercancel", up);
  input.addEventListener("lostpointercapture", up);
  input.addEventListener("input", () => {
    holding.add(input);
    apply(currentDb(), true);
  });
  input.addEventListener("change", () => {
    clearTimeout(timer);
    push();
    setTimeout(() => holding.delete(input), 1200);
  });
  input.addEventListener("dblclick", () => { holding.add(input); apply(0, false); setTimeout(() => holding.delete(input), 1200); });

  // on a touch screen the native input is switched off; the cap is the handle
  const hit = $(".cap-hit", strip);
  let grab = null;
  hit.addEventListener("pointerdown", (e) => {
    const zone = r.zone.getBoundingClientRect();
    grab = { startY: e.clientY, startPos: parseFloat(input.value) / 1000, travel: zone.height - 22 };
    hit.setPointerCapture(e.pointerId);
    holding.add(input);
    strip.classList.add("is-down");
    e.preventDefault();
  });
  hit.addEventListener("pointermove", (e) => {
    if (!grab) return;
    const pos = Math.min(1, Math.max(0, grab.startPos + (grab.startY - e.clientY) / grab.travel));
    apply(posToDb(pos), true);
  });
  const letGo = () => {
    if (!grab) return;
    grab = null;
    strip.classList.remove("is-down");
    clearTimeout(timer);
    push();
    setTimeout(() => holding.delete(input), 1200);
  };
  hit.addEventListener("pointerup", letGo);
  hit.addEventListener("pointercancel", letGo);
  hit.addEventListener("dblclick", () => { holding.add(input); apply(0, false); setTimeout(() => holding.delete(input), 1200); });
  input.addEventListener("keydown", (e) => {
    const step = { ArrowUp: 10, ArrowRight: 10, ArrowDown: -10, ArrowLeft: -10, PageUp: 50, PageDown: -50 }[e.key];
    if (step === undefined) return;
    e.preventDefault();
    holding.add(input);
    const pos = Math.min(1000, Math.max(0, parseInt(input.value, 10) + step));
    apply(posToDb(pos / 1000, false), false);
    setTimeout(() => holding.delete(input), 1200);
  });
}

function setFaderPosition(strip, db) {
  const r = strip._r;
  r.fader.value = String(Math.round(dbToPos(db) * 1000));
  r.zone.style.setProperty("--p", String(dbToPos(db)));
  r.fader.setAttribute("aria-valuetext", db === null || db <= -70 ? "Off" : `${percent(db)} percent`);
}

/* A range input that talks to Live while dragging, without fighting the poll. */
function slider(input, output, command, format) {
  let last = 0;
  let timer = null;
  const push = () => {
    const { cmd, args } = command(parseFloat(input.value));
    liveCmd(cmd, args).catch(() => {});
    last = Date.now();
  };
  input.addEventListener("pointerdown", () => { holding.add(input); stopTween(input, "value"); input._glideTo = null; });
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) {
    input.addEventListener(ev, () => setTimeout(() => holding.delete(input), 1200));
  }
  const announce = () => input.setAttribute("aria-valuetext", format(parseFloat(input.value)));
  announce();
  new MutationObserver(announce).observe(input, { attributes: true, attributeFilter: ["value"] });
  input.addEventListener("input", () => {
    holding.add(input);
    stopTween(input, "value");
    input._glideTo = null;
    output.textContent = format(parseFloat(input.value));
    announce();
    clearTimeout(timer);
    if (Date.now() - last > 150) push();
    else timer = setTimeout(push, 150);
  });
  input.addEventListener("change", () => {
    clearTimeout(timer);
    push();
    setTimeout(() => holding.delete(input), 1200);
  });
}

/* What the input and output mean, in a sentence a volunteer would say. */
function describeRouting(row) {
  if (row.is_return) return "A reverb or delay that other channels can send to.";
  const input = row.is_midi ? "MIDI instrument"
    : !row.input || row.input.type === "No Input" ? "Playback track"
    : row.input.type === "Ext. In" ? `Input ${row.input.channel}` : row.input.type;
  const out = row.output;
  const output = !out || out.type === "Master" ? "Plays through the main speakers"
    : out.type === "Ext. Out" ? `Plays to outputs ${out.channel}`
    : out.type === "Sends Only" ? "Only feeds the shared effects" : `Plays to ${out.type}`;
  return `${input}. ${output}.`;
}

/* The line under a channel's name: what the assistant is doing to it, then that it did it. */
/* The line under a channel's name: what the assistant is doing to it, then that it did it;
   otherwise where its sound comes from, in two words. */
function paintNote(strip) {
  const note = strip._r.note;
  const row = strip._row;
  if (strip._running) note.textContent = "Editing…";
  else if (strip._changedUntil && performance.now() < strip._changedUntil) note.textContent = "Changed";
  else if (row?.solo) note.textContent = "Solo";
  else if (row && songClip(row) && leftOut(row)) note.textContent = "Left out";
  else note.textContent = row ? idleNote(row) : "";
}

function idleNote(row) {
  if (row.is_return) return "Shared";
  const out = row.output;
  if (out && out.type !== "Master") return out.type === "Ext. Out" ? `Out ${out.channel}` : out.type === "Sends Only" ? "Effects only" : out.type;
  if (row.is_midi) return "MIDI";
  if (row.input && row.input.type === "Ext. In") return `Input ${row.input.channel}`;
  return "Playback";
}

function updateStrip(strip, row, snap) {
  const r = strip._r;
  const m = r.more;
  strip._row = row;
  strip.classList.toggle("is-return", row.is_return);
  strip.classList.toggle("is-muted", row.mute);
  strip.classList.toggle("is-solo", !!row.solo);
  if (row.color != null) {
    const hex = "#" + row.color.toString(16).padStart(6, "0");
    strip.style.setProperty("--track", hex);
    strip._more.style.setProperty("--track", hex);
  }

  const number = row.is_return ? String.fromCharCode(65 + row.index) : String(row.index + 1);
  const shown = row.is_return ? row.name.replace(/^[A-Z]-\s*/, "") : row.name;   // Ableton's "A-" prefix stays out of sight
  if (r.name.textContent !== shown) r.name.textContent = shown;
  strip.setAttribute("aria-label", `${row.name}, ${row.is_return ? "shared effect " : "track "}${number}`);
  r.plate.setAttribute("aria-label", `Select ${row.name}`);
  r.plate.title = row.is_return ? row.name : `${row.name}. Drag to another folder.`;
  if (document.activeElement !== m.name) m.name.value = row.name;
  m.title.textContent = row.is_return ? `Shared effect ${number}` : `Channel ${number}`;
  m.summary.textContent = describeRouting(row);
  m.balance.hidden = row.is_return;
  m.soundHelp.hidden = row.is_return;
  m.sendsTitle.hidden = row.is_return;
  m.sends.hidden = row.is_return;
  m.copyColours.hidden = row.is_return;
  const routeKey = `${row.output?.type || ""}|${row.output?.channel || ""}`;
  if ($("#drawer").contains(strip._more) && strip._routeKey !== routeKey && !strip._more.contains(document.activeElement)) {
    strip._routeKey = routeKey;
    loadRouting(strip);
  }
  if (row.is_return) m.devicesTitle.textContent = "Effects";
  else {
    const names = row.sends.map((s) => s.return.replace(/^[A-Z]-/, ""));
    m.sendsTitle.textContent = names.length ? names.join(" and ") : "Reverb and delay";
    m.devicesTitle.textContent = "Other effects";
  }
  paintNote(strip);
  updateSongMix(strip, row, snap);

  // Return tracks (shared effects) aren't sorted into instrument folders.
  r.plate.draggable = !row.is_return;
  m.folderLine.hidden = row.is_return;
  const folders = state.folders || [];
  const folderSig = folders.map((f) => f.key + f.label).join("|");
  if (m.folderSel._sig !== folderSig) {
    m.folderSel._sig = folderSig;
    m.folderSel.replaceChildren(...folders.map((f) => new Option(f.label, f.key)));
  }
  if (!row.is_return && document.activeElement !== m.folderSel && !m.folderSel._pending) m.folderSel.value = row.folder;

  for (const key of [r.mute, m.mute]) {
    key.setAttribute("aria-pressed", row.mute);
    key.textContent = row.mute ? "Muted" : "Mute";
  }
  for (const key of [r.solo, m.solo]) key.setAttribute("aria-pressed", !!row.solo);
  paintMeter(r.meter, row.meter);

  // the level: while the assistant's light is running, hold what was there before
  const db = row.volume_db === null || row.volume_db === undefined ? -70 : row.volume_db;
  const held = strip._running && strip._heldDb !== undefined && performance.now() - strip._heldAt < 4000;
  // While the volunteer has hold of the fader, what's on screen is the truth: an update that
  // was already on its way back from Live would otherwise put an older level back.
  if (!holding.has(r.fader)) {
    strip._lastDb = db;
    // the level as it was before the assistant started on this channel (the change and the
    // news that the assistant is working can arrive in the same update)
    const lit = (litUntil.get(row.name.toLowerCase()) || 0) > performance.now();
    if (!strip._running && !lit) strip._stableDb = db;
  }
  if (!holding.has(r.fader)) setFaderPosition(strip, held ? strip._heldDb : db);

  const pan = row.pan_value ?? panFromText(row.pan);
  if (!holding.has(r.panInput)) {
    glideInput(r.panInput, pan, (v) => r.pan.style.setProperty("--p", String((v + 1) / 2)));
    r.panInput.setAttribute("aria-valuetext", panText(pan));
  }
  if (!holding.has(m.balanceInput)) {
    glideInput(m.balanceInput, pan);
    const words = panText(pan);
    if (m.balanceInput.getAttribute("aria-valuetext") !== words) m.balanceInput.setAttribute("aria-valuetext", words);
    if (m.balanceOut.textContent !== words) m.balanceOut.textContent = words;
  }
  m.balanceInput.setAttribute("aria-label", `Balance, ${row.name}`);

  const devSig = JSON.stringify(row.devices);
  if (m.devices._sig !== devSig) {
    const had = m.devices._sig ? JSON.parse(m.devices._sig).length : null;
    m.devices._sig = devSig;
    m.devices.replaceChildren(...row.devices.map((d, i) => deviceRow(strip, d, i)));
    if (had !== null) [...m.devices.children].slice(had).forEach((node, i) => enter(node, i * 60));
  }

  if (m.sends.children.length !== row.sends.length) {
    m.sends.replaceChildren(...row.sends.map((s, i) => sendControl(strip, i)));
  }
  row.sends.forEach((s, i) => {
    const control = m.sends.children[i];
    const label = $(".control-label", control);
    label.textContent = s.return.replace(/^[A-Z]-/, "");
    label.title = s.return;
    const input = $("input", control);
    input.setAttribute("aria-label", `${label.textContent} send, ${row.name}`);
    if (!holding.has(input)) {
      const out = $("output", control);
      glideInput(input, Math.round(dbToPos(s.level_db ?? -70) * 1000), (v) => {
        const shown = sendText(posToDb(v / 1000));
        if (out.textContent !== shown) out.textContent = shown;
      });
      const words = sendText(s.level_db);
      if (input.getAttribute("aria-valuetext") !== words) input.setAttribute("aria-valuetext", words);
    }
  });
  updateClips(strip, row, snap);
  updateTone(strip, row);
}

/* The picked song's own level and On/Off for this channel, and whether it follows the song key. */
function updateSongMix(strip, row, snap) {
  const m = strip._r.more;
  const clip = songClip(row);
  strip.classList.toggle("song-off", !!clip && leftOut(row));
  m.song.hidden = !clip;
  if (clip) {
    const on = !leftOut(row);
    const name = songName(snap, mixSong);
    m.songTitle.textContent = `In ${name}`;
    m.songOn.setAttribute("aria-pressed", String(!on));
    m.songOn.textContent = on ? "Playing in this song" : "Left out of this song";
    m.songOn.title = on ? `Press to leave ${row.name} out of ${name}.` : `Press to bring ${row.name} back into ${name}.`;
    m.songLevelInput.setAttribute("aria-label", `${row.name} level in ${name}`);
    if (!holding.has(m.songLevelInput)) {
      glideInput(m.songLevelInput, clip.gain_db ?? 0, (v) => { m.songLevelOut.textContent = dbText(Math.round(v * 10) / 10); });
    }
  }
  // Click and guide keep their key when a song is transposed; any track can opt in or out.
  m.keyLine.hidden = row.is_return || row.is_midi;
  m.followsKey.checked = !row.keeps_key;
}

/* Live's meters run 0-1 (after the fader). The snapshot only says whether a channel has one;
   the level comes from the fast meter feed, or from the snapshot when RigLink is too old for it. */
function paintMeter(meter, reading) {
  if (!meter) return;
  meter.hidden = reading == null;
  if (!meterFeedLive()) aimMeter(meter, reading?.peak ?? 0);
}

function songName(snap, sceneIndex) {
  const scene = snap.scenes.find((s) => s.index === sceneIndex);
  return scene?.name || `Song ${sceneIndex + 1}`;
}

function updateClips(strip, row, snap) {
  const m = strip._r.more;
  const clips = row.clips || [];
  const gains = m.clipGains;
  const gainSig = JSON.stringify(clips.filter((c) => c.is_audio).map((c) => [c.scene_index, songName(snap, c.scene_index)]));
  if (gains._sig !== gainSig) {
    gains._sig = gainSig;
    const rows = clips.filter((c) => c.is_audio).map((c) => clipGainControl(strip, c, snap));
    gains.replaceChildren(...(rows.length ? [el("h3", "", "Level in each song"), ...rows] : []));
  }
  for (const control of gains.querySelectorAll(".clip-gain")) {
    const clip = clips.find((c) => c.scene_index === control._scene);
    const input = $("input", control);
    if (!clip || holding.has(input)) continue;
    const out = $("output", control);
    glideInput(input, clip.gain_db ?? -24, (v) => { out.textContent = dbText(Math.round(v * 10) / 10); });
  }
}

function clipGainControl(strip, clip, snap) {
  const control = el("div", "control clip-gain");
  control._scene = clip.scene_index;
  control.innerHTML = `<span class="control-label"></span><input type="range" min="-24" max="12" step="0.5"><output></output>`;
  const label = $(".control-label", control);
  label.textContent = songName(snap, clip.scene_index);
  label.title = `Clip level: ${clip.name}`;
  $("input", control).setAttribute("aria-label", `Clip level in ${songName(snap, clip.scene_index)}`);
  slider($("input", control), $("output", control),
    (db) => ({ cmd: "set_clip_gain", args: { track_index: strip._row.index, scene_index: clip.scene_index, db } }), dbText);
  return control;
}

// -- tone: the channel's EQ Eight, drawn as a curve with a handle per band ----------------

const EQ_H = 200;        // viewBox height; the width follows the graph's shape on screen
const EQ_TOP = 18;       // room for a handle at +15 dB
const EQ_BOTTOM = 162;   // and at -15 dB, above the frequency labels
const EQ_MIN_HZ = 20;
const EQ_MAX_HZ = 20000;
const EQ_RANGE_DB = 15;  // EQ Eight's own gain range
const EQ_RATE = 48000;   // for drawing only
const EQ_GAINED = new Set(["low shelf", "bell", "high shelf"]);
const EQ_LABELS = {
  "low cut 48": "Low cut (steep)", "low cut": "Low cut", "low shelf": "Low shelf", "bell": "Bell",
  "notch": "Notch", "high shelf": "High shelf", "high cut": "High cut", "high cut 48": "High cut (steep)",
};
const EQ_GUESSED = Object.keys(EQ_LABELS);
const SVG_NS = "http://www.w3.org/2000/svg";

/* One of EQ_LABELS' keys for Live's name of a filter type (same rules as app/eq.py). */
function eqKind(text, index) {
  const t = (text || "").toLowerCase();
  const steep = t.includes("48");
  let side = null;
  if (t.includes("notch")) return "notch";
  if (t.includes("bell") || t.includes("peak")) return "bell";
  if (t.includes("shelf")) return t.includes("low") ? "low shelf" : "high shelf";
  if (t.includes("pass")) side = t.includes("high") ? "low cut" : "high cut";
  else if (t.includes("cut")) side = t.includes("low") ? "low cut" : "high cut";
  if (side) return steep ? side + " 48" : side;
  return EQ_GUESSED[index] || "bell";
}

const EQ_MID = (EQ_TOP + EQ_BOTTOM) / 2;
const EQ_HALF = (EQ_BOTTOM - EQ_TOP) / 2;
const hzToX = (hz, w) => (Math.log(hz / EQ_MIN_HZ) / Math.log(EQ_MAX_HZ / EQ_MIN_HZ)) * w;
const xToHz = (x, w) => EQ_MIN_HZ * Math.pow(EQ_MAX_HZ / EQ_MIN_HZ, Math.min(1, Math.max(0, x / w)));
const dbToY = (db) => EQ_MID - (db / EQ_RANGE_DB) * EQ_HALF;
const yToDb = (y) => Math.max(-EQ_RANGE_DB, Math.min(EQ_RANGE_DB, ((EQ_MID - y) / EQ_HALF) * EQ_RANGE_DB));

function hzText(hz) {
  return hz >= 1000 ? `${(hz / 1000).toFixed(hz >= 10000 ? 1 : 2)} kHz` : `${Math.round(hz)} Hz`;
}

/* A band's response in dB at one frequency: the textbook (RBJ) biquads, close enough to draw. */
function bandDb(b, hz) {
  const w0 = (2 * Math.PI * b.freq_hz) / EQ_RATE;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * Math.max(0.1, b.q));
  const A = Math.pow(10, b.gain_db / 40);
  const sq = 2 * Math.sqrt(A) * alpha;
  let c;  // [b0, b1, b2, a0, a1, a2]
  switch (b.kind.replace(" 48", "")) {
    case "bell": c = [1 + alpha * A, -2 * cos, 1 - alpha * A, 1 + alpha / A, -2 * cos, 1 - alpha / A]; break;
    case "low shelf": c = [A * ((A + 1) - (A - 1) * cos + sq), 2 * A * ((A - 1) - (A + 1) * cos), A * ((A + 1) - (A - 1) * cos - sq),
      (A + 1) + (A - 1) * cos + sq, -2 * ((A - 1) + (A + 1) * cos), (A + 1) + (A - 1) * cos - sq]; break;
    case "high shelf": c = [A * ((A + 1) + (A - 1) * cos + sq), -2 * A * ((A - 1) + (A + 1) * cos), A * ((A + 1) + (A - 1) * cos - sq),
      (A + 1) - (A - 1) * cos + sq, 2 * ((A - 1) - (A + 1) * cos), (A + 1) - (A - 1) * cos - sq]; break;
    case "low cut": c = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2, 1 + alpha, -2 * cos, 1 - alpha]; break;
    case "high cut": c = [(1 - cos) / 2, 1 - cos, (1 - cos) / 2, 1 + alpha, -2 * cos, 1 - alpha]; break;
    case "notch": c = [1, -2 * cos, 1, 1 + alpha, -2 * cos, 1 - alpha]; break;
    default: return 0;
  }
  const w = (2 * Math.PI * hz) / EQ_RATE;
  const mag = (x0, x1, x2) => {
    const re = x0 + x1 * Math.cos(w) + x2 * Math.cos(2 * w);
    const im = x1 * Math.sin(w) + x2 * Math.sin(2 * w);
    return re * re + im * im;
  };
  const db = 10 * Math.log10(mag(c[0], c[1], c[2]) / mag(c[3], c[4], c[5]));
  return b.kind.endsWith("48") ? db * 4 : db;
}

function eqBands(eq) {
  return eq.bands.map((b) => ({ ...b, kind: eqKind(b.type, b.type_index) }));
}

function curvePath(bands, w) {
  const on = bands.filter((b) => b.on);
  let d = "";
  for (let x = 0; x <= w; x += 3) {
    const hz = xToHz(x, w);
    const db = on.reduce((sum, b) => sum + bandDb(b, hz), 0);
    const y = Math.max(EQ_TOP - 12, Math.min(EQ_BOTTOM + 12, dbToY(db)));
    d += `${x ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }
  return d;
}

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function buildTone(strip) {
  const section = el("section", "m-tone");
  const head = el("div", "tone-head");
  const title = el("h3", "", "Tone (EQ)");
  const flat = button("Flat", "text-btn tone-flat", () => eqFlat(strip));
  head.append(title, flat);
  const help = el("p", "tone-help");
  const svg = svgEl("svg", { class: "eq-graph", role: "group" });
  const grid = svgEl("g", { class: "eq-grid" });
  const fill = svgEl("path", { class: "eq-fill" });
  const curve = svgEl("path", { class: "eq-curve" });
  const handles = svgEl("g", { class: "eq-handles" });
  svg.append(grid, fill, curve, handles);
  const band = el("div", "tone-band");
  const bandName = el("span", "tone-band-name");
  const typeSel = el("select", "tone-type");
  typeSel.setAttribute("aria-label", "Filter type");
  const onLine = el("label", "tone-on");
  const onBox = el("input");
  onBox.type = "checkbox";
  onLine.append(onBox, el("span", "", "On"));
  const readout = el("output", "tone-readout");
  band.append(bandName, typeSel, onLine, readout);
  const none = el("div", "tone-none");
  none.append(el("p", "", "No EQ on this channel yet."),
    button("Add EQ", "add-device", () => liveCmd("load_device", { ...target(strip), device_name: "EQ Eight" }).catch(() => {})));
  section.append(head, help, none, svg, band);

  const t = { section, title, flat, help, svg, grid, fill, curve, handles, band, bandName, typeSel, onBox, readout, none,
    selected: 1, bands: null, w: 600 };
  new ResizeObserver(() => layoutTone(strip)).observe(svg);
  typeSel.addEventListener("change", () => setBand(strip, t.selected, { type_index: parseInt(typeSel.value, 10) }, true));
  onBox.addEventListener("change", () => setBand(strip, t.selected, { on: onBox.checked }, true));
  return t;
}

/* Fit the drawing to the graph's shape on screen, so it fills the width without stretching. */
function layoutTone(strip) {
  const t = strip._r.more.tone;
  const box = t.svg.getBoundingClientRect();
  if (!box.width || !box.height) return;
  const w = Math.round((box.width / box.height) * EQ_H);
  if (w === t.w && t.grid.childElementCount) return;
  t.w = w;
  t.svg.setAttribute("viewBox", `0 0 ${w} ${EQ_H}`);
  const lines = [];
  for (const hz of [50, 100, 200, 500, 1000, 2000, 5000, 10000]) {
    lines.push(svgEl("line", { x1: hzToX(hz, w), x2: hzToX(hz, w), y1: 0, y2: EQ_BOTTOM + 14 }));
    const label = svgEl("text", { x: hzToX(hz, w), y: EQ_H - 8, "text-anchor": "middle" });
    label.textContent = hz >= 1000 ? `${hz / 1000}k` : String(hz);
    lines.push(label);
  }
  for (const db of [-12, -6, 6, 12]) lines.push(svgEl("line", { x1: 0, x2: w, y1: dbToY(db), y2: dbToY(db), class: "minor" }));
  lines.push(svgEl("line", { x1: 0, x2: w, y1: dbToY(0), y2: dbToY(0), class: "zero" }));
  t.grid.replaceChildren(...lines);
  paintTone(strip);
}

/* Change one band on screen at once, and in Live (throttled while dragging). */
function setBand(strip, n, change, now = false) {
  const t = strip._r.more.tone;
  stopTween(t, "bands");
  const b = t.bands && t.bands.find((x) => x.band === n);
  if (!b) return;
  Object.assign(b, change);
  if (change.type_index !== undefined) b.kind = eqKind(t.types[change.type_index], change.type_index);
  t.heldUntil = performance.now() + 1500;
  paintTone(strip);
  t.pending = { ...(t.pending || {}), ...change };
  const push = () => {
    const args = { ...target(strip), band: n, device_index: t.deviceIndex, ...t.pending };
    t.pending = null;
    t.lastPush = Date.now();
    liveCmd("set_eq_band", args).catch(() => {});
  };
  clearTimeout(t.timer);
  if (now || Date.now() - (t.lastPush || 0) > 120) push();
  else t.timer = setTimeout(push, 120);
}

async function eqFlat(strip) {
  try {
    render(await api("/api/eq-flat", { track: strip._row.name, is_return: strip._row.is_return }));
  } catch (e) {
    toast(e.message, "error");
  }
}

function updateTone(strip, row) {
  const t = strip._r.more.tone;
  const eq = row.eq;
  t.none.hidden = !!eq;
  t.svg.style.display = eq ? "" : "none";
  t.band.hidden = !eq;
  t.flat.hidden = !eq;
  t.help.textContent = mixSong != null
    ? `Drag a dot to shape the sound. Saved with ${songName(state.live.snapshot, mixSong) || "this song"}.`
    : "Drag a dot to shape the sound: left and right is pitch, up and down is louder or quieter.";
  if (!eq) { t.bands = null; return; }
  if (t.dragging || performance.now() < (t.heldUntil || 0)) return;
  const sig = JSON.stringify(eq);
  if (t.sig === sig) return;
  t.sig = sig;
  t.types = eq.types && eq.types.length ? eq.types : EQ_GUESSED;
  t.deviceIndex = eq.device_index;
  const from = t.bands && t.bands.length === eq.bands.length ? t.bands.map((b) => ({ ...b })) : null;
  const to = eqBands(eq);
  t.bands = to;
  const typesSig = t.types.join("|");
  if (t.typeSel._sig !== typesSig) {
    t.typeSel._sig = typesSig;
    t.typeSel.replaceChildren(...t.types.map((text, i) => new Option(EQ_LABELS[eqKind(text, i)] || text, String(i))));
  }
  if (t.handles.childElementCount !== t.bands.length) buildHandles(strip);   // keep focus on a dot across updates
  if (!from) {
    paintTone(strip);
    return;
  }
  // the dots and the curve travel from the old settings to the new: pitch on a log scale
  tween(t, "bands", 0, 1, (k) => {
    t.bands = to.map((b, i) => ({
      ...b,
      freq_hz: Math.exp(Math.log(from[i].freq_hz) + (Math.log(b.freq_hz) - Math.log(from[i].freq_hz)) * k),
      gain_db: from[i].gain_db + (b.gain_db - from[i].gain_db) * k,
      q: from[i].q + (b.q - from[i].q) * k,
    }));
    paintTone(strip);
  }, 520);
}

function buildHandles(strip) {
  const t = strip._r.more.tone;
  t.handles.replaceChildren(...t.bands.map((b) => {
    const g = svgEl("g", { class: "eq-handle", tabindex: "0", role: "slider" });
    g.append(svgEl("circle", { r: 13 }));
    const label = svgEl("text", { "text-anchor": "middle", dy: "4.5" });
    label.textContent = String(b.band);
    g.append(label);
    g._band = b.band;
    wireHandle(strip, g);
    return g;
  }));
}

function wireHandle(strip, g) {
  const t = strip._r.more.tone;
  const n = g._band;
  const toGraph = (e) => {
    const box = t.svg.getBoundingClientRect();
    return { x: ((e.clientX - box.left) / box.width) * t.w, y: ((e.clientY - box.top) / box.height) * EQ_H };
  };
  g.addEventListener("pointerdown", (e) => {
    stopTween(t, "bands");
    t.selected = n;
    t.dragging = true;
    g.setPointerCapture(e.pointerId);
    paintTone(strip);
    e.preventDefault();
  });
  g.addEventListener("pointermove", (e) => {
    if (!t.dragging || !g.hasPointerCapture(e.pointerId)) return;
    const p = toGraph(e);
    const b = t.bands.find((x) => x.band === n);
    const change = { freq_hz: Math.round(xToHz(p.x, t.w)) };
    if (!b.on) change.on = true;
    if (EQ_GAINED.has(b.kind)) change.gain_db = Math.round(yToDb(p.y) * 2) / 2;
    setBand(strip, n, change);
  });
  const end = () => { t.dragging = false; t.heldUntil = performance.now() + 1500; };
  g.addEventListener("pointerup", end);
  g.addEventListener("pointercancel", end);
  g.addEventListener("focus", () => { t.selected = n; paintTone(strip); });
  g.addEventListener("dblclick", () => setBand(strip, n, { gain_db: 0 }, true));
  g.addEventListener("keydown", (e) => {
    const b = t.bands.find((x) => x.band === n);
    const step = e.shiftKey ? 4 : 1;
    let change = null;
    if (e.key === "ArrowLeft") change = { freq_hz: Math.max(EQ_MIN_HZ, Math.round(b.freq_hz / Math.pow(2, step / 12))) };
    if (e.key === "ArrowRight") change = { freq_hz: Math.min(EQ_MAX_HZ, Math.round(b.freq_hz * Math.pow(2, step / 12))) };
    if (e.key === "ArrowUp" && EQ_GAINED.has(b.kind)) change = { gain_db: Math.min(EQ_RANGE_DB, b.gain_db + 0.5 * step) };
    if (e.key === "ArrowDown" && EQ_GAINED.has(b.kind)) change = { gain_db: Math.max(-EQ_RANGE_DB, b.gain_db - 0.5 * step) };
    if (!change) return;
    e.preventDefault();
    setBand(strip, n, change);
  });
}

function bandText(b) {
  const words = [hzText(b.freq_hz)];
  if (EQ_GAINED.has(b.kind)) words.push(dbText(b.gain_db));
  if (b.kind === "bell") words.push(`Q ${b.q.toFixed(2)}`);
  return words.join(" · ");
}

function paintTone(strip) {
  const t = strip._r.more.tone;
  if (!t.bands) return;
  const path = curvePath(t.bands, t.w);
  t.curve.setAttribute("d", path);
  t.fill.setAttribute("d", `${path}L${t.w},${dbToY(0)}L0,${dbToY(0)}Z`);
  for (const g of t.handles.children) {
    const b = t.bands.find((x) => x.band === g._band);
    const y = EQ_GAINED.has(b.kind) ? dbToY(b.gain_db) : dbToY(0);
    g.setAttribute("transform", `translate(${hzToX(b.freq_hz, t.w).toFixed(1)},${y.toFixed(1)})`);
    g.classList.toggle("is-off", !b.on);
    g.classList.toggle("is-selected", b.band === t.selected);
    g.setAttribute("aria-label", `Band ${b.band}, ${EQ_LABELS[b.kind]}${b.on ? "" : ", off"}`);
    g.setAttribute("aria-valuetext", bandText(b));
  }
  const b = t.bands.find((x) => x.band === t.selected) || t.bands[0];
  t.bandName.textContent = `Band ${b.band}`;
  if (document.activeElement !== t.typeSel) t.typeSel.value = String(b.type_index);
  t.onBox.checked = b.on;
  t.readout.textContent = bandText(b);
}

function deviceRow(strip, name, index) {
  const row = el("div", "device");
  const remove = el("button", "device-x");
  remove.type = "button";
  remove.setAttribute("aria-label", `Remove ${name}`);
  remove.addEventListener("click", async () => {
    if (!await ask(`Remove ${name}?`, `${name} comes off ${strip._row.name}. Undo in Ableton brings it back.`, "Remove")) return;
    liveCmd("delete_device", { ...target(strip), device_index: index }).catch(() => {});
  });
  row.append(el("span", "device-idx", String(index + 1)), el("span", "device-name", name), remove);
  return row;
}

function sendControl(strip, returnIndex) {
  const control = el("div", "control send-level");
  control.innerHTML = `<span class="control-label"></span><input type="range" min="0" max="1000" step="1"><output></output>`;
  const input = $("input", control);
  input.setAttribute("aria-label", `Send ${String.fromCharCode(65 + returnIndex)}`);
  // the slider is a position, so its thumb matches the percentage it reads
  slider(input, $("output", control),
    (pos) => ({ cmd: "set_send", args: { ...target(strip), return_index: returnIndex, db: posToDb(pos / 1000) } }),
    (pos) => sendText(posToDb(pos / 1000)));
  return control;
}

async function loadRouting(strip) {
  const line = strip._r.more.routeLine;
  line.replaceChildren(el("span", "", "Plays to"), el("span", "", "Loading…"));
  let routing;
  try {
    routing = await api("/api/live", { cmd: "get_routing", args: target(strip) }).then((d) => d.result);
  } catch (e) {
    line.replaceChildren();
    toast(e.message, "error");
    return;
  }
  const out = routing.output;
  line.replaceChildren();
  const pick = el("div", "pick");

  const typeSel = select(out.types, out.type, {
    "Master": "The room (main speakers)", "Ext. Out": "Another output (in-ears, etc.)", "Sends Only": "Only the shared effects",
  });
  typeSel.setAttribute("aria-label", "Output type");
  typeSel.addEventListener("change", async () => {
    await liveCmd("set_routing", { ...target(strip), direction: "output", type_name: typeSel.value }).catch(() => {});
    loadRouting(strip);
  });
  pick.append(typeSel);

  const channels = out.channels.filter(Boolean);
  if (channels.length > 1 || (channels.length === 1 && out.type !== "Master")) {
    const chanSel = select(channels, out.channel);
    chanSel.setAttribute("aria-label", "Output channel");
    chanSel.addEventListener("change", () => liveCmd("set_routing", {
      ...target(strip), direction: "output", type_name: typeSel.value, channel_name: chanSel.value,
    }).catch(() => {}));
    pick.append(chanSel);
  }
  line.append(el("span", "", "Plays to"), pick);
}

function select(options, current, labels = {}) {
  const sel = document.createElement("select");
  for (const option of options) {
    const o = document.createElement("option");
    o.value = option;
    o.textContent = labels[option] || option;
    o.selected = option === current;
    sel.append(o);
  }
  return sel;
}

// Load the output routing when a channel is shown in the drawer.
const drawerWatcher = new MutationObserver(() => {
  const strip = strips.get(selectedKey);
  if (!strip || !$("#drawer").contains(strip._more)) return;
  const routeKey = `${strip._row?.output?.type || ""}|${strip._row?.output?.channel || ""}`;
  if (strip._routeKey !== routeKey) {
    strip._routeKey = routeKey;
    loadRouting(strip);
  }
});
drawerWatcher.observe($("#drawer"), { childList: true });

// -- add-effect dialog -------------------------------------------------------

let dialogStrip = null;

async function openDeviceDialog(strip) {
  dialogStrip = strip;
  $("#device-track").textContent = strip._row.name;
  const sel = $("#device-select");
  if (!stockDevices) {
    try {
      stockDevices = (await api("/api/devices")).devices;
    } catch (e) {
      toast(e.message, "error");
      return;
    }
  }
  sel.replaceChildren();
  const common = Object.keys(DEVICE_HELP).filter((n) => stockDevices?.audio_effects?.includes(n));
  if (common.length) {
    const group = document.createElement("optgroup");
    group.label = "Common";
    for (const name of common) group.append(new Option(name, name));
    sel.append(group);
  }
  const groups = { audio_effects: "Every effect", instruments: "Instruments", midi_effects: "MIDI effects" };
  for (const [key, label] of Object.entries(groups)) {
    const names = (stockDevices?.[key] || []).filter((n) => !common.includes(n));
    if (!names.length) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const name of names) group.append(new Option(name, name));
    sel.append(group);
  }
  paintDeviceHelp();
  await loadPresets();
  $("#device-dialog").returnValue = "";
  $("#device-dialog").showModal();
}

async function loadPresets() {
  const presetSel = $("#preset-select");
  presetSel.replaceChildren(new Option("Default settings", ""));
  const device = $("#device-select").value;
  if (!device) return;
  try {
    const { presets } = await api("/api/presets?device=" + encodeURIComponent(device));
    for (const p of presets) presetSel.append(new Option(p, p));
  } catch {
    /* presets are optional */
  }
}

/* What the common effects do, in a sentence. */
const DEVICE_HELP = {
  "Reverb": "Adds space, as if the singer were in a bigger room.",
  "Compressor": "Evens out loud and quiet moments.",
  "EQ Eight": "Adjusts bass, middle and treble.",
  "Limiter": "Stops sudden loud spikes.",
  "Echo": "Repeats the sound, like a delay.",
  "Gate": "Cuts background noise when nobody is playing.",
};

function paintDeviceHelp() {
  $("#device-help").textContent = DEVICE_HELP[$("#device-select").value] || "";
}

$("#device-select").addEventListener("change", () => { paintDeviceHelp(); loadPresets(); });

$("#device-dialog").addEventListener("close", async () => {
  if ($("#device-dialog").returnValue !== "add" || !dialogStrip) return;
  const device = $("#device-select").value;
  const preset = $("#preset-select").value || null;
  const trackName = dialogStrip._row.name;
  toast(`Adding ${device} to ${trackName}…`);
  try {
    await liveCmd("load_device", { ...target(dialogStrip), device_name: device, preset });
    toast(`Added ${preset ? `${device} (${preset})` : device} to ${trackName}.`);
  } catch {
    /* liveCmd already said why */
  }
});

// -- colour picker -------------------------------------------------------------

let colorStrip = null;

function openColorDialog(strip) {
  colorStrip = strip;
  $("#color-track").textContent = strip._row.name;
  const box = $("#swatches");
  const nameBox = $("#swatch-name");
  nameBox.textContent = " ";
  box.replaceChildren(...Object.entries(state.colors).map(([name, hex]) => {
    const swatch = button("", "swatch", async () => {
      $("#color-dialog").close();
      const rgb = parseInt(hex.slice(1), 16);
      await liveCmd("set_track_color", { ...target(colorStrip), rgb }).catch(() => {});
    });
    swatch.style.setProperty("--track", hex);
    swatch.setAttribute("aria-label", name);
    if (strip._row.color != null && parseInt(hex.slice(1), 16) === strip._row.color) swatch.setAttribute("aria-current", "true");
    const label = () => { nameBox.textContent = name; };
    swatch.addEventListener("mouseenter", label);
    swatch.addEventListener("focus", label);
    return swatch;
  }));
  $("#color-dialog").showModal();
}

// -- import stems -------------------------------------------------------------

let importPath = null;

async function openImportDialog() {
  if (sending !== null || state?.busy) {
    toast("Still working on the last message. One moment.");
    return;
  }
  $("#import-dialog").showModal();
  await showFolder(importPath);
}

async function showFolder(path) {
  const list = $("#folder-list");
  list.replaceChildren(el("span", "muted", "Looking…"));
  let folder;
  try {
    folder = await api("/api/folders" + (path ? "?path=" + encodeURIComponent(path) : ""));
  } catch (e) {
    list.replaceChildren(el("span", "muted", e.message));
    return;
  }
  importPath = folder.path;
  $("#folder-path").textContent = /^~\/?\.?$/.test(folder.display) ? "Home" : folder.display;
  $("#folder-path").title = folder.path;
  $("#folder-up").disabled = !folder.parent;
  $("#folder-up").onclick = () => showFolder(folder.parent);

  $("#import-roots").replaceChildren(...folder.roots.map((r) =>
    Object.assign(button(r.name, "root" + (r.path === folder.path ? " active" : ""), () => showFolder(r.path)),
      r.path === folder.path ? { ariaCurrent: "true" } : {})));

  list.replaceChildren(...folder.folders.map((f) => {
    const li = document.createElement("li");
    const b = button("", "folder", () => showFolder(f.path));
    b.append(el("span", "folder-name", f.name));
    if (f.audio) b.append(el("span", "folder-count", `${f.audio} file${f.audio === 1 ? "" : "s"}`));
    li.append(b);
    return li;
  }));
  if (!folder.folders.length) list.replaceChildren(el("span", "muted", "No folders in here."));

  const n = folder.audio.length;
  $("#folder-files").textContent = n
    ? `${n} audio file${n === 1 ? "" : "s"} here: ${folder.audio.slice(0, 6).join(", ")}${n > 6 ? "…" : ""}`
    : "No audio files directly in this folder (folders inside it are included).";
}

$("#import-btn").addEventListener("click", openImportDialog);

$("#import-go").addEventListener("click", async () => {
  if (!importPath) return;
  const go = $("#import-go");
  go.disabled = true;
  go.textContent = "Listening to your files…";
  const note = $("#import-note").value;
  const name = importPath.split("/").pop();
  startSending(`Import the song files in “${name}”.` + (note.trim() ? " " + note.trim() : ""));
  renderChat();
  $("#import-dialog").close();
  try {
    render(await api("/api/import", { folder: importPath, note }));
    $("#import-note").value = "";
  } catch (e) {
    toast(e.message, "error");
  } finally {
    sending = null;
    chatSig = "";
    if (state) renderChat();
    go.disabled = false;
    go.textContent = "Import song files";
  }
});

// -- songs: a cue list ---------------------------------------------------------

function playingScenes(snap) {
  const out = new Set();
  for (const t of snap.tracks) for (const c of t.clips || []) if (c.is_playing) out.add(c.scene_index);
  return out;
}

function renderSongs(snap) {
  const list = $("#songs");
  $("#songs-empty").hidden = snap.scenes.length > 0;
  if (list.contains(document.activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
  const playing = playingScenes(snap);
  const sig = JSON.stringify([snap.scenes, [...playing]]);
  if (sig === songsSig) return;
  const first = !list.childElementCount && !songsShown;
  songsShown = true;
  // rows are rebuilt, so they're followed by song name: a moved song travels to its new place
  const known = new Set([...list.children].map((li) => li._song));
  flip(list, ".song", (li) => li._song, () => {
    list.replaceChildren(...snap.scenes.map((scene) => songRow(scene, playing.has(scene.index))));
  });
  if (!first) [...list.children].filter((li) => !known.has(li._song)).forEach((li, i) => enter(li, i * 50));
}

let songsShown = false;

function songRow(scene, isPlaying) {
  const li = el("li", "song");
  li._song = scene.name || `#${scene.index}`;
  li.classList.toggle("playing", isPlaying);
  li.append(el("span", "song-num", String(scene.index + 1)));

  const titleBox = el("div", "song-title");
  const name = el("input", "song-name");
  name.setAttribute("aria-label", `Title of song ${scene.index + 1}`);
  name.placeholder = "Name this song";
  name.value = scene.name;
  const fit = () => { if (!CSS.supports("field-sizing", "content")) name.style.width = Math.max(8, name.value.length + 1) + "ch"; };
  fit();
  name.addEventListener("input", fit);
  name.addEventListener("change", () => liveCmd("set_scene", { scene_index: scene.index, name: name.value.trim() }).catch(() => {}));
  titleBox.append(name, el("span", "song-leader"));
  li.append(titleBox);

  const bpm = el("input", "song-bpm");
  bpm.type = "number";
  bpm.inputMode = "decimal";
  bpm.min = "20"; bpm.max = "999";
  bpm.placeholder = "—";
  bpm.setAttribute("aria-label", `Tempo of ${scene.name || "song " + (scene.index + 1)}`);
  bpm.value = scene.tempo ?? "";
  bpm.addEventListener("change", () => {
    const value = parseFloat(bpm.value);
    if (value >= 20 && value <= 999) liveCmd("set_scene", { scene_index: scene.index, bpm: value }).catch(() => {});
  });
  const bpmCell = el("div", "song-bpm-cell");
  bpmCell.append(bpm, el("span", "bpm-unit", "BPM"));
  li.append(bpmCell);

  li.append(transposer(scene));

  const start = el("button", "key start");
  start.type = "button";
  start.innerHTML = isPlaying
    ? "<span>Playing</span>"
    : '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 1v10l9-5z"/></svg><span>Start</span>';
  // Starting a song here also puts the mixer on its mix.
  start.addEventListener("click", () => liveCmd("fire_scene", { scene_index: scene.index }).then(poll, () => {}));
  start.setAttribute("aria-label", isPlaying ? `${scene.name || "Song " + (scene.index + 1)}, playing` : `Start ${scene.name || "song " + (scene.index + 1)}`);
  if (isPlaying) li.setAttribute("aria-current", "true");
  li.append(start);
  return li;
}

/* Key: the minus and plus keys move every audio clip in the song a semitone; the middle key goes
   back to the original key. A song off its original key is the one lit key in the list, so nobody
   forgets it on Sunday. Clips that disagree read "Mixed keys" until a key is picked. */
function transposer(scene) {
  const box = el("div", "transpose");
  // An older RigLink doesn't report keys; Live needs a restart before this can work.
  if (!("transpose" in scene)) {
    box.hidden = true;
    return box;
  }
  const name = scene.name || "song " + (scene.index + 1);
  const down = button(MINUS, "key key-step", () => set((key ?? 0) - 1));
  const value = button("", "key key-value", () => set(0));
  const up = button("+", "key key-step", () => set((key ?? 0) + 1));
  down.setAttribute("aria-label", `Transpose ${name} down a semitone`);
  up.setAttribute("aria-label", `Transpose ${name} up a semitone`);
  value.title = "Raises or lowers every part in the song. Press to go back to the original key.";
  let key = scene.transpose;
  const show = () => {
    const n = Math.abs(key ?? 0);
    const text = key === null ? "Mixed keys" : key === 0 ? "Original key" : `${key > 0 ? "Up" : "Down"} ${n} half step${n === 1 ? "" : "s"}`;
    value.textContent = text;
    value.setAttribute("aria-label", `Key of ${name}: ${text}. Press to go back to the original key.`);
    box.classList.toggle("shifted", key !== 0);
    down.disabled = key !== null && key <= -12;
    up.disabled = key !== null && key >= 12;
  };
  // Show the new key straight away, so a quick second press counts from it.
  const set = (n) => {
    if (n < -12 || n > 12 || n === key) return;
    const before = key;
    key = n;
    show();
    liveCmd("transpose_song", { scene_index: scene.index, semitones: n })
      .then((res) => { if (res && !res.clips) { toast("That song has no audio clips to transpose."); key = before; show(); } })
      .catch(() => { key = before; show(); });
  };
  show();
  box.append(down, value, up);
  return box;
}

$("#add-song").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#song-name").value.trim();
  const bpm = parseFloat($("#song-bpm").value);
  try {
    await liveCmd("create_scene", { name, bpm: bpm >= 20 ? bpm : null });
    $("#song-name").value = "";
    $("#song-bpm").value = "";
    songsSig = "";
    poll();
  } catch {
    /* liveCmd already said why */
  }
});

// -- room memory: a ledger -------------------------------------------------------

let roomSig = "";

let roomShown = false;

function renderRoom() {
  const facts = state.room || [];
  const list = $("#facts");
  const sig = JSON.stringify(facts);
  if (sig === roomSig) return;
  const known = roomShown ? new Set([...list.children].map((li) => li._fact)) : null;
  roomShown = true;
  roomSig = sig;
  $("#facts-empty").hidden = facts.length > 0;
  list.replaceChildren(...facts.map((f) => {
    const li = el("li", "fact");
    li._fact = f.id;
    if (known && !known.has(f.id)) enter(li);
    li.append(el("span", "fact-date", shortDate(f.added)), el("span", "fact-text", f.text));
    const forget = button("Forget", "text-btn", async () => {
      try {
        render(await api("/api/room", { remove: f.id }));
      } catch (e) {
        toast(e.message, "error");
      }
    });
    forget.setAttribute("aria-label", `Forget: ${f.text}`);
    li.append(forget);
    return li;
  }));
}

$("#add-fact").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#fact-text").value.trim();
  if (!text) return;
  const before = (state.room || []).length;
  try {
    const next = await api("/api/room", { add: text });
    render(next);
    if ((next.room || []).length > before) $("#fact-text").value = "";
    else toast(before >= 60 ? "Room is full (60 notes). Forget one first." : "That is already saved.", "error");
  } catch (err) {
    toast(err.message, "error");
  }
});

// -- views -------------------------------------------------------------------

let currentTab = "mixer";

function setTab(tab) {
  const changed = tab !== currentTab;
  if (changed) $("#pane-session").scrollTop = 0;
  currentTab = tab;
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === tab;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
  });
  if (state) renderLive();
  if (changed) enter($(`#panel-${tab}`), 0, "translateY(6px)");
}

function setView(view) {
  const was = $("#layout").dataset.view;
  const changed = was !== undefined && (was === "chat") !== (view === "chat");   // only the phone swaps panes
  $("#layout").dataset.view = view;
  if (changed) enter($(view === "chat" ? "#pane-chat" : "#pane-session"), 0, "translateY(6px)");
  $$(".bottom-nav button").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  if (view !== "chat") setTab(view);
  chatSig = "";
  if (state) renderChat();
  paintActivity();
}

$$(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
$(".tabs").addEventListener("keydown", (e) => {
  const tabs = $$(".tab");
  const at = tabs.findIndex((t) => t.getAttribute("aria-selected") === "true");
  const next = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  const tab = tabs[(next + tabs.length) % tabs.length];
  setTab(tab.dataset.tab);
  tab.focus();
});
$$(".bottom-nav button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

// -- toasts ------------------------------------------------------------------

function toast(message, kind = "info") {
  const node = el("div", "toast " + kind, message);
  $("#toasts").append(node);
  enter(node, 0, "translateX(24px)");
  let timer = setTimeout(() => leave(node), kind === "error" ? 8000 : 4000);
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", () => { timer = setTimeout(() => leave(node), 2500); });
}

listen();
poll();
pollMeters();
