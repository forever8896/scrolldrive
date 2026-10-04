// Frameline studio: one screen at a time. Pitch -> questions -> brief -> storyboard ->
// keyframes (approve, redo) -> film (approve) -> deliver with the words editable live.
// The server keeps the session on disk; reloading the page picks up exactly where it was.
import { $, api, usd, enter, reduceMotion, store, EASE } from "/static/common.js";

const STEPS = ["brief", "board", "keyframes", "film", "deliver"];
const PHASE_STEP = { pitch: "brief", questions: "brief", confirm: "brief", board: "board",
  shooting: "keyframes", keyframes: "keyframes", filming: "film", deliver: "deliver" };
const PHASE_SCENE = { pitch: "pitch", questions: "questions", confirm: "confirm", board: "board",
  shooting: "board", keyframes: "board", filming: "board", deliver: "deliver" };
const TYPES = [["product", "Product"], ["portfolio", "Portfolio"], ["brand", "Brand"], ["event", "Event"], ["experimental", "Strange"]];
const STYLES = [["", "Director's choice"], ["photoreal", "Photoreal"], ["editorial", "Editorial"], ["surreal", "Surreal"],
  ["graphic", "Graphic"], ["noir", "Noir"], ["dreamy", "Dreamy"]];
const EXAMPLE = { text: "Ember, a handmade ceramic pour-over dripper from a two-person studio in Porto. Warm, slow, early-morning light, the calm before the day. For people who weigh their coffee beans. The site should make them want to hold it.", photo: "/runs/nightfall/product-photo.jpg" };
const SID_KEY = "frameline-session";

let V = null;            // the server's view of this session
let prices = null;
let events = null;       // live render stream
let shownScene = null;

/* ---------------- motion helpers ---------------- */
// Headline words rise out of a blur, one after another.
function kinetic(el, text) {
  el.textContent = "";
  el.setAttribute("aria-label", text);
  text.split(/(\s+)/).forEach((part, i) => {
    if (/^\s+$/.test(part)) return el.append(part);
    const w = document.createElement("span");
    w.className = "w";
    w.setAttribute("aria-hidden", "true");
    w.textContent = part;
    el.append(w);
    enter(w, { opacity: 0, transform: "translateY(0.55em)", filter: "blur(10px)" }, { duration: 900, delay: 60 + i * 28 });
  });
}

function stagger(nodes, from = { opacity: 0, transform: "translateY(14px)" }, base = 200, step = 55) {
  [...nodes].forEach((n, i) => enter(n, from, { delay: base + i * step, duration: 700 }));
}

function leave(el) {
  if (reduceMotion || !el) return Promise.resolve();
  return el.animate([{ opacity: 1, transform: "none", filter: "blur(0)" },
    { opacity: 0, transform: "translateY(-28px)", filter: "blur(8px)" }], { duration: 420, easing: EASE, fill: "forwards" }).finished;
}

function bignum(text) {
  const el = $("#bignum");
  if (el.textContent === text) return;
  el.textContent = text;
  enter(el, { opacity: 0, transform: "translateY(40px)" }, { duration: 1200 });
}

/* ---------------- the agent's voice ---------------- */
let sayTimer = null;
function say(text, tone = "") {
  const line = $("#agent-line");
  const out = $("#agent-text");
  line.classList.toggle("warn", tone === "warn");
  clearInterval(sayTimer);
  if (reduceMotion) { out.textContent = text; return; }
  out.textContent = "";
  let i = 0;
  sayTimer = setInterval(() => {
    i += 2;
    out.textContent = text.slice(0, i);
    if (i >= text.length) clearInterval(sayTimer);
  }, 14);
}

/* ---------------- stepper and scenes ---------------- */
function setStep(phase) {
  const idx = STEPS.indexOf(PHASE_STEP[phase] || "brief");
  document.querySelectorAll("#stepper li").forEach((li, i) => {
    li.classList.toggle("done", i < idx);
    li.classList.toggle("active", i === idx);
    li.setAttribute("aria-current", i === idx ? "step" : "false");
  });
  $("#stepper-fill").style.transform = `scaleX(${idx / (STEPS.length - 1)})`;
}

function showScene(name) {
  document.querySelectorAll(".scene").forEach((s) => {
    const on = s.dataset.scene === name;
    if (on && s.hidden) {
      s.hidden = false;
      enter(s, { opacity: 0, transform: "translateY(20px) scale(0.99)", filter: "blur(6px)" }, { duration: 800 });
    } else if (!on) s.hidden = true;
  });
  shownScene = name;
}

/* ---------------- render the whole view ---------------- */
function render(v, { quiet = false } = {}) {
  if (V && v.board && V.board && JSON.stringify(v.board.copy) !== JSON.stringify(V.board.copy)) copy = null; // the director changed the words
  V = v;
  renderChat();
  schedulePoll();
  store.set(SID_KEY, v.id);
  setStep(v.phase);
  const scene = PHASE_SCENE[v.phase];
  const changed = scene !== shownScene;
  showScene(scene);
  if (scene === "pitch") renderPitch(changed);
  else if (readTimer) stopReading();
  if (scene === "questions") renderQuestions();
  if (scene === "confirm") renderConfirm(changed);
  if (scene === "board") renderBoard(changed);
  if (scene === "deliver") renderDeliver(changed);
  if (v.error && !quiet) say(`That stopped: ${v.error}`, "warn");
  if (v.job?.running) follow();
}

/* ---------------- 1. pitch ---------------- */
function renderPitch(changed) {
  if (V.agent?.busy) {  // the director is reading the pitch
    if ($("#reading").hidden) { $(".pitch").hidden = true; showReading(V.pitch || ""); }
    return;
  }
  stopReading();
  $("#reading").hidden = true;
  $(".pitch").hidden = false;
  $(".pitch").getAnimations().forEach((a) => a.cancel());
  if (V.pitch && !$("#pitch-input").value) $("#pitch-input").value = V.pitch;
  renderRefs();
  bignum("");
  if (changed) {
    kinetic($("#pitch-title"), "What are we making?");
    stagger(document.querySelectorAll("#starters .chip"), undefined, 500, 45);
    say("Tell me what you want to make. Add images if your subject should look exactly right.");
    setTimeout(() => $("#pitch-input").focus({ preventScroll: true }), 300);
  }
}

function renderRefs() {
  const wrap = $("#refs");
  wrap.querySelectorAll(".ref").forEach((n) => n.remove());
  V.refs.forEach((url, i) => {
    const d = document.createElement("div");
    d.className = "ref";
    d.innerHTML = `<img alt=""><button type="button" aria-label="Remove image"><i class="ph ph-x"></i></button>`;
    d.querySelector("img").src = url;
    d.querySelector("button").addEventListener("click", async () => render(await api("/api/ref/remove", { session: V.id, index: i }), { quiet: true }));
    wrap.insertBefore(d, $("#ref-add"));
  });
  $("#ref-add").hidden = V.refs.length >= 6;
  $("#ref-add span").textContent = V.refs.length ? "Add" : "Add images";
}

async function uploadFiles(files) {
  for (const f of [...files].slice(0, 6 - V.refs.length)) {
    if (!f.type.startsWith("image/")) { say("That file is not an image. JPG, PNG or WebP work.", "warn"); continue; }
    try { V = await api(`/api/upload?session=${V.id}`, f); }
    catch (e) { say(`Upload failed: ${e.message}`, "warn"); }
  }
  renderRefs();
  const last = $("#refs .ref:last-of-type img");
  if (last) enter(last.parentElement, { opacity: 0, transform: "scale(0.8)" });
}

$("#ref-input").addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
["dragenter", "dragover"].forEach((t) => $("#pitch-box").addEventListener(t, (e) => { e.preventDefault(); $("#pitch-box").classList.add("dragging"); }));
["dragleave", "drop"].forEach((t) => $("#pitch-box").addEventListener(t, (e) => { e.preventDefault(); $("#pitch-box").classList.remove("dragging"); }));
$("#pitch-box").addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));

