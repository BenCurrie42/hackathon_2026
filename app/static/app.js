/* Holy Sound front end. No framework, no build step: this file is served as-is. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const POLL_MS = 1500;
const POLL_PLAYING_MS = 600;   // meters move while a song plays
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

// -- polling ------------------------------------------------------------------

let pollTimer = null;

async function poll() {
  clearTimeout(pollTimer);
  try {
    render(await api("/api/state"));
  } catch (e) {
    setStatus("bad", e.message);
  }
  const playing = state?.live.snapshot?.song.is_playing;
  const wait = document.hidden ? POLL_HIDDEN_MS
    : applyingId !== null || state?.applying ? POLL_APPLYING_MS
    : playing ? POLL_PLAYING_MS : POLL_MS;
  pollTimer = setTimeout(poll, wait);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

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
  node.className = "status " + kind;
  $(".status-text", node).textContent = text;
  $(".status-detail", node).textContent = detail;
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
    if (document.activeElement !== tempo) tempo.value = Math.round(snap.song.tempo * 100) / 100;
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
    node.addEventListener("keydown", (e) => { if (e.key === "Enter") setView("room"); });
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
    if (p && (p.status === "pending" || p.id === applyingId)) found = p;
  }
  return found;
}

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
  welcome.hidden = state.chat.length > 0 || sending !== null;
  if (!welcome.hidden) paintWelcome();

  for (const entry of state.chat) {
    if (entry.role === "note" || entry.role === "heard") {
      box.append(eventLine(entry.role, entry.text || ""));
      continue;
    }
    if (entry.text || entry.thinking) {
      const node = turn(entry.role, entry.text || "");
      if (entry.thinking) node.insertBefore(thoughts(entry.thinking, entry.id), $(".turn-text", node));
      box.append(node);
    }
    if (entry.proposal && entry.proposal !== docked && entry.proposal.id !== docked?.id) {
      box.append(slipEl(entry.proposal, false));
    }
  }
  if (pendingText !== null) {
    const node = turn("user", pendingText);
    node.classList.add("pending");
    box.append(node);
  }
  if (reply) {
    box.append(reply.el);
  } else if (sending !== null || state.busy) {
    box.append(working("Working…"));
  }
  if (!welcome.hidden) box.scrollTop = 0;
  else if (nearBottom || sending !== null) box.scrollTop = box.scrollHeight;

  const dock = $("#slip-dock");
  dock.replaceChildren(...(docked ? [slipEl(docked, true)] : []));
  if (docked) {
    const steps = $(".steps", dock);
    if (steps && dock._lastSlip !== docked.id) steps.scrollTop = 0;
    dock._lastSlip = docked.id;
  }

  $("#composer .send").disabled = sending !== null || state.busy;
  $("#import-btn").disabled = sending !== null || state.busy;
  $("#reset-btn").hidden = state.chat.length === 0 && sending === null;
  $("#nav-chat-mark").hidden = !(docked && docked.status === "pending" && $("#layout").dataset.view !== "chat");
}

function paintWelcome() {
  $("#welcome-date").textContent = nextSundayLabel();
  const facts = [...(state.room || [])].sort((a, b) => b.id - a.id).slice(0, 2);
  const line = $("#welcome-saved");
  line.hidden = facts.length === 0;
  if (facts.length) $(".welcome-saved-text", line).textContent = "Saved in Room: " + facts.map((f) => f.text).join(" ");
}

// Collapsed by default: a volunteer wants the answer, the reasoning is there if they're curious.
function thoughts(text, id) {
  const node = el("details", "thoughts");
  node.innerHTML = '<summary>Show thinking</summary><div class="thought-text"></div>';
  $(".thought-text", node).textContent = text;
  const summary = $("summary", node);
  if (id !== undefined) node.open = openThoughts.has(id);
  summary.textContent = node.open ? "Hide thinking" : "Show thinking";
  node.addEventListener("toggle", () => {
    summary.textContent = node.open ? "Hide thinking" : "Show thinking";
    if (id !== undefined) node.open ? openThoughts.add(id) : openThoughts.delete(id);
  });
  return node;
}

// -- the reply being written -------------------------------------------------
// /api/events streams the assistant's reply as it's written. The finished turn
// arrives through /api/state as usual, so on "end" this entry just goes away.

const TOOL_LABELS = {
  listen_to_stems: "Listening to the stems…",
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
    if (!reply.text && !reply.tool) summary.textContent = "Thinking…";
    else summary.textContent = think.open ? "Hide thinking" : "Show thinking";
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

function stepRow(step, i, mode, p) {
  const li = el("li", "step");
  li.append(el("span", "step-num", String(i + 1)));
  const body = el("div", "step-body");
  body.append(el("p", "step-text", trueMinus(step.text)));
  if (step.change) {
    const ch = el("p", "step-change");
    const from = el("s");
    from.innerHTML = numHtml(trueMinus(String(step.change.from)));
    const to = el("span");
    to.innerHTML = numHtml(trueMinus(String(step.change.to))) + (step.change.unit ? " " + escapeHtml(step.change.unit) : "");
    ch.append(from);
    ch.insertAdjacentHTML("beforeend", ARROW_SVG);
    ch.append(to);
    body.append(ch);
  }
  const note = consequence(step);
  if (note && mode === "pending") body.append(el("p", "step-note " + note.kind, note.text));
  li.append(body);

  if (mode === "pending") return li;

  // applying or finished: the state of this step, in words and a lamp
  const result = p.results?.[i];
  let stateName;
  let reason = "";
  if (result) {
    stateName = !result.ok ? "failed" : result.partial ? "check" : "done";
    if (stateName === "failed") reason = result.text;
    else if (stateName === "check") reason = result.text.replace(/^.*? But /, "But ");
  } else {
    stateName = state.applying?.id === p.id ? state.applying.states[i] || "waiting" : "waiting";
  }
  const words = { waiting: "Waiting", working: "Working", done: "Done", check: "Check", failed: "Failed" };
  const status = el("span", "step-state " + stateName);
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
    exported: "Downloaded as a session file",
  }[p.status];
  if (collapsedWords) {
    const node = el("div", "slip collapsed");
    const line = el("div", "slip-collapsed");
    line.append(el("span", "", collapsedWords));
    const list = el("ol", "steps");
    list.hidden = true;
    p.steps.forEach((step, i) => list.append(stepRow(step, i, "done", { results: [] })));
    const toggle = button("Show steps", "text-btn", () => {
      list.hidden = !list.hidden;
      toggle.textContent = list.hidden ? "Show steps" : "Hide steps";
    });
    line.append(toggle);
    node.append(line, list);
    return node;
  }

  const applying = p.status === "pending" && p.id === applyingId;
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
  node.append(head);

  if (p.status === "pending" && !applying) {
    node.append(el("p", "slip-sub", state.demo
      ? "Nothing in the demo set changes until you press Apply."
      : "Nothing in Ableton changes until you press Apply."));
  }

  const list = el("ol", "steps");
  const mode = p.status === "pending" && !applying ? "pending" : "done";
  p.steps.forEach((step, i) => list.append(stepRow(step, i, mode, p)));
  node.append(list);

  if (mode === "done" && total > 4 && !docked) {
    const rows = [...list.children];
    rows.slice(4).forEach((r) => { r.hidden = true; });
    const more = button("Show all", "text-btn", () => {
      const hidden = rows[4].hidden;
      rows.slice(4).forEach((r) => { r.hidden = !hidden; });
      more.textContent = hidden ? "Show fewer" : "Show all";
    });
    node.append(more);
  }

  if (p.status === "pending" && !applying) {
    const connected = state.live.connected;
    const actions = el("div", "slip-actions");
    if (connected) {
      const apply = button(`Apply ${noun}`, "key big on-paper", () => act(p.id, "apply", apply));
      actions.append(apply);
    } else {
      if (p.exportable) {
        const dl = button("Download session file", "key big on-paper", () => exportProposal(p.id, dl));
        actions.append(dl);
      }
      actions.append(el("p", "slip-line disabled", "Apply (Ableton isn’t open)"));
      actions.append(el("p", "slip-note", "Ableton isn’t open, so Apply is off." +
        (p.exportable ? " Download the new tracks as a session file instead." : "")));
    }
    const sub = el("div", "sub-row");
    if (connected && p.exportable) {
      const dl = button("Download session file", "text-btn", () => exportProposal(p.id, dl));
      sub.append(dl);
    } else {
      sub.append(el("span"));
    }
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
    toast("Downloaded. Double-click it to open in Live." + (notes.length ? " Note: " + notes.join(" ") : ""));
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

$("#try-example").addEventListener("click", () => {
  const input = $("#message-input");
  input.value = "Lead vocal on input 1, click to the in-ears.";
  autosize();
  input.focus();
});

$("#open-room").addEventListener("click", () => setView("room"));

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Start over? Your Ableton set stays as it is.")) return;
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

function posToDb(pos) {
  if (pos < 0.012) return -70;
  if (pos >= 1) return 6;
  for (let i = 1; i < LAW.length; i++) {
    if (pos <= LAW[i][1]) {
      const [d0, p0] = LAW[i - 1];
      const [d1, p1] = LAW[i];
      const db = d0 + ((pos - p0) / (p1 - p0)) * (d1 - d0);
      const half = Math.round(db * 2) / 2;
      return Math.abs(half) <= 1 ? 0 : half;   // a detent at unity
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
  const amount = Math.round(Math.abs(pan) * 50);
  return amount === 0 ? "Centre" : `${pan < 0 ? "Left" : "Right"} ${amount}`;
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
  $("#sr-status").textContent = `Editing ${name}`;

  const rects = $$(".run rect", strip);
  const w = strip.offsetWidth, h = strip.offsetHeight;
  rects.forEach((r) => {
    r.setAttribute("x", 1.5); r.setAttribute("y", 1.5);
    r.setAttribute("width", Math.max(1, w - 3)); r.setAttribute("height", Math.max(1, h - 3));
    r.getAnimations?.().forEach((a) => a.cancel());
    r.style.strokeDasharray = "";
    r.style.display = "";
  });
  const perimeter = rects[0].getTotalLength?.() || 2 * (w + h);
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
  $("#sr-status").textContent = `Changed ${name}`;

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

function renderMixer(snap) {
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
      strip.remove();
      strips.delete(key);
    }
  }

  $("#mixer-empty").hidden = snap.tracks.length > 0;
  keepSelection(groups);
  paintActivity();
}

/* One entry per folder, in the server's order. Empty folders stay in the
   list (hidden until a drag starts) so there's somewhere to drop a track.
   Shared effects (return tracks) are a trailing folder you can't drop into. */
