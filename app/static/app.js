/* Holy Sound front end. No framework, no build step: this file is served as-is. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const POLL_MS = 1500;
const POLL_HIDDEN_MS = 5000;

let state = null;
let sending = null;           // text of the message in flight, shown optimistically
let chatSig = "";
let songsSig = "";
let stockDevices = null;
const strips = new Map();     // "t0" / "r1" -> strip element
const holding = new WeakSet(); // controls the user is touching: polling won't move them

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
  pollTimer = setTimeout(poll, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

function render(next) {
  state = next;
  renderLive();
  renderAi();
  renderChat();
}

// -- header ------------------------------------------------------------------

function setStatus(kind, text, detail = "") {
  const el = $("#live-status");
  el.className = "status " + kind;
  $(".status-text", el).textContent = text;
  $(".status-detail", el).textContent = detail;
}

function renderLive() {
  const live = state.live;
  const snap = live.snapshot;
  if (live.connected) {
    const n = snap.tracks.length;
    setStatus("ok", "Live connected", `· ${n} track${n === 1 ? "" : "s"}`);
  } else {
    setStatus("bad", "Live not connected");
  }

  $("#transport").hidden = !live.connected;
  if (snap) {
    const play = $("#play-btn");
    play.classList.toggle("playing", snap.song.is_playing);
    play.setAttribute("aria-label", snap.song.is_playing ? "Stop" : "Play");
    const tempo = $("#tempo-input");
    if (document.activeElement !== tempo) tempo.value = Math.round(snap.song.tempo * 100) / 100;
  }

  $("#offline").hidden = live.connected;
  // The standard "can't reach Live" message just repeats the steps below it.
  const reason = live.message || "";
  $("#offline-reason").textContent = reason.includes("Control Surface") ? "" : reason;
  $("#panel-mixer").hidden = !live.connected || currentTab !== "mixer";
  $("#panel-songs").hidden = !live.connected || currentTab !== "songs";
  if (snap) {
    renderMixer(snap);
    renderSongs(snap);
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

// -- chat --------------------------------------------------------------------

function renderAi() {
  const banner = $("#ai-banner");
  banner.hidden = state.ai.ready;
  banner.textContent = state.ai.message || "";
}

function renderChat() {
  const sig = JSON.stringify([state.chat, state.busy, sending, state.live.connected]);
  if (sig === chatSig) return;
  chatSig = sig;

  const box = $("#messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  const welcome = $("#welcome");
  box.replaceChildren(welcome);
  welcome.hidden = state.chat.length > 0 || sending !== null;

  for (const entry of state.chat) {
    if (entry.text) box.append(bubble(entry.role, entry.text));
    if (entry.proposal) box.append(proposalCard(entry.proposal));
  }
  if (sending !== null) {
    const pending = bubble("user", sending);
    pending.classList.add("pending");
    box.append(pending);
  }
  if (sending !== null || state.busy) {
    const dots = document.createElement("div");
    dots.className = "thinking";
    dots.setAttribute("aria-label", "Thinking");
    dots.innerHTML = "<span></span><span></span><span></span>";
    box.append(dots);
  }
  if (nearBottom || sending !== null) box.scrollTop = box.scrollHeight;
  $("#composer .send").disabled = sending !== null || state.busy;
}

function bubble(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  if (role === "assistant") el.innerHTML = markdownLite(text);
  else el.textContent = text;
  return el;
}

const STATUS_LABELS = {
  pending: "",
  applied: "Applied",
  dismissed: "Not applied",
  superseded: "Replaced by a newer suggestion",
  exported: "Downloaded as a session file",
};

function proposalCard(p) {
  const card = document.createElement("div");
  card.className = "proposal " + p.status;

  const head = document.createElement("div");
  head.className = "proposal-head";
  const problems = (p.results || []).filter((r) => !r.ok || r.partial).length;
  head.innerHTML = `<span>${p.status === "pending" ? "Suggested changes" : "Changes"}</span><span class="state"></span>`;
  $(".state", head).textContent = p.status === "applied" && problems
    ? `Applied · ${problems} need${problems === 1 ? "s" : ""} a look`
    : STATUS_LABELS[p.status];
  card.append(head);

  const list = document.createElement("ol");
  list.className = "steps";
  p.steps.forEach((step, i) => {
    const li = document.createElement("li");
    const result = p.results?.[i];
    li.textContent = result ? result.text : step.text;
    if (result) li.className = !result.ok ? "fail" : result.partial ? "partial" : "ok";
    if (!result && step.destructive) {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "removes";
      li.append(tag);
    }
    list.append(li);
  });
  card.append(list);

  if (p.status === "pending") {
    const connected = state.live.connected;
    const actions = document.createElement("div");
    actions.className = "proposal-actions";

    const apply = button("Apply", "btn primary", () => act(p.id, "apply", apply));
    apply.disabled = !connected;
    if (!connected) apply.title = "Open Live to apply these";
    const later = button("Not now", "btn ghost", () => act(p.id, "dismiss", later));
    actions.append(apply);
    if (p.exportable) {
      const dl = button("Download session file", connected ? "btn" : "btn primary", () => exportProposal(p.id, dl));
      actions.append(dl);
    }
    actions.append(later);
    card.append(actions);

    if (!connected) {
      const note = document.createElement("p");
      note.className = "proposal-note";
      note.textContent = "Live isn’t connected, so these can’t be applied yet." +
        (p.exportable ? " You can download the new tracks as a session file instead." : "");
      card.append(note);
    }
  }
  return card;
}

function button(label, className, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = className;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

async function act(id, verb, btn) {
  btn.disabled = true;
  const original = btn.textContent;
  if (verb === "apply") btn.textContent = "Applying…";
  try {
    render(await api(`/api/proposals/${id}/${verb}`, {}));
    poll();
  } catch (e) {
    toast(e.message, "error");
    btn.disabled = false;
    btn.textContent = original;
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
  sending = text;
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
  input.style.height = Math.min(input.scrollHeight, 180) + "px";
}
$("#message-input").addEventListener("input", autosize);

$("#chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip) send(chip.textContent);
});

$("#reset-btn").addEventListener("click", async () => {
  if (!confirm("Start a new conversation? Your Live set stays as it is.")) return;
  try {
    render(await api("/api/reset", {}));
  } catch (e) {
    toast(e.message, "error");
  }
});

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

// -- mixer -------------------------------------------------------------------

function renderMixer(snap) {
  syncStrips($("#strips"), snap.tracks, "t", snap);
  syncStrips($("#returns"), snap.returns, "r", snap);
  $("#returns-label").hidden = snap.returns.length === 0;
  $("#mixer-empty").hidden = snap.tracks.length > 0;
}

function syncStrips(container, rows, prefix, snap) {
  const keep = new Set();
  rows.forEach((row, i) => {
    const key = prefix + row.index;
    keep.add(key);
    let el = strips.get(key);
    if (!el) {
      el = createStrip();
      strips.set(key, el);
    }
    if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
    updateStrip(el, row, snap);
  });
  for (const [key, el] of strips) {
    if (key.startsWith(prefix) && !keep.has(key)) {
      el.remove();
      strips.delete(key);
    }
  }
}

function target(el) {
  return { track_index: el._row.index, is_return: el._row.is_return };
}

function createStrip() {
  const el = $("#strip-template").content.firstElementChild.cloneNode(true);

  const name = $(".strip-name", el);
  name.addEventListener("change", () => {
    const value = name.value.trim();
    if (value && value !== el._row.name) liveCmd("set_track_name", { ...target(el), name: value }).catch(() => {});
  });
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); });

  $(".mute", el).addEventListener("click", () => liveCmd("set_mute", { ...target(el), on: !el._row.mute }).catch(() => {}));
  $(".solo", el).addEventListener("click", () => liveCmd("set_solo", { ...target(el), on: !el._row.solo }).catch(() => {}));

  slider($(".volume input", el), $(".volume output", el), (db) => ({ cmd: "set_volume", args: { ...target(el), db } }), dbText);
  slider($(".pan input", el), $(".pan output", el), (pan) => ({ cmd: "set_pan", args: { ...target(el), pan } }), panText);

  $(".more", el).addEventListener("toggle", (e) => { if (e.target.open) loadRouting(el); });
  return el;
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
    paintFill(input);
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

function paintFill(input) {
  const min = parseFloat(input.min), max = parseFloat(input.max);
  input.style.setProperty("--fill", ((parseFloat(input.value) - min) / (max - min)) * 100 + "%");
}

function dbText(db) {
  return db <= -70 ? "−inf dB" : `${db > 0 ? "+" : ""}${db.toFixed(1)} dB`;
}

function panText(pan) {
  const amount = Math.round(Math.abs(pan) * 50);
  return amount === 0 ? "C" : `${amount}${pan < 0 ? "L" : "R"}`;
}

function panFromText(text) {
  const m = /^(\d+)([LR])$/.exec(text || "");
  return m ? (parseInt(m[1], 10) / 50) * (m[2] === "L" ? -1 : 1) : 0;
}

function routeLabel(side, isInput) {
  if (!side) return "";
  if (side.type === "No Input") return "No input";
  if (side.type === "Ext. In") return `Input ${side.channel}`;
  if (side.type === "Ext. Out") return `Outputs ${side.channel}`;
  return side.channel && !isInput ? `${side.type} ${side.channel}` : side.type;
}

function updateStrip(el, row, snap) {
  el._row = row;
  el.classList.toggle("is-return", row.is_return);
  el.classList.toggle("muted-track", row.mute);
  $(".strip-num", el).textContent = row.is_return ? String.fromCharCode(65 + row.index) : row.index + 1;

  const name = $(".strip-name", el);
  if (document.activeElement !== name) name.value = row.name;

  const route = row.is_return
    ? `→ ${routeLabel(row.output)}`
    : `${row.is_midi ? "MIDI · " : ""}${routeLabel(row.input, true)} → ${routeLabel(row.output)}`;
  $(".strip-route", el).textContent = route;

  $(".mute", el).setAttribute("aria-pressed", row.mute);
  $(".solo", el).setAttribute("aria-pressed", row.solo);

  const vol = $(".volume input", el);
  if (!holding.has(vol)) {
    vol.value = row.volume_db ?? -70;
    paintFill(vol);
    $(".volume output", el).textContent = row.volume.replace("-", "−");
  }
  const pan = $(".pan input", el);
  if (!holding.has(pan)) {
    pan.value = row.pan_value ?? panFromText(row.pan);
    $(".pan output", el).textContent = row.pan;
  }

  const devSig = JSON.stringify(row.devices);
  const devices = $(".devices", el);
  if (devices._sig !== devSig) {
    devices._sig = devSig;
    devices.replaceChildren(...row.devices.map((d, i) => deviceChip(el, d, i)));
    devices.append(button("+ Effect", "add-device", () => openDeviceDialog(el)));
  }

  const sends = $(".sends", el);
  if (sends.children.length !== row.sends.length) {
    sends.replaceChildren(...row.sends.map((s, i) => sendControl(el, i)));
  }
  row.sends.forEach((s, i) => {
    const control = sends.children[i];
    $(".control-label", control).textContent = s.return;
    $(".control-label", control).title = s.return;
    const input = $("input", control);
    if (!holding.has(input)) {
      input.value = s.level_db ?? -70;
      paintFill(input);
      $("output", control).textContent = s.level.replace("-", "−");
    }
  });
  $(".more summary", el).textContent = row.sends.length ? "Sends & outputs" : "Outputs";
}

function deviceChip(el, name, index) {
  const chip = document.createElement("span");
  chip.className = "device";
  chip.textContent = name;
  const remove = button("×", "", () => {
    if (!confirm(`Remove ${name} from ${el._row.name}? (Cmd+Z in Live brings it back.)`)) return;
    liveCmd("delete_device", { ...target(el), device_index: index }).catch(() => {});
  });
  remove.setAttribute("aria-label", `Remove ${name}`);
  chip.append(remove);
  return chip;
}

function sendControl(el, returnIndex) {
  const control = document.createElement("div");
  control.className = "control send-level";
  control.innerHTML = `<span class="control-label"></span><input type="range" min="-70" max="6" step="0.5"><output></output>`;
  const input = $("input", control);
  input.setAttribute("aria-label", `Send ${String.fromCharCode(65 + returnIndex)}`);
  slider(input, $("output", control), (db) => ({ cmd: "set_send", args: { ...target(el), return_index: returnIndex, db } }), dbText);
  return control;
}

async function loadRouting(el) {
  const line = $(".route-line", el);
  line.innerHTML = "<span>Output</span><span>Loading…</span>";
  let routing;
  try {
    routing = await api("/api/live", { cmd: "get_routing", args: target(el) }).then((d) => d.result);
  } catch (e) {
    line.innerHTML = "";
    toast(e.message, "error");
    return;
  }
  const out = routing.output;
  line.replaceChildren();
  const label = document.createElement("span");
  label.textContent = "Output";
  const pick = document.createElement("div");
  pick.style.display = "flex";
  pick.style.gap = "8px";

  const typeSel = select(out.types, out.type);
  typeSel.setAttribute("aria-label", "Output type");
  typeSel.addEventListener("change", async () => {
    await liveCmd("set_routing", { ...target(el), direction: "output", type_name: typeSel.value }).catch(() => {});
    loadRouting(el);
  });
  pick.append(typeSel);

  const channels = out.channels.filter(Boolean);
  if (channels.length > 1 || (channels.length === 1 && out.type !== "Master")) {
    const chanSel = select(channels, out.channel);
    chanSel.setAttribute("aria-label", "Output channel");
    chanSel.addEventListener("change", () => liveCmd("set_routing", {
      ...target(el), direction: "output", type_name: typeSel.value, channel_name: chanSel.value,
    }).catch(() => {}));
    pick.append(chanSel);
  }
  line.append(label, pick);
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

// -- add-effect dialog -------------------------------------------------------

let dialogStrip = null;

async function openDeviceDialog(el) {
  dialogStrip = el;
  $("#device-track").textContent = el._row.name;
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

// -- songs -------------------------------------------------------------------

function renderSongs(snap) {
  const list = $("#songs");
  if (list.contains(document.activeElement)) return;
  const sig = JSON.stringify(snap.scenes);
  if (sig === songsSig) return;
  songsSig = sig;
  list.replaceChildren(...snap.scenes.map(songRow));
}

function songRow(scene) {
  const li = document.createElement("li");
  li.className = "song";
  li.innerHTML = `<span class="song-num"></span><input class="song-name" aria-label="Song title" placeholder="Untitled"><input class="song-bpm" type="number" inputmode="decimal" min="20" max="999" placeholder="BPM" aria-label="Tempo">`;
  $(".song-num", li).textContent = scene.index + 1;
  const name = $(".song-name", li);
  const bpm = $(".song-bpm", li);
  name.value = scene.name;
  bpm.value = scene.tempo ?? "";
  name.addEventListener("change", () => liveCmd("set_scene", { scene_index: scene.index, name: name.value.trim() }).catch(() => {}));
  bpm.addEventListener("change", () => {
    const value = parseFloat(bpm.value);
    if (value >= 20 && value <= 999) liveCmd("set_scene", { scene_index: scene.index, bpm: value }).catch(() => {});
  });
  const start = button("", "btn start", () => liveCmd("fire_scene", { scene_index: scene.index }).catch(() => {}));
  start.innerHTML = "▶<span> Start</span>";
  start.setAttribute("aria-label", `Start ${scene.name || "song " + (scene.index + 1)}`);
  li.append(start);
  return li;
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

// -- views -------------------------------------------------------------------

let currentTab = "mixer";

function setTab(tab) {
  currentTab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", t.dataset.tab === tab));
  if (state) renderLive();
}

function setView(view) {
  $("#layout").dataset.view = view;
  document.querySelectorAll(".bottom-nav button").forEach((b) => {
    if (b.dataset.view === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  if (view !== "chat") setTab(view);
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => setTab(t.dataset.tab)));
document.querySelectorAll(".bottom-nav button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

// -- toasts ------------------------------------------------------------------

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 5000);
}

poll();