document.querySelectorAll("#starters [data-start]").forEach((b) => b.addEventListener("click", () => {
  const input = $("#pitch-input");
  input.value = b.dataset.start;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}));

$("#example-btn").addEventListener("click", async () => {
  $("#pitch-input").value = EXAMPLE.text;
  if (!V.refs.length) {
    const blob = await fetch(EXAMPLE.photo).then((r) => r.blob());
    await uploadFiles([new File([blob], "ember.jpg", { type: "image/jpeg" })]);
  }
});

$("#pitch-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("#pitch-go").click(); }
});

// The pitch goes to the director, who reads it, asks only what it must, and drafts the storyboard.
$("#pitch-go").addEventListener("click", async () => {
  const pitch = $("#pitch-input").value.trim();
  if (pitch.length < 3) { say("Give me a sentence or two to work with.", "warn"); $("#pitch-input").focus(); return; }
  await leave($(".pitch"));
  $(".pitch").hidden = true;
  showReading(pitch);
  say(V.refs.length ? `Reading your pitch and studying ${V.refs.length === 1 ? "your image" : `your ${V.refs.length} images`}.` : "Reading your pitch.");
  try {
    render(await api("/api/chat", { session: V.id, text: pitch }), { quiet: true });
    openChat(true);
  } catch (e) {
    stopReading();
    $(".pitch").hidden = false;
    $(".pitch").getAnimations().forEach((a) => a.cancel());
    say(`I could not read that just now: ${e.message}`, "warn");
  }
});

// While the producer reads: the pitch appears word by word and a highlight scans across it.
let readTimer = null;
function showReading(text) {
  const el = $("#reading-text");
  el.textContent = "";
  const words = text.split(/\s+/).slice(0, 90);
  words.forEach((w) => { const s = document.createElement("span"); s.textContent = `${w} `; el.append(s); });
  $("#reading").hidden = false;
  stagger(el.children, { opacity: 0, filter: "blur(6px)" }, 0, 12);
  let i = 0;
  readTimer = setInterval(() => {
    el.children[i % words.length]?.classList.remove("lit");
    i += 1;
    el.children[i % words.length]?.classList.add("lit", "seen");
  }, reduceMotion ? 400 : 90);
}
function stopReading() { clearInterval(readTimer); $("#reading").hidden = true; }

/* ---------------- 2. questions ---------------- */
let qIndex = 0;
let answers = [];

function renderTray() {
  const tray = $("#tray");
  tray.innerHTML = "";
  (V.brief.understood || []).forEach((t) => {
    const c = document.createElement("span");
    c.className = "tray-chip";
    c.innerHTML = `<i class="ph ph-check"></i><span></span>`;
    c.querySelector("span").textContent = t;
    tray.append(c);
  });
  answers.forEach((a) => addTrayChip(a.answer, false));
  stagger(tray.children, { opacity: 0, transform: "translateY(-8px) scale(0.96)" }, 100, 60);
}

function addTrayChip(text, animate = true) {
  const c = document.createElement("span");
  c.className = "tray-chip answer";
  c.innerHTML = `<i class="ph ph-arrow-bend-down-right"></i><span></span>`;
  c.querySelector("span").textContent = text;
  $("#tray").append(c);
  if (animate) enter(c, { opacity: 0, transform: "scale(0.7)" }, { duration: 600 });
  return c;
}

function renderQuestions() {
  qIndex = 0;
  answers = [];
  renderTray();
  showQuestion();
}

function showQuestion() {
  const qs = V.brief.questions;
  const q = qs[qIndex];
  const wrap = $("#q-wrap");
  wrap.innerHTML = "";
  bignum(String(qIndex + 1).padStart(2, "0"));
  const card = document.createElement("div");
  card.className = "q";
  card.innerHTML = `
    <p class="eyebrow">Question ${qIndex + 1} of ${qs.length}</p>
    <h2 class="kinetic q-title"></h2>
    <div class="q-options"></div>
    <form class="q-own"><input placeholder="Or say it in your own words" aria-label="Your answer"><button class="send" aria-label="Answer"><i class="ph ph-arrow-right"></i></button></form>
    <button class="link-btn q-skip">Skip, you decide</button>`;
  wrap.append(card);
  kinetic(card.querySelector(".q-title"), q.question);
  const opts = card.querySelector(".q-options");
  if (q.kind === "images") {
    const drop = document.createElement("label");
    drop.className = "q-drop";
    drop.innerHTML = `<input type="file" accept="image/*" multiple hidden><i class="ph ph-images"></i><strong>Drop images or click to choose</strong><span>Up to 6</span>`;
    drop.querySelector("input").addEventListener("change", async (e) => {
      await uploadFiles(e.target.files);
      answer(`${V.refs.length} image${V.refs.length === 1 ? "" : "s"} added`, drop);
    });
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("dragging"); }));
    drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
    drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      await uploadFiles(e.dataTransfer.files);
      answer(`${V.refs.length} image${V.refs.length === 1 ? "" : "s"} added`, drop);
    });
    opts.append(drop);
  }
  q.options.forEach((o) => {
    const b = document.createElement("button");
    b.className = "option";
    b.textContent = o;
    b.addEventListener("click", () => answer(o, b));
    opts.append(b);
  });
  stagger(opts.children, { opacity: 0, transform: "translateY(18px) scale(0.97)" }, 380, 70);
  enter(card.querySelector(".q-own"), { opacity: 0 }, { delay: 700 });
  enter(card.querySelector(".q-skip"), { opacity: 0 }, { delay: 800 });
  card.querySelector(".q-own").addEventListener("submit", (e) => {
    e.preventDefault();
    const val = e.target.querySelector("input").value.trim();
    if (val) answer(val, e.target.querySelector("input"));
  });
  card.querySelector(".q-skip").addEventListener("click", () => answer("", null));
}

// The chosen answer flies up into the brief tray, then the next question rises.
async function answer(text, fromEl) {
  const q = V.brief.questions[qIndex];
  answers.push({ id: q.id, answer: text, question: q.question });
  if (text) {
    fromEl?.classList.add("picked");
    const chip = addTrayChip(text, false);
    if (fromEl && !reduceMotion) {
      const a = fromEl.getBoundingClientRect();
      const b = chip.getBoundingClientRect();
      chip.style.visibility = "hidden";
      const ghost = chip.cloneNode(true);
      ghost.classList.add("flying");
      ghost.style.visibility = "visible";
      Object.assign(ghost.style, { left: `${a.left}px`, top: `${a.top}px`, width: `${b.width}px` });
      document.body.append(ghost);
      await ghost.animate([
        { transform: "translate(0,0) scale(1.1)", opacity: 0.9 },
        { transform: `translate(${b.left - a.left}px, ${b.top - a.top}px) scale(1)`, opacity: 1 }],
      { duration: 650, easing: EASE }).finished;
      ghost.remove();
      chip.style.visibility = "";
    }
  }
  await leave($("#q-wrap .q"));
  qIndex += 1;
  if (qIndex < V.brief.questions.length) return showQuestion();
  $("#q-wrap").innerHTML = "";
  try {
    const v = await api("/api/answer", { session: V.id, answers });
    render(v);
    say("That is everything. Check the brief, set the length and quality, and I will direct it.");
  } catch (e) { say(`Could not save that: ${e.message}`, "warn"); }
}

/* ---------------- 3. confirm ---------------- */
function segmented(el, items, value, onPick) {
  el.innerHTML = "";
  items.forEach(([val, label]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(val === value));
    b.textContent = label;
    b.addEventListener("click", () => {
      el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", "false"));
      b.setAttribute("aria-checked", "true");
      onPick(val);
    });
    el.append(b);
  });
}

function priceFor(scenes, res) {
  return prices?.prices?.[res]?.[scenes] ?? null;
}

function updatePrice() {
  const c = V.controls;
  $("#len-val").textContent = `${c.scenes} moves, about ${c.scenes * 5} seconds`;
  const price = priceFor(c.scenes, c.resolution);
  if (price != null) $("#p-price").textContent = usd(price);
}