function buildMixerGroups(snap) {
  const folders = state.folders || [];
  const known = new Set(folders.map((f) => f.key));
  const byKey = new Map(folders.map((f) => [f.key, []]));
  for (const row of snap.tracks) {
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
    bus.setAttribute("aria-label", `${folded ? "Show" : "Hide"} ${nameEl.textContent}`);
  };
  apply(loadCollapsedGroups().has(key));
  bus.addEventListener("click", () => {
    const folded = !section.classList.contains("folded");
    apply(folded);
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

/* Live can't show folders, so give every track its folder's colour there. */
$$(".match-colors").forEach((btn) => btn.addEventListener("click", async () => {
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
  toast(`Matched ${done} track colour${done === 1 ? "" : "s"} in Live to their folders.`);
}));

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
    }
    if (container.children[i] !== strip) container.insertBefore(strip, container.children[i] || null);
    updateStrip(strip, row, snap);
  });
}

function target(strip) {
  return { track_index: strip._row.index, is_return: strip._row.is_return };
}

// -- selecting a channel and the drawer ------------------------------------------

function keepSelection(groups) {
  const order = groups.flatMap((g) => g.rows.map((r) => g.prefix + r.index));
  if (!order.length) {
    selectedKey = null;
    $("#drawer").replaceChildren(el("p", "drawer-empty", "Pick a channel number to see its effects, sends and routing."));
    return;
  }
  if (!selectedKey || !strips.has(selectedKey)) {
    const old = selectedKey;
    const oldIndex = old ? parseInt(old.slice(1), 10) : 0;
    selectedKey = order.find((k) => k.startsWith("t") && parseInt(k.slice(1), 10) >= oldIndex) || order[0];
  }
  for (const [key, strip] of strips) {
    const on = key === selectedKey;
    strip.classList.toggle("is-selected", on);
    strip.setAttribute("aria-current", on ? "true" : "false");
  }
  const strip = strips.get(selectedKey);
  const drawer = $("#drawer");
  if (drawer.firstElementChild !== strip._more) drawer.replaceChildren(strip._more);
}