const FIELD_LABELS = [["name", "Name"], ["subject", "Subject"], ["mood", "Mood"], ["audience", "For"], ["goal", "Visitors should"]];
function renderConfirm(changed) {
  const b = V.brief;
  $("#directing").hidden = true;
  $(".confirm").hidden = false;
  $(".confirm").getAnimations().forEach((a) => a.cancel());
  bignum("");
  if (changed) kinetic($("#c-headline"), b.headline || "Your film");
  const rows = $("#brief-rows");
  rows.innerHTML = "";
  FIELD_LABELS.forEach(([k, label]) => {
    const d = document.createElement("div");
    d.className = "brief-row";
    d.innerHTML = `<dt></dt><dd contenteditable="plaintext-only" spellcheck="false"></dd>`;
    d.querySelector("dt").textContent = label;
    const dd = d.querySelector("dd");
    dd.textContent = b[k] || "";
    dd.dataset.key = k;
    dd.dataset.placeholder = k === "name" ? "No name yet" : "Your call";
    dd.addEventListener("input", () => { V.brief[k] = dd.textContent.trim(); });
    dd.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); dd.blur(); } });
    rows.append(d);
  });
  (V.answers || []).filter((a) => a.answer && !FIELD_LABELS.some(([k]) => k === a.field)).forEach((a) => {
    const d = document.createElement("div");
    d.className = "brief-row";
    d.innerHTML = "<dt>Also</dt><dd></dd>";
    d.querySelector("dd").textContent = a.answer;
    rows.append(d);
  });
  const refs = $("#c-refs");
  refs.innerHTML = "";
  V.refs.forEach((u) => { const i = new Image(); i.src = u; i.alt = ""; refs.append(i); });
  if (changed) stagger([...rows.children, ...refs.children], { opacity: 0, transform: "translateX(-12px)" }, 350, 60);

  const c = V.controls;
  segmented($("#ctl-type"), TYPES, c.type, (v) => { c.type = v; });
  segmented($("#ctl-style"), STYLES, c.style, (v) => { c.style = v; });
  segmented($("#ctl-res"), [["768P", "Standard 768p"], ["1080P", "High 1080p"]], c.resolution, (v) => { c.resolution = v; updatePrice(); });
  $("#ctl-scenes").value = c.scenes;
  updatePrice();
  if (changed) stagger(document.querySelectorAll(".controls > *"), { opacity: 0, transform: "translateY(12px)" }, 450, 50);
}
$("#ctl-scenes").addEventListener("input", (e) => { V.controls.scenes = Number(e.target.value); updatePrice(); });
$("#repitch-btn").addEventListener("click", async () => render(await api("/api/back", { session: V.id, to: "pitch" })));

$("#direct-btn").addEventListener("click", async () => {
  const btn = $("#direct-btn");
  btn.disabled = true;
  await leave($(".confirm"));
  $(".confirm").hidden = true;
  $("#directing").hidden = false;
  const n = V.controls.scenes + 1;
  kinetic($("#directing-title"), `Writing ${n} shots and ${n - 1} camera moves`);
  const gs = $("#ghost-strip");
  gs.innerHTML = "";
  for (let i = 0; i < n; i += 1) { const s = document.createElement("span"); s.style.setProperty("--i", i); gs.append(s); }
  stagger(gs.children, { opacity: 0, transform: "translateX(40px)" }, 300, 90);
  say(V.refs.length ? "The director is studying your images and writing every shot." : "The director is writing every shot.");
  try {
    const v = await api("/api/storyboard", { session: V.id, controls: V.controls, brief: V.brief });
    render(v);
    say(`Storyboard ready: ${v.board.keyframes.length} stills, ${v.board.moves.length} camera moves. Nothing is shot until you say so.`);
  } catch (e) {
    $("#directing").hidden = true;
    $(".confirm").hidden = false;
    $(".confirm").getAnimations().forEach((a) => a.cancel());
    say(`The director stumbled: ${e.message}. Try again.`, "warn");
  } finally { btn.disabled = false; }
});

/* ---------------- 4. board: storyboard, keyframes, film ---------------- */
function tile(kind, item) {
  const div = document.createElement("div");
  div.className = `frame ${kind}`;
  div.dataset.id = item.id;
  div.innerHTML = `<div class="media"></div><div class="label"></div><div class="prompt"></div>${kind === "move" ? '<div class="join"></div>' : ""}`;
  div.querySelector(".label").textContent = kind === "move" ? `Move ${item.from} to ${item.to}` : `Keyframe ${item.id}`;
  div.querySelector(".prompt").textContent = kind === "move"
    ? item.prompt.replace(/^One continuous slow camera move, no cuts:\s*/i, "")
    : item.prompt.replace(/^Keep the subject from reference[^.]*\.\s*/i, "");
  return div;
}

function setMedia(frame, state, url) {
  const media = frame.querySelector(".media");
  media.className = `media ${state}`;
  if (state === "planned") {
    const still = frame.classList.contains("still");
    media.innerHTML = `<i class="ph ${still ? "ph-aperture" : "ph-film-slate"}"></i><span>${still ? "Shot after approval" : "Filmed after approval"}</span>`;
  } else if (state === "pending") {
    media.innerHTML = "";
  } else if (state === "working") {
    media.innerHTML = `<i class="ph ph-film-slate"></i><span>Filming</span>`;
  } else if (state === "img") {
    if (media.querySelector("img")?.src.endsWith(url)) return;
    media.innerHTML = "";
    const img = new Image();
    img.alt = `Keyframe ${frame.dataset.id}`;
    img.onload = () => requestAnimationFrame(() => img.classList.add("shown"));
    img.src = url;
    media.append(img);
  } else if (state === "video") {
    media.innerHTML = "";
    const v = document.createElement("video");
    Object.assign(v, { src: url, muted: true, loop: true, playsInline: true, autoplay: !reduceMotion });
    media.append(v);
    enter(v, { opacity: 0 });
  }
}

function renderBoard(changed) {
  const b = V.board;
  const phase = V.phase;
  $("#board-title").textContent = b.copy.title || "Storyboard";
  $("#board-tagline").textContent = b.copy.tagline || "";
  $("#board-eyebrow").textContent = { board: "Storyboard", shooting: "Shooting keyframes", keyframes: "Keyframes", filming: "Filming" }[phase];
  bignum("");
  const strip = $("#strip");
  if (changed || strip.dataset.name !== b.name || strip.children.length !== b.keyframes.length + b.moves.length) {
    strip.innerHTML = "";
    strip.dataset.name = b.name;
    b.keyframes.forEach((k, i) => {
      strip.append(tile("still", k));
      if (b.moves[i]) strip.append(tile("move", b.moves[i]));
    });
    strip.style.gridTemplateColumns = [...strip.children].map((el) => (el.classList.contains("move") ? "minmax(170px, 1fr)" : "minmax(260px, 1.5fr)")).join(" ");
    stagger(strip.children, { opacity: 0, transform: "translateY(18px)" }, 200, 60);
  }
  b.keyframes.forEach((k) => {
    const f = strip.querySelector(`.frame[data-id="${k.id}"]`);
    if (k.url) setMedia(f, "img", k.url);
    else setMedia(f, phase === "shooting" ? "pending" : "planned");
    f.querySelector(".prompt").textContent = k.prompt.replace(/^Keep the subject from reference[^.]*\.\s*/i, "");
    redoControls(f, k);
  });
  b.moves.forEach((m) => {
    const f = strip.querySelector(`.frame[data-id="${m.id}"]`);
    if (m.url) setMedia(f, "video", m.url);
    else if (!f.querySelector(".media.working")) setMedia(f, "planned");
  });
  renderGate();
}

function redoControls(frame, k) {
  frame.querySelector(".redo")?.remove();
  frame.querySelector(".score")?.remove();
  const mark = (V.agent?.inspection?.shots || []).find((x) => x.id === k.id);
  if (mark && k.url && V.phase === "keyframes") {
    const b = document.createElement("span");
    b.className = `score${mark.score <= 6 ? " low" : ""}`;
    b.textContent = `Director ${mark.score}/10`;
    b.title = mark.issue || "No issues found";
    frame.querySelector(".media").append(b);
  }
  if (V.phase !== "keyframes" || !k.url) return;
  const r = document.createElement("div");
  r.className = "redo";
  const left = V.allowance?.reshoots ?? 0;
  r.innerHTML = `<button class="redo-btn" type="button"><i class="ph ph-arrow-clockwise"></i> Redo</button>`;
  const btn = r.querySelector("button");
  btn.disabled = left < 1;
  btn.title = left < 1 ? "No reshoots left for this film" : `${left} reshoot${left === 1 ? "" : "s"} left`;
  btn.addEventListener("click", () => openRedo(frame, k));
  frame.querySelector(".media").append(r);
}

function openRedo(frame, k) {
  document.querySelectorAll(".redo-panel").forEach((p) => p.remove());
  const p = document.createElement("form");
  p.className = "redo-panel";
  p.innerHTML = `<label>Change what you want in this shot, then reshoot it</label><textarea rows="6"></textarea>
    <div class="redo-actions"><button type="button" class="link-btn">Cancel</button>
    <button class="btn btn-primary btn-sm">Reshoot (${V.allowance?.reshoots ?? 0} left)</button></div>`;
  p.querySelector("textarea").value = k.prompt;
  p.querySelector(".link-btn").addEventListener("click", () => p.remove());
  p.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const v = await api("/api/redo", { session: V.id, id: k.id, prompt: p.querySelector("textarea").value });
      p.remove();
      ledger(`Reshooting keyframe ${k.id}`);
      say(`Reshooting keyframe ${k.id}. The camera moves around it will be filmed from the new still.`);
      render(v);
    } catch (err) { say(`Could not reshoot: ${err.message}`, "warn"); }
  });
  frame.append(p);
  enter(p, { opacity: 0, transform: "translateY(-6px)" });
  p.querySelector("textarea").focus();
}

function renderGate() {
  const b = V.board;
  const phase = V.phase;
  const gate = $("#gate");
  const go = $("#gate-go");
  const back = $("#gate-back");
  const running = V.job?.running;
  gate.hidden = false;
  go.hidden = false;
  back.hidden = false;
  go.disabled = false;
  $("#paybox").hidden = true;
  if (phase === "board" && !V.pay?.paid) {
    const price = usd(V.pay.amount_usd);
    $("#gate-title").textContent = `${price} for the whole site`;
    $("#gate-body").textContent = "One payment covers everything: the stills, the film, the words and your site. After the stills you approve them before the film is made, at no extra cost. Paid in USDC on Base, straight to the studio's own wallet.";
    go.innerHTML = !wallet.address ? `Connect wallet <i class="ph ph-wallet"></i>`
      : wallet.owner ? `Shoot free, you own the studio <i class="ph ph-aperture"></i>`
        : `Pay ${price} and shoot <i class="ph ph-arrow-right"></i>`;
    back.innerHTML = `<i class="ph ph-arrow-left"></i> Back to the brief`;
    $("#paybox").hidden = false;
    $("#pay-wallet").innerHTML = wallet.address ? `<i class="ph ph-wallet"></i> ${short(wallet.address)}${wallet.owner ? " · owner" : ""}` : "";
    $("#pm-amount").textContent = V.pay.amount_usd.toFixed(2);
    $("#pm-to").textContent = V.pay.to;
    $("#board-status").textContent = "Awaiting payment";
    $("#board-status").className = "board-status";
  } else if (phase === "board") {
    const p = V.payment;
    $("#gate-title").textContent = `Shoot the ${b.keyframes.length} keyframes`;
    $("#gate-body").textContent = `${p?.kind === "owner" ? "Owner wallet: no charge." : p ? `Paid ${usd(p.usd)} from ${short(p.address)}.` : ""} Stills first; you review every one before any film is made.`;
    go.innerHTML = `Shoot keyframes <i class="ph ph-aperture"></i>`;
    back.innerHTML = `<i class="ph ph-arrow-left"></i> Back to the brief`;
    $("#board-status").textContent = "Awaiting approval";
    $("#board-status").className = "board-status";
  } else if (phase === "keyframes") {
    const missing = b.keyframes.some((k) => !k.url);
    $("#gate-title").textContent = missing ? "Some stills are missing" : "Happy with the stills? Approve them to film";
    $("#gate-body").textContent = missing
      ? "Reshoot the missing ones, or go back to the storyboard."
      : `Already paid: nothing more to pay. Redo any shot you do not love (${V.allowance?.reshoots ?? 0} reshoot${V.allowance?.reshoots === 1 ? "" : "s"} left), then approve and the camera moves are filmed between them while the words are written.`;
    go.innerHTML = `Approve and film <i class="ph ph-film-reel"></i>`;
    go.disabled = missing;
    back.innerHTML = `<i class="ph ph-arrow-left"></i> Back to the storyboard`;
    back.hidden = true;
    $("#board-status").textContent = "Your review";
    $("#board-status").className = "board-status";
  } else {
    gate.hidden = true;
    $("#board-status").textContent = phase === "shooting" ? "Shooting" : "Filming";
    $("#board-status").className = "board-status live";
  }
  if (running) gate.hidden = true;
  setProgress(progressNow());
}

function progressNow() {
  const b = V.board;
  const k = b.keyframes.filter((x) => x.url).length;
  const m = b.moves.filter((x) => x.url).length;
  if (V.phase === "shooting" || V.phase === "board") return k / b.keyframes.length;
  return (b.keyframes.length + m) / (b.keyframes.length + b.moves.length);
}
function setProgress(f) { $("#progress-bar").style.transform = `scaleX(${Math.max(0, Math.min(1, f))})`; }