function selectStrip(strip, scroll = false) {
  selectedKey = strip._key;
  keepSelection(buildMixerGroups(state.live.snapshot));
  if (scroll && matchMedia("(max-width: 820px)").matches) {
    $("#drawer").scrollIntoView({ block: "nearest", behavior: reducedMotion.matches ? "auto" : "smooth" });
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
  const balance = el("div", "control balance");
  balance.innerHTML = '<span class="control-label">Balance</span><input type="range" min="-1" max="1" step="0.02" aria-label="Balance, left to right"><output></output>';
  sound.append(el("h3", "", "Sound"), keys, balance);

  const effects = el("section", "m-effects");
  const devices = el("div", "devices");
  const addDevice = button("Add effect", "add-device", () => openDeviceDialog(strip));
  const sends = el("div", "sends");
  effects.append(el("h3", "", "Effects"), devices, addDevice, el("h3", "again", "Reverb and delay"), sends);

  const routing = el("section", "m-routing");
  const folderLine = el("label", "folder-line");
  const folderSel = el("select", "folder-select");
  folderSel.setAttribute("aria-label", "Folder");
  folderLine.append(el("span", "", "Folder"), folderSel);
  const routeLine = el("div", "route-line");
  const clipGains = el("div", "clip-gains");
  routing.append(el("h3", "", "Where it plays"), folderLine, routeLine, clipGains);

  more.append(channel, sound, effects, routing);
  strip._more = more;
  return {
    title, name, summary, colourChip: chip, mute, solo,
    balance, balanceInput: $("input", balance), balanceOut: $("output", balance),
    devices, addDevice, sends, folderLine, folderSel, routeLine, clipGains,
  };
}

function createStrip() {
  const strip = $("#strip-template").content.firstElementChild.cloneNode(true);
  const m = buildMore(strip);
  const r = {
    plate: $(".plate", strip), name: $(".strip-name", strip), note: $(".strip-note", strip),
    mute: $(".mute", strip), zone: $(".fader-zone", strip), fader: $(".fader", strip),
    ghost: $(".ghost", strip), meter: $(".meter", strip), more: m,
  };
  strip._r = r;

  // selecting: tap anywhere on the channel except the fader and Mute
  strip.addEventListener("click", (e) => {
    if (e.target.closest(".mute, .fader-zone")) return;
    selectStrip(strip, true);
  });
  strip.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === strip) { e.preventDefault(); selectStrip(strip, true); }
  });

  // naming happens in the drawer
  const rename = () => {
    const value = m.name.value.trim();
    if (value && value !== strip._row.name) liveCmd("set_track_name", { ...target(strip), name: value }).catch(() => {});
  };
  m.name.addEventListener("change", rename);
  m.name.addEventListener("keydown", (e) => { if (e.key === "Enter") m.name.blur(); });

  const toggleMute = () => liveCmd("set_mute", { ...target(strip), on: !strip._row.mute }).catch(() => {});
  r.mute.addEventListener("click", toggleMute);
  m.mute.addEventListener("click", toggleMute);
  m.solo.addEventListener("click", () => liveCmd("set_solo", { ...target(strip), on: !strip._row.solo }).catch(() => {}));
  slider(m.balanceInput, m.balanceOut, (pan) => ({ cmd: "set_pan", args: { ...target(strip), pan } }), panText);

  wireFader(strip);

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
  m.folderSel.addEventListener("change", (e) => moveTrack(strip, e.target.value));
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
  const up = () => strip.classList.remove("is-down");
  input.addEventListener("pointerup", up);
  input.addEventListener("pointercancel", up);
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
  input.addEventListener("keydown", (e) => {
    const step = { ArrowUp: 0.5, ArrowRight: 0.5, ArrowDown: -0.5, ArrowLeft: -0.5, PageUp: 3, PageDown: -3 }[e.key];
    if (step === undefined) return;
    e.preventDefault();
    holding.add(input);
    const db = Math.min(6, Math.max(-70, (strip._lastDb ?? currentDb()) + step));
    apply(db <= -69.5 ? -70 : db, true);
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
  input.addEventListener("pointerdown", () => holding.add(input));
  input.addEventListener("input", () => {
    holding.add(input);
    output.textContent = format(parseFloat(input.value));
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
  if (row.is_return) return "A shared effect. Other channels send to it.";
  const input = row.is_midi ? "MIDI instrument"
    : !row.input || row.input.type === "No Input" ? "Playback track"
    : row.input.type === "Ext. In" ? `Input ${row.input.channel}` : row.input.type;
  const out = row.output;
  const output = !out || out.type === "Master" ? "Plays in the room"
    : out.type === "Ext. Out" ? `Plays to outputs ${out.channel}`
    : out.type === "Sends Only" ? "Only feeds the shared effects" : `Plays to ${out.type}`;
  return `${input}. ${output}.`;
}

/* The line under a channel's name: what the assistant is doing to it, then that it did it. */
function paintNote(strip) {
  const note = strip._r.note;
  if (strip._running) note.textContent = "Editing\u2026";
  else if (strip._changedUntil && performance.now() < strip._changedUntil) note.textContent = "Changed";
  else note.textContent = "";
}

function updateStrip(strip, row, snap) {
  const r = strip._r;
  const m = r.more;
  strip._row = row;
  strip.classList.toggle("is-return", row.is_return);
  strip.classList.toggle("is-muted", row.mute);
  if (row.color != null) {
    const hex = "#" + row.color.toString(16).padStart(6, "0");
    strip.style.setProperty("--track", hex);
    strip._more.style.setProperty("--track", hex);
  }

  const number = row.is_return ? String.fromCharCode(65 + row.index) : String(row.index + 1);
  if (r.name.textContent !== row.name) r.name.textContent = row.name;
  strip.setAttribute("aria-label", `${row.name}, ${row.is_return ? "shared effect " : "track "}${number}`);
  if (document.activeElement !== m.name) m.name.value = row.name;
  m.title.textContent = `Channel ${number}`;
  m.summary.textContent = describeRouting(row);
  paintNote(strip);

  // Return tracks (shared effects) aren't sorted into instrument folders.
  r.plate.draggable = !row.is_return;
  m.folderLine.hidden = row.is_return;
  const folders = state.folders || [];
  const folderSig = folders.map((f) => f.key + f.label).join("|");
  if (m.folderSel._sig !== folderSig) {
    m.folderSel._sig = folderSig;
    m.folderSel.replaceChildren(...folders.map((f) => new Option(f.label, f.key)));
  }
  if (!row.is_return && document.activeElement !== m.folderSel) m.folderSel.value = row.folder;

  for (const key of [r.mute, m.mute]) key.setAttribute("aria-pressed", row.mute);
  m.solo.setAttribute("aria-pressed", row.solo);
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
  if (!holding.has(m.balanceInput)) {
    m.balanceInput.value = String(pan);
    m.balanceOut.textContent = panText(pan);
  }

  const devSig = JSON.stringify(row.devices);
  if (m.devices._sig !== devSig) {
    m.devices._sig = devSig;
    m.devices.replaceChildren(...row.devices.map((d, i) => deviceRow(strip, d, i)));
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
    if (!holding.has(input)) {
      input.value = s.level_db ?? -70;
      $("output", control).textContent = sendText(s.level_db);
    }
  });
  updateClips(strip, row, snap);
}

/* Live's meters run 0-1 (after the fader). Peak since the last poll, shown in n segments. */
function paintMeter(meter, reading) {
  if (!meter) return;
  meter.hidden = reading == null;
  meter.style.setProperty("--lit", String(Math.min(1, reading?.peak ?? 0)));
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
    input.value = clip.gain_db ?? -24;
    $("output", control).textContent = trueMinus(clip.gain || "");
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

function deviceRow(strip, name, index) {
  const row = el("div", "device");
  const remove = el("button", "device-x");
  remove.type = "button";
  remove.setAttribute("aria-label", `Remove ${name}`);
  remove.addEventListener("click", () => {
    if (!confirm(`Remove ${name} from ${strip._row.name}? (Cmd+Z in Live brings it back.)`)) return;
    liveCmd("delete_device", { ...target(strip), device_index: index }).catch(() => {});
  });
  row.append(el("span", "device-idx", String(index + 1)), el("span", "device-name", name), remove);
  return row;
}

function sendControl(strip, returnIndex) {
  const control = el("div", "control send-level");
  control.innerHTML = `<span class="control-label"></span><input type="range" min="-70" max="6" step="0.5"><output></output>`;
  const input = $("input", control);
  input.setAttribute("aria-label", `Send ${String.fromCharCode(65 + returnIndex)}`);
  slider(input, $("output", control), (db) => ({ cmd: "set_send", args: { ...target(strip), return_index: returnIndex, db } }), sendText);
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

  const typeSel = select(out.types, out.type);
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

function select(options, current) {
  const sel = document.createElement("select");
  for (const option of options) {
    const o = document.createElement("option");
    o.value = o.textContent = option;
    o.selected = option === current;
    sel.append(o);
  }
  return sel;
}

// Load the output routing when a channel is shown in the drawer.
const drawerWatcher = new MutationObserver(() => {
  const strip = strips.get(selectedKey);
  if (strip && $("#drawer").contains(strip._more) && strip._routedFor !== selectedKey + "|" + (strip._row?.output?.type || "")) {
    strip._routedFor = selectedKey + "|" + (strip._row?.output?.type || "");
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
  const groups = { audio_effects: "Effects", instruments: "Instruments", midi_effects: "MIDI effects" };
  for (const [key, label] of Object.entries(groups)) {
    const names = stockDevices?.[key];
    if (!names?.length) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const name of names) {
      const o = document.createElement("option");
      o.value = o.textContent = name;
      group.append(o);
    }
    sel.append(group);
  }
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

$("#device-select").addEventListener("change", loadPresets);

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
  $("#folder-path").textContent = folder.display;
  $("#folder-path").title = folder.path;
  $("#folder-up").disabled = !folder.parent;
  $("#folder-up").onclick = () => showFolder(folder.parent);

  $("#import-roots").replaceChildren(...folder.roots.map((r) =>
    button(r.name, "root" + (r.path === folder.path ? " active" : ""), () => showFolder(r.path))));

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
  startSending(`Import the audio in “${name}”.` + (note.trim() ? " " + note.trim() : ""));
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
    go.textContent = "Import stems";
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
  if (list.contains(document.activeElement)) return;
  const playing = playingScenes(snap);
  const sig = JSON.stringify([snap.scenes, [...playing]]);
  if (sig === songsSig) return;
  songsSig = sig;
  list.replaceChildren(...snap.scenes.map((scene) => songRow(scene, playing.has(scene.index))));
}

function songRow(scene, isPlaying) {
  const li = el("li", "song");
  li.classList.toggle("playing", isPlaying);
  li.append(el("span", "song-num", String(scene.index + 1)));

  const titleBox = el("div", "song-title");
  const name = el("input", "song-name");
  name.setAttribute("aria-label", "Song title");
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
  bpm.setAttribute("aria-label", "Tempo");
  bpm.value = scene.tempo ?? "";
  bpm.addEventListener("change", () => {
    const value = parseFloat(bpm.value);
    if (value >= 20 && value <= 999) liveCmd("set_scene", { scene_index: scene.index, bpm: value }).catch(() => {});
  });
  li.append(bpm);

  li.append(transposer(scene));

  const start = el("button", "key start");
  start.type = "button";
  start.innerHTML = isPlaying
    ? "<span>Playing</span>"
    : '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 1v10l9-5z"/></svg><span>Start</span>';
  start.addEventListener("click", () => liveCmd("fire_scene", { scene_index: scene.index }).catch(() => {}));
  start.setAttribute("aria-label", `Start ${scene.name || "song " + (scene.index + 1)}`);
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
  value.title = "Back to the original key";
  let key = scene.transpose;
  const show = () => {
    const text = key === null ? "Mixed keys" : key === 0 ? "Original key" : (key > 0 ? "Up " : "Down ") + Math.abs(key);
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

function renderRoom() {
  const facts = state.room || [];
  const list = $("#facts");
  const sig = JSON.stringify(facts);
  if (sig === roomSig) return;
  roomSig = sig;
  $("#facts-empty").hidden = facts.length > 0;
  list.replaceChildren(...facts.map((f) => {
    const li = el("li", "fact");
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
  try {
    render(await api("/api/room", { add: text }));
    $("#fact-text").value = "";
  } catch (err) {
    toast(err.message, "error");
  }
});

// -- views -------------------------------------------------------------------

let currentTab = "mixer";

function setTab(tab) {
  currentTab = tab;
  $$(".tab").forEach((t) => {
    const on = t.dataset.tab === tab;
    t.setAttribute("aria-selected", on);
    t.tabIndex = on ? 0 : -1;
  });
  if (state) renderLive();
}

function setView(view) {
  $("#layout").dataset.view = view;
  $$(".bottom-nav button").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  if (view !== "chat") setTab(view);
  chatSig = "";
  if (state) renderChat();
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
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}

listen();
poll();