/* ---------------- payment: owner wallets sign, everyone else pays USDC on Base ---------------- */
const wallet = { address: null, owner: false };
const short = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "");
const hexOf = (text) => `0x${[...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function ensureBase(eth) {
  if ((await eth.request({ method: "eth_chainId" })) === "0x2105") return;
  try {
    await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x2105" }] });
  } catch (e) {
    if (e.code !== 4902) throw e;
    await eth.request({ method: "wallet_addEthereumChain", params: [{ chainId: "0x2105", chainName: "Base",
      nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 }, rpcUrls: ["https://mainnet.base.org"],
      blockExplorerUrls: ["https://basescan.org"] }] });
  }
}

async function connectWallet() {
  const eth = window.ethereum;
  if (!eth) {
    $("#pay-manual").hidden = false;
    say("No browser wallet found. Pay from any wallet with the address below, or install Coinbase Wallet, MetaMask or Rabby.", "warn");
    return false;
  }
  const [address] = await eth.request({ method: "eth_requestAccounts" });
  wallet.address = address.toLowerCase();
  const r = await api("/api/pay/challenge", { session: V.id, address: wallet.address });
  wallet.owner = r.owner;
  wallet.message = r.message || null;
  say(wallet.owner ? "That is the studio owner's wallet: you can shoot for free." : `Connected ${short(address)}.`);
  return true;
}

async function ownerSignIn() {
  if (!wallet.message) await connectWallet();
  const signature = await window.ethereum.request({ method: "personal_sign", params: [hexOf(wallet.message), wallet.address] });
  wallet.message = null;
  render(await api("/api/pay/owner", { session: V.id, signature }), { quiet: true });
}

async function confirmPayment(tx) {
  say("Payment sent. Waiting for Base to confirm it.");
  for (let i = 0; i < 80; i += 1) {
    const r = await api("/api/pay/confirm", { session: V.id, tx });
    if (r.status !== "pending") { render(r, { quiet: true }); return; }
    await sleep(3000);
  }
  throw new Error("Base has not confirmed the payment yet. Paste the transaction hash under 'Pay from another wallet' in a minute.");
}

async function payUsdc() {
  const eth = window.ethereum;
  await ensureBase(eth);
  const p = V.pay;
  const data = `0xa9059cbb${p.to.slice(2).toLowerCase().padStart(64, "0")}${BigInt(p.amount_units).toString(16).padStart(64, "0")}`;
  say(`Approve ${usd(p.amount_usd)} USDC in your wallet.`);
  const tx = await eth.request({ method: "eth_sendTransaction", params: [{ from: wallet.address, to: p.token, data }] });
  ledger(`You paid ${usd(p.amount_usd)} USDC`, short(tx));
  await confirmPayment(tx);
}

$("#pay-manual-btn").addEventListener("click", () => { $("#pay-manual").hidden = !$("#pay-manual").hidden; });
$("#pm-copy").addEventListener("click", () => { navigator.clipboard?.writeText(V.pay.to); say("Studio wallet address copied."); });
$("#pay-manual").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tx = $("#pm-tx").value.trim();
  try { await confirmPayment(tx); if (V.pay?.paid) say("Payment received. Shoot when you are ready."); }
  catch (err) { say(`Could not accept that payment: ${err.message}`, "warn"); }
});

$("#gate-go").addEventListener("click", async () => {
  const go = $("#gate-go");
  go.disabled = true;
  try {
    if (V.phase === "board" && !V.pay?.paid) {
      if (!wallet.address) { await connectWallet(); renderGate(); return; }
      if (wallet.owner) await ownerSignIn();
      else await payUsdc();
      if (!V.pay?.paid) { renderGate(); return; }
    }
    if (V.phase === "board") {
      const v = await api("/api/shoot", { session: V.id, approve: true });
      ledger("Keyframes approved by you");
      say("Paid. Shooting the stills now; you review them before anything is filmed.");
      render(v);
    } else if (V.phase === "keyframes") {
      go.innerHTML = `Writing the words <i class="ph ph-pen-nib"></i>`;
      say("The copywriter is writing the words from your stills. The camera rolls right after.");
      const v = await api("/api/film", { session: V.id, approve: true });
      ledger("Film approved by you");
      say("Filming the camera moves. Each one starts on one still and lands exactly on the next.");
      render(v);
    }
  } catch (e) {
    go.disabled = false;
    renderGate();
    say(`Could not start: ${e.message}`, "warn");
  }
});
$("#gate-back").addEventListener("click", async () => render(await api("/api/back", { session: V.id, to: "confirm" })));

function ledger(text, amount = "") {
  $("#ledger").hidden = false;
  const li = document.createElement("li");
  li.innerHTML = "<span></span><span class=\"amt\"></span>";
  li.children[0].textContent = text;
  li.children[1].textContent = amount;
  $("#ledger-list").prepend(li);
}

// Live render events. The server replays the job log first, so this is safe to re-run on reload.
function follow() {
  if (events) return;
  const seen = new Set();
  events = new EventSource(`/api/events?session=${V.id}`);
  const q = (id) => document.querySelector(`#strip .frame[data-id="${id}"]`);
  events.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    const key = JSON.stringify(ev);
    if (seen.has(key) && ev.type !== "finished") return;
    seen.add(key);
    switch (ev.type) {
      case "kf_start": { const f = q(ev.id); if (f) setMedia(f, "pending"); ledger(`Shooting keyframe ${ev.id}`); break; }
      case "kf_done": {
        const f = q(ev.id); if (!f) break;
        setMedia(f, "img", ev.url);
        const k = V.board.keyframes.find((x) => x.id === ev.id); if (k) k.url = ev.url;
        f.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", inline: "nearest", block: "nearest" });
        setProgress(progressNow());
        break;
      }
      case "clip_start": { const f = q(`${ev.from}-${ev.to}`); if (f) setMedia(f, "working"); ledger(`Filming move ${ev.from} to ${ev.to}`); break; }
      case "clip_done": {
        const f = q(`${ev.from}-${ev.to}`); if (!f) break;
        setMedia(f, "video", ev.url);
        const mv = V.board.moves.find((x) => x.id === `${ev.from}-${ev.to}`); if (mv) mv.url = ev.url;
        f.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", inline: "nearest", block: "nearest" });
        setProgress(progressNow());
        break;
      }
      case "join": {
        const f = q(`${ev.from}-${ev.to}`); if (!f) break;
        const j = f.querySelector(".join");
        j.textContent = ev.ok ? `Lands on ${ev.to}` : `Check the landing on ${ev.to}`;
        j.classList.toggle("bad", !ev.ok);
        break;
      }
      case "error": ledger(`Stopped: ${ev.message}`); break;
      case "finished": {
        events.close(); events = null;
        const v = ev.view;
        if (v.phase === "keyframes" && !v.error) say("The stills are in. Redo any you do not love, then approve them to film. It is already paid for.");
        if (v.phase === "deliver") { setProgress(1); say("Done. Click any words in the preview to change them, or ask for a rewrite."); }
        render(v);
        break;
      }
    }
  };
  events.onerror = () => { /* EventSource reconnects by itself; the server replays */ };
}

/* ---------------- 5. deliver + live words ---------------- */
let copy = null;
let saveTimer = null;

function renderDeliver(changed) {
  const b = V.board;
  if (!changed && copy) return;
  copy = structuredClone(b.copy);
  if (!copy.beats) copy.beats = (copy.acts || []).map((a) => a.text);
  bignum("");
  $("#open-site").href = b.site;
  $("#download-zip").href = b.zip || "#";
  buildWordFields();
  renderMoves();
  const frame = $("#result-frame");
  frame.onload = wireFrame;
  frame.src = b.site;
  $("#save-state").textContent = "";
  voice();
}

// Each camera move as a small looping tile; any can be filmed again with a new direction.
function renderMoves() {
  const left = V.allowance?.refilms ?? 0;
  $("#refilms-left").textContent = left ? `${left} refilm${left === 1 ? "" : "s"} left` : "No refilms left";
  $("#refilm-panel").hidden = true;
  const row = $("#moves-row");
  row.innerHTML = "";
  V.board.moves.forEach((m) => {
    const t = document.createElement("div");
    t.className = `move-tile${m.lands === false ? " drifts" : ""}`;
    t.innerHTML = `<video muted playsinline loop preload="metadata"></video>
      <div class="move-meta"><b></b>${m.lands === false ? '<span class="drift"><i class="ph ph-warning"></i> Check the landing</span>' : ""}</div>
      <button class="link-btn refilm-btn" type="button"><i class="ph ph-film-reel"></i> Refilm</button>`;
    const v = t.querySelector("video");
    v.poster = V.board.keyframes.find((k) => k.id === m.from)?.url || "";
    if (m.url) v.src = m.url;
    t.querySelector("b").textContent = `${m.from} to ${m.to}`;
    t.addEventListener("mouseenter", () => { if (!reduceMotion) v.play().catch(() => {}); });
    t.addEventListener("mouseleave", () => v.pause());
    const btn = t.querySelector(".refilm-btn");
    btn.disabled = left < 1;
    btn.addEventListener("click", () => openRefilm(m));
    row.append(t);
  });
}

let refilmMove = null;
function openRefilm(m) {
  refilmMove = m;
  $("#refilm-label").textContent = `How should the camera travel from ${m.from} to ${m.to}? Change it, or keep it and simply refilm.`;
  $("#refilm-prompt").value = m.prompt.replace(/^One continuous slow camera move, no cuts:\s*/i, "");
  $("#refilm-panel").hidden = false;
  enter($("#refilm-panel"), { opacity: 0, transform: "translateY(-6px)" });
  $("#refilm-prompt").focus({ preventScroll: true });
}
$("#refilm-cancel").addEventListener("click", () => { $("#refilm-panel").hidden = true; });
$("#refilm-panel").addEventListener("submit", async (e) => {
  e.preventDefault();
  const m = refilmMove;
  if (!m) return;
  $("#refilm-go").disabled = true;
  try {
    const body = { session: V.id, id: m.id, prompt: $("#refilm-prompt").value };
    let v;
    try { v = await api("/api/refilm", body); }
    catch (err) {
      // A film opened by link has no payment in this session: the owner wallet can sign in here.
      if (!/payment/i.test(err.message)) throw err;
      say("Sign in with the studio owner's wallet to refilm this film.");
      if (!(await connectWallet()) || !wallet.owner) throw new Error("only the paid session or the owner wallet can refilm this film");
      await ownerSignIn();
      v = await api("/api/refilm", body);
    }
    ledger(`Refilming move ${m.from} to ${m.to}`);
    say(`Refilming the move from ${m.from} to ${m.to}. Your words and layout stay as they are.`);
    copy = null;
    render(v);
  } catch (err) { say(`Could not refilm: ${err.message}`, "warn"); }
  finally { $("#refilm-go").disabled = false; }
});

function voice() {
  const parts = [copy.voice && `Voice: ${copy.voice}`, copy.layout?.note && `Type: ${copy.layout.note}`].filter(Boolean);
  $("#voice").hidden = !parts.length;
  $("#voice").textContent = parts.join("  ·  ");
}

function field(label, key, value, { multi = false, hint = "" } = {}) {
  const wrap = document.createElement("label");
  wrap.className = "wf";
  wrap.innerHTML = `<span class="wf-label"></span>${multi ? "<textarea rows=\"2\"></textarea>" : "<input>"}`;
  wrap.querySelector(".wf-label").textContent = label;
  if (hint) wrap.querySelector(".wf-label").dataset.hint = hint;
  const input = wrap.querySelector("input, textarea");
  input.value = value || "";
  input.dataset.key = key;
  const fit = () => { if (multi) { input.style.height = "auto"; input.style.height = `${input.scrollHeight + 2}px`; } };
  input.addEventListener("input", () => { setCopy(key, input.value); pushToFrame(key, input.value); fit(); scheduleSave(); });
  input.addEventListener("focus", () => focusInFrame(key));
  input.addEventListener("fit", fit);
  requestAnimationFrame(fit);
  return wrap;
}

function group(title) {
  const g = document.createElement("div");
  g.className = "wf-group";
  g.innerHTML = "<h3></h3>";
  g.querySelector("h3").textContent = title;
  return g;
}

const PRESETS = [["cinema", "Cinema"], ["editorial", "Editorial"], ["gallery", "Gallery"], ["poster", "Poster"], ["whisper", "Whisper"]];
const GRID = ["tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br"];
const GRID_NAMES = { tl: "top left", tc: "top centre", tr: "top right", ml: "middle left", c: "centre", mr: "middle right", bl: "bottom left", bc: "bottom centre", br: "bottom right" };
const SIZES = [["s", "S"], ["m", "M"], ["l", "L"], ["xl", "XL"]];
const ENTERS = [["rise", "Rise"], ["fade", "Fade"], ["blur", "Blur"], ["wipe", "Wipe"], ["type", "Type"]];

// Older films only had left / centre / right; fill in a full layout from that.
function ensureLayout() {
  const side = { right: "r", left: "l" }[copy.align] || "c";
  const lay = copy.layout || {};
  copy.layout = {
    ...lay,
    preset: lay.preset || "cinema",
    hero: lay.hero || (copy.align === "right" ? "br" : "bl"),
    beats: copy.beats.map((_, i) => ({ pos: `b${side}`, size: "m", enter: "rise", ...(lay.beats || [])[i] })),
  };
}

// A 3x3 grid of dots: where on the frame the words sit.
function posGrid(value, label, onPick) {
  const g = document.createElement("div");
  g.className = "pos-grid";
  g.setAttribute("role", "radiogroup");
  g.setAttribute("aria-label", label);
  GRID.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-label", GRID_NAMES[p]);
    b.setAttribute("aria-checked", String(p === value));
    b.title = GRID_NAMES[p];
    b.addEventListener("click", () => {
      g.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", "false"));
      b.setAttribute("aria-checked", "true");
      onPick(p);
    });
    g.append(b);
  });
  return g;
}

function mini(items, value, onPick) {
  const el = document.createElement("div");
  el.className = "seg seg-mini";
  segmented(el, items, value, onPick);
  return el;
}

// Push the layout into the live preview without reloading it.
function applyLayout() {
  const doc = frameDoc();
  if (!doc) return;
  const L = copy.layout;
  doc.body.dataset.hero = L.hero;
  const stage = doc.querySelector(".fl-stage");
  if (stage) stage.dataset.preset = L.preset;
  doc.querySelectorAll(".fl-beat").forEach((el, i) => {
    const b = L.beats[i];
    if (!b) return;
    el.dataset.pos = b.pos;
    el.dataset.size = b.size;
    el.dataset.enter = b.enter;
  });
}

function layoutChanged(beatIndex) {
  applyLayout();
  if (beatIndex != null) focusInFrame(`beats.${beatIndex}`);
  scheduleSave();
}

function beatControls(i) {
  const b = copy.layout.beats[i];
  const row = document.createElement("div");
  row.className = "beat-ctl";
  row.append(
    posGrid(b.pos, `Position of beat ${i + 1}`, (v) => { b.pos = v; layoutChanged(i); }),
    mini(SIZES, b.size, (v) => { b.size = v; layoutChanged(i); }),
    mini(ENTERS, b.enter, (v) => { b.enter = v; layoutChanged(i); }));
  const why = copy.layout.why?.[i];
  const wrap = document.createElement("div");
  wrap.append(row);
  if (why) {
    const p = document.createElement("p");
    p.className = "why";
    p.textContent = why;
    wrap.append(p);
  }
  return wrap;
}

function buildWordFields() {
  ensureLayout();
  const box = $("#word-fields");
  box.innerHTML = "";
  const look = group("Style");
  look.append(mini(PRESETS, copy.layout.preset, (v) => { copy.layout.preset = v; layoutChanged(0); }));
  const top = group("Arrival");
  const heroRow = document.createElement("div");
  heroRow.className = "beat-ctl";
  heroRow.append(posGrid(copy.layout.hero, "Title position", (v) => {
    copy.layout.hero = v;
    applyLayout();
    frameDoc()?.defaultView.scrollTo({ top: 0, behavior: "instant" });
    scheduleSave();
  }));
  const heroLbl = document.createElement("span");
  heroLbl.className = "wf-label";
  heroLbl.textContent = "Title position";
  heroRow.prepend(heroLbl);
  top.append(heroRow, field("Title", "title", copy.title), field("Tagline", "tagline", copy.tagline, { multi: true }));
  const film = group("Over the film");
  copy.beats.forEach((t, i) => {
    const card = document.createElement("div");
    card.className = "beat-card";
    card.append(field(`Beat ${i + 1}`, `beats.${i}`, t, { multi: true }), beatControls(i));
    film.append(card);
  });
  const after = group("After the film");
  (copy.sections || []).forEach((s, i) => after.append(
    field(`Heading ${i + 1}`, `sections.${i}.heading`, s.heading),
    field(`Text ${i + 1}`, `sections.${i}.body`, s.body, { multi: true })));
  const end = group("Button");
  end.append(field("Label", "cta.label", copy.cta?.label), field("Link", "cta.href", copy.cta?.href, { hint: "mailto: or https://" }));
  box.append(look, top, film, after, end);
  stagger(box.querySelectorAll(".wf, .beat-ctl"), { opacity: 0, transform: "translateY(8px)" }, 150, 25);
}

function getCopy(key) { return key.split(".").reduce((o, k) => (o == null ? o : o[k]), copy); }
function setCopy(key, value) {
  const parts = key.split(".");
  let o = copy;
  parts.slice(0, -1).forEach((k) => { o[k] ??= {}; o = o[k]; });
  o[parts.at(-1)] = value;
}

function frameDoc() { try { return $("#result-frame").contentDocument; } catch { return null; } }

// Make every piece of text in the preview editable in place (same origin: no messaging needed).
function wireFrame() {
  const doc = frameDoc();
  if (!doc) return;
  doc.body.classList.add("fl-editing");
  const style = doc.createElement("style");
  style.textContent = `[data-edit]{outline:1px dashed transparent;outline-offset:6px;border-radius:4px;transition:outline-color .2s;cursor:text}
    [data-edit]:hover{outline-color:color-mix(in srgb, currentColor 45%, transparent)}
    [data-edit]:focus{outline:1px solid #ee7d45}`;
  doc.head.append(style);
  const wire = (el) => {
    if (el.dataset.wired) return;
    el.dataset.wired = "1";
    el.setAttribute("contenteditable", "plaintext-only");
    el.spellcheck = false;
    el.addEventListener("input", () => {
      const key = el.dataset.edit;
      const value = el.innerText.replace(/\n{3,}/g, "\n\n");
      setCopy(key, value);
      doc.querySelectorAll(`[data-edit="${key}"]`).forEach((o) => { if (o !== el) o.textContent = value; });
      const input = document.querySelector(`#word-fields [data-key="${key}"]`);
      if (input) { input.value = value; input.dispatchEvent(new Event("fit")); }
      scheduleSave();
    });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" && !/^(beats|sections\.\d+\.body)/.test(el.dataset.edit)) { e.preventDefault(); el.blur(); } });
  };
  doc.querySelectorAll("[data-edit]").forEach(wire);
  // Beats are created by the page's script; wire them as they appear.
  new MutationObserver(() => doc.querySelectorAll("[data-edit]").forEach(wire)).observe(doc.body, { childList: true, subtree: true });
  doc.addEventListener("click", (e) => { if (e.target.closest("a")) e.preventDefault(); }, true);
}

function pushToFrame(key, value) {
  const doc = frameDoc();
  doc?.querySelectorAll(`[data-edit="${key}"]`).forEach((el) => { if (el !== doc.activeElement) el.textContent = value; });
  if (key === "cta.href") doc?.querySelector(".cta")?.setAttribute("href", value);
}

// Focusing a field scrolls the preview to where those words live.
function focusInFrame(key) {
  const doc = frameDoc();
  const win = $("#result-frame").contentWindow;
  if (!doc || !win) return;
  const m = key.match(/^beats\.(\d+)$/);
  if (m) {
    const track = doc.getElementById("track");
    const n = copy.beats.length;
    const p = n > 1 ? Number(m[1]) / (n - 1) : 0.5;
    const pp = Math.min(0.97, Math.max(0.03, p));
    // Instant: smooth scrolling a same-origin iframe from the parent stalls in Chrome.
    win.scrollTo({ top: track.offsetTop + pp * (track.offsetHeight - win.innerHeight), behavior: "instant" });
    return;
  }
  const el = doc.querySelector(`[data-edit="${key.replace(/\.href$/, ".label")}"]`);
  if (el) win.scrollTo({ top: el.getBoundingClientRect().top + win.scrollY - win.innerHeight / 2 + el.offsetHeight / 2, behavior: "instant" });
}

function scheduleSave() {
  $("#save-state").textContent = "Editing";
  $("#save-state").className = "save-state";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    $("#save-state").textContent = "Saving";
    try {
      const r = await api("/api/copy", { session: V.id, copy });
      V.board.copy = r.copy;
      $("#save-state").textContent = "Saved to your site and zip";
      $("#save-state").className = "save-state ok";
    } catch (e) {
      $("#save-state").textContent = `Not saved: ${e.message}`;
      $("#save-state").className = "save-state warn";
    }
  }, 900);
}

$("#rewrite").addEventListener("submit", async (e) => {
  e.preventDefault();
  const act = e.submitter?.dataset.act || "words";
  const typed = $("#rewrite-input").value.trim();
  const note = typed || (act === "words" ? "Make it sharper and more specific." : "");
  const btns = [...document.querySelectorAll("#rewrite button")];
  const btn = e.submitter || btns[0];
  const label = btn.innerHTML;
  btns.forEach((b) => { b.disabled = true; });
  btn.innerHTML = `<i class="ph ph-circle-notch spin"></i> ${act === "words" ? "Writing" : "Looking at the frames"}`;
  $("#word-fields").classList.add("busy");
  say(act === "words" ? `The copywriter is on it: ${note}` : `The typographer is studying every frame${note ? `: ${note}` : "."}`);
  try {
    const r = await api(act === "words" ? "/api/rewrite" : "/api/layout", { session: V.id, note });
    copy = structuredClone(r.copy);
    if (!copy.beats) copy.beats = (copy.acts || []).map((a) => a.text);
    V.board.copy = r.copy;
    buildWordFields();
    voice();
    applyLayout();
    const doc = frameDoc();
    if (act === "words") {
      const keys = ["title", "tagline", "cta.label", ...copy.beats.map((_, i) => `beats.${i}`),
        ...(copy.sections || []).flatMap((_, i) => [`sections.${i}.heading`, `sections.${i}.body`])];
      keys.forEach((k) => doc?.querySelectorAll(`[data-edit="${k}"]`).forEach((el) => {
        el.textContent = getCopy(k) ?? "";
        enter(el, { opacity: 0, filter: "blur(6px)" }, { duration: 700 });
      }));
    } else focusInFrame("beats.0");
    $("#rewrite-input").value = "";
    $("#save-state").textContent = act === "words" ? "Rewritten and saved" : "Laid out and saved";
    $("#save-state").className = "save-state ok";
    say(act === "words" ? "New words are in. Keep editing by hand, or ask again."
      : `New layout: ${(copy.layout?.note || "done").replace(/[.\s]+$/, "")}. Adjust any line below, or undo.`);
  } catch (err) { say(`That failed: ${err.message}`, "warn"); }
  finally {
    btns.forEach((b) => { b.disabled = false; });
    btn.innerHTML = label;
    $("#word-fields").classList.remove("busy");
  }
});

$("#undo-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/undo", { session: V.id });
    copy = structuredClone(r.copy);
    if (!copy.beats) copy.beats = (copy.acts || []).map((a) => a.text);
    V.board.copy = r.copy;
    buildWordFields();
    voice();
    // Words may have changed too: reload the preview so every line matches.
    $("#result-frame").src = `${V.board.site.split("?")[0]}?v=${Date.now()}`;
    $("#save-state").textContent = "Undone";
    $("#save-state").className = "save-state ok";
    say("Back to the previous words and layout.");
  } catch (e) { say(`Could not undo: ${e.message}`, "warn"); }
});

/* ---------------- the director: conversation and live state ---------------- */
let chatShown = 0;
let lastSaid = null;
const ACTION_ICON = { pay: "ph-wallet", render: "ph-film-reel", reshoot: "ph-arrow-clockwise", inspect: "ph-eye",
  storyboard: "ph-film-slate", brief: "ph-note-pencil", shoot: "ph-aperture", film: "ph-film-reel", refilm: "ph-film-reel",
  rewrite: "ph-pen-nib", layout: "ph-text-aa", set_words: "ph-pen-nib" };

function renderChat() {
  const log = $("#chat-log");
  const msgs = V.chat || [];
  if (log.dataset.sid !== V.id || msgs.length < chatShown) { log.innerHTML = ""; chatShown = 0; log.dataset.sid = V.id; }
  log.querySelector(".typing")?.remove();
  msgs.slice(chatShown).forEach((m) => {
    const li = document.createElement("li");
    li.className = m.who === "action" ? `action${m.ok === false ? " bad" : ""}` : m.who;
    if (m.who === "action") {
      const icon = document.createElement("i");
      icon.className = `ph ${m.ok === false ? "ph-warning" : ACTION_ICON[m.tool] || "ph-check"}`;
      li.append(icon);
      if (m.detail) {
        const d = document.createElement("details");
        d.innerHTML = "<summary></summary><div class=\"detail\"></div>";
        d.querySelector("summary").textContent = m.text;
        d.querySelector(".detail").textContent = m.detail;
        li.append(d);
      } else li.append(document.createTextNode(m.text));
    } else li.textContent = m.text;
    log.append(li);
    if (chatShown && !reduceMotion) enter(li, { opacity: 0, transform: "translateY(8px)" }, { duration: 450 });
  });
  chatShown = msgs.length;
  const busy = Boolean(V.agent?.busy);
  if (busy) { const t = document.createElement("li"); t.className = "typing"; t.innerHTML = "<span></span><span></span><span></span>"; log.append(t); }
  $("#agent-line").classList.toggle("thinking", busy);
  $("#chat-panel").classList.toggle("thinking", busy);
  document.body.classList.toggle("agent-working", busy);
  $("#review-toggle").checked = Boolean(V.agent?.review);
  log.scrollTop = log.scrollHeight;
  const latest = [...msgs].reverse().find((m) => m.who === "agent");
  if (latest && latest.at !== lastSaid) {
    if (lastSaid !== null) say(latest.text);
    lastSaid = latest.at;
  }
}

function openChat(open) {
  $("#chat-panel").hidden = !open;
  $("#chat-toggle").setAttribute("aria-expanded", String(open));
  if (open) { enter($("#chat-panel"), { opacity: 0, transform: "translateY(10px)" }, { duration: 400 }); $("#chat-log").scrollTop = 1e9; }
}

// While the director thinks or a render runs, keep the screen in step with the server.
let pollTimer = null;
function schedulePoll() {
  clearTimeout(pollTimer);
  const w = V.agent?.waiting;
  if (!(V.agent?.busy || w === "job" || w === "slot")) return;
  pollTimer = setTimeout(async () => {
    try { render(await api(`/api/session?session=${V.id}`), { quiet: true }); } catch { schedulePoll(); }
  }, 1600);
}

$("#chat-toggle").addEventListener("click", () => openChat($("#chat-panel").hidden));
$("#chat-close").addEventListener("click", () => openChat(false));
$("#review-toggle").addEventListener("change", async (e) => {
  try { render(await api("/api/agent/settings", { session: V.id, review: e.target.checked }), { quiet: true }); }
  catch (err) { say(`Could not change that: ${err.message}`, "warn"); }
});
$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  if (V.phase === "pitch" && !V.pitch) { $("#pitch-input").value = text; return $("#pitch-go").click(); }
  try { render(await api("/api/chat", { session: V.id, text }), { quiet: true }); openChat(true); }
  catch (err) { input.value = text; say(`Could not send that: ${err.message}`, "warn"); }
});

/* ---------------- new film ---------------- */
$("#new-btn").addEventListener("click", async () => {
  if (events) { events.close(); events = null; }
  history.replaceState(null, "", "/studio");
  $("#pitch-input").value = "";
  $("#ledger-list").innerHTML = "";
  $("#ledger").hidden = true;
  copy = null;
  shownScene = null;
  lastSaid = null;
  openChat(false);
  render(await api("/api/session", {}));
});

/* ---------------- guided tour ---------------- */
const TOUR = [
  { title: "Welcome to the studio", body: "Pitch any site: a product, your portfolio, an event, something strange. An agent turns it into a scroll film with words. This shows the path." },
  { target: "#pitch-box", title: "Pitch it in one go", body: "Say what it is and how it should feel. Add images when your subject must look exactly right. If you say enough, you skip every question." },
  { target: "#stepper", title: "Pay once, the director does the rest", body: "One payment at the storyboard covers the whole site. The director shoots the stills, checks every one itself, reshoots weak ones within the film's allowance, and films the moves." },
  { target: "#agent-line", title: "Talk to it any time", body: "Ask for changes in plain words: another angle, a calmer ending, new words, a different type style. Or edit the words yourself in the preview. You can close the tab; your film waits here.", last: true },
];
let tourIdx = 0;
function placeTour() {
  const step = TOUR[tourIdx];
  const spot = $("#tour-spot");
  const card = $("#tour-card");
  $("#tour-count").textContent = tourIdx === 0 ? "Quick guide" : `Step ${tourIdx} of ${TOUR.length - 1}`;
  $("#tour-title").textContent = step.title;
  $("#tour-body").textContent = step.body;
  $("#tour-back").hidden = tourIdx === 0;
  $("#tour-next").textContent = step.last ? "Start" : tourIdx === 0 ? "Show me" : "Next";
  const vw = innerWidth, vh = innerHeight, pad = 10;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  const target = step.target && $(step.target);
  if (!target || !target.offsetParent) {
    Object.assign(spot.style, { top: `${vh / 2}px`, left: `${vw / 2}px`, width: "0px", height: "0px" });
    Object.assign(card.style, { top: `${(vh - ch) / 2}px`, left: `${(vw - cw) / 2}px` });
    return;
  }
  const r = target.getBoundingClientRect();
  Object.assign(spot.style, { top: `${r.top - pad}px`, left: `${r.left - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
  let top, left;
  if (r.right + 24 + cw < vw) { left = r.right + 24; top = r.top + r.height / 2 - ch / 2; }
  else if (r.left - 24 - cw > 0) { left = r.left - 24 - cw; top = r.top + r.height / 2 - ch / 2; }
  else if (r.bottom + 24 + ch < vh) { top = r.bottom + 24; left = r.left + r.width / 2 - cw / 2; }
  else { top = r.top - 24 - ch; left = r.left + r.width / 2 - cw / 2; }
  card.style.top = `${Math.max(16, Math.min(vh - ch - 16, top))}px`;
  card.style.left = `${Math.max(16, Math.min(vw - cw - 16, left))}px`;
}
function openTour() { tourIdx = 0; $("#tour").hidden = false; placeTour(); enter($("#tour-card"), { opacity: 0, transform: "translateY(10px) scale(0.98)" }); $("#tour-next").focus(); }
function closeTour() { $("#tour").hidden = true; store.set("frameline-tour-2", "done"); if (V?.phase === "pitch") $("#pitch-input").focus(); }
$("#tour-next").addEventListener("click", () => { if (TOUR[tourIdx].last) return closeTour(); tourIdx += 1; placeTour(); });
$("#tour-back").addEventListener("click", () => { tourIdx = Math.max(0, tourIdx - 1); placeTour(); });
$("#tour-skip").addEventListener("click", closeTour);
$("#guide-btn").addEventListener("click", openTour);
addEventListener("resize", () => { if (!$("#tour").hidden) placeTour(); });
addEventListener("keydown", (e) => {
  if ($("#tour").hidden) return;
  if (e.key === "Escape") closeTour();
  if (e.key === "ArrowRight") $("#tour-next").click();
  if (e.key === "ArrowLeft" && tourIdx > 0) $("#tour-back").click();
});

/* ---------------- boot: resume whatever this browser was doing ---------------- */
(async () => {
  api("/api/prices").then((p) => { prices = p; if (V?.phase === "confirm") updatePrice(); }).catch(() => {});
  let run = new URLSearchParams(location.search).get("run");
  const shared = new URLSearchParams(location.search).get("session");
  let v = null;
  try {
    if (shared) { v = await api(`/api/session?session=${encodeURIComponent(shared)}`).catch(() => null); history.replaceState(null, "", "/studio"); }
    if (!v && run) v = await api("/api/open", { run });
    else if (store.get(SID_KEY)) v = await api(`/api/session?session=${store.get(SID_KEY)}`).catch(() => null);
    v ||= await api("/api/session", {});
  } catch (e) { say(`Could not reach the studio: ${e.message}`, "warn"); return; }
  const prefill = new URLSearchParams(location.search).get("pitch");
  if (prefill && v.phase !== "pitch") v = await api("/api/session", {});
  if (prefill) { $("#pitch-input").value = prefill.slice(0, 3000); history.replaceState(null, "", "/studio"); }
  render(v, { quiet: true });
  if (v.phase !== "pitch") {
    const back = { questions: "Picking up where you left off: a question or two remained.", confirm: "Your brief is waiting.",
      board: "Your storyboard is waiting for approval.", shooting: "Still shooting your keyframes. They land here as they finish.",
      keyframes: "Your keyframes are here. Redo any, then film it.", filming: "Still filming. The moves land here as they finish.",
      deliver: "Your site is here. Click any words in the preview to change them." };
    say(back[v.phase] || "Welcome back.");
  }
  if (store.get("frameline-tour-2") !== "done" && v.phase === "pitch") setTimeout(openTour, reduceMotion ? 0 : 900);
})();
