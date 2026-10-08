// Frameline studio. The director runs the process from the conversation on the right; the left shows what
// exists now (pitch, brief, storyboard, stills, the finished site) and keeps direct edits. The two decisions
// a person makes, the price and the stills, sit in the conversation right under the director's question.
// The server keeps the session on disk; reloading the page picks up exactly where it was.
import { $, api, usd, enter, reduceMotion, store, EASE } from "/static/common.js";

const STEPS = ["brief", "board", "keyframes", "film", "deliver"];
const PHASE_STEP = { pitch: "brief", questions: "brief", confirm: "brief", board: "board",
  shooting: "keyframes", keyframes: "keyframes", filming: "film", deliver: "deliver" };
const PHASE_SCENE = { pitch: "pitch", questions: "brief", confirm: "brief", board: "board",
  shooting: "board", keyframes: "board", filming: "board", deliver: "deliver" };
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

/* ---------------- the studio's own notes ---------------- */
// Wallet steps and errors. Inside the decision card while one is open, otherwise a short toast.
let toastTimer = null;
function say(text, tone = "") {
  if (!$("#decide").hidden) {
    const note = $("#decide-note");
    note.textContent = text;
    note.classList.toggle("warn", tone === "warn");
    return;
  }
  const t = $("#toast");
  t.textContent = text;
  t.classList.toggle("warn", tone === "warn");
  t.hidden = false;
  enter(t, { opacity: 0, transform: "translate(-50%, 10px)" }, { duration: 400 });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, tone === "warn" ? 7000 : 4000);
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
  if (scene === "brief") renderBrief(changed);
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
  try {
    render(await api("/api/chat", { session: V.id, text: pitch }), { quiet: true });
    openDock(true);
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

/* ---------------- 2. the brief: what the director understood ---------------- */
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

const FIELD_LABELS = [["name", "Name"], ["subject", "Subject"], ["mood", "Mood"], ["audience", "For"], ["goal", "Visitors should"]];
function renderBrief(changed) {
  const b = V.brief || {};
  bignum("");
  const head = $("#c-headline");
  if (changed || head.dataset.text !== (b.headline || "")) {
    head.dataset.text = b.headline || "";
    kinetic(head, b.headline || b.name || "Your film");
  }
  const rows = $("#brief-rows");
  const sig = JSON.stringify([b, V.answers, V.refs]);
  if (changed || rows.dataset.sig !== sig) {
    rows.dataset.sig = sig;
    rows.innerHTML = "";
    FIELD_LABELS.filter(([k]) => b[k]).forEach(([k, label]) => {
      const d = document.createElement("div");
      d.className = "brief-row";
      d.innerHTML = "<dt></dt><dd></dd>";
      d.querySelector("dt").textContent = label;
      d.querySelector("dd").textContent = b[k];
      rows.append(d);
    });
    const refs = $("#c-refs");
    refs.innerHTML = "";
    V.refs.forEach((u) => { const i = new Image(); i.src = u; i.alt = ""; refs.append(i); });
    if (changed) stagger([...rows.children, ...refs.children], { opacity: 0, transform: "translateX(-12px)" }, 350, 60);
  }
  // While the director drafts the storyboard, its shots take shape as placeholders.
  const writing = Boolean(V.agent?.busy);
  const gs = $("#ghost-strip");
  $("#directing").hidden = !writing;
  if (writing && !gs.children.length) {
    const n = (V.controls?.scenes || 3) + 1;
    for (let i = 0; i < n; i += 1) { const sp = document.createElement("span"); sp.style.setProperty("--i", i); gs.append(sp); }
    stagger(gs.children, { opacity: 0, transform: "translateX(40px)" }, 300, 90);
  } else if (!writing) gs.innerHTML = "";
}

/* ---------------- 4. board: storyboard, keyframes, film ---------------- */
function tile(kind, item) {
  const div = document.createElement("div");
  div.className = `frame ${kind}`;
  div.dataset.id = item.id;
  div.innerHTML = `<div class="media"></div><div class="label"></div><div class="prompt"></div>${kind === "move" ? '<div class="join"></div>' : ""}`;
  div.querySelector(".label").textContent = kind === "move" ? `Camera move ${item.from} → ${item.to}` : `Still ${item.id}`;
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
    media.innerHTML = `<i class="ph ${still ? "ph-aperture" : "ph-film-slate"}"></i><span>${still ? "Shot once paid" : "Filmed after you see the stills"}</span>`;
  } else if (state === "pending") {
    media.innerHTML = "";
  } else if (state === "working") {
    media.innerHTML = `<i class="ph ph-film-slate"></i><span>Filming</span>`;
  } else if (state === "img") {
    if (media.querySelector("img")?.src.endsWith(url)) return;
    media.innerHTML = "";
    const img = new Image();
    img.alt = `Still ${frame.dataset.id}`;
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
  $("#board-eyebrow").textContent = { board: "Storyboard", shooting: "Shooting the stills", keyframes: "Your stills", filming: "Filming" }[phase];
  bignum("");
  const strip = $("#strip");
  if (changed || strip.dataset.name !== b.name || strip.children.length !== b.keyframes.length + b.moves.length) {
    strip.innerHTML = "";
    strip.dataset.name = b.name;
    b.keyframes.forEach((k, i) => {
      strip.append(tile("still", k));
      if (b.moves[i]) strip.append(tile("move", b.moves[i]));
    });
    strip.style.gridTemplateColumns = [...strip.children].map((el) => (el.classList.contains("move") ? "minmax(190px, 1fr)" : "minmax(320px, 1.7fr)")).join(" ");
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
  setProgress(progressNow());
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
      ledger(`Reshooting still ${k.id}`);
      render(v);
    } catch (err) { say(`Could not reshoot: ${err.message}`, "warn"); }
  });
  frame.append(p);
  enter(p, { opacity: 0, transform: "translateY(-6px)" });
  p.querySelector("textarea").focus();
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
  ledger(`You paid ${usd(p.amount_usd)} USDC`);
  await confirmPayment(tx);
}

$("#pay-manual-btn").addEventListener("click", () => { $("#pay-manual").hidden = !$("#pay-manual").hidden; });
$("#pm-copy").addEventListener("click", () => { navigator.clipboard?.writeText(V.pay.to); say("Studio wallet address copied."); });
$("#pay-manual").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tx = $("#pm-tx").value.trim();
  try { await confirmPayment(tx); }
  catch (err) { say(`Could not accept that payment: ${err.message}`, "warn"); }
});

/* ---------------- the decision in front of you, pinned above the composer ---------------- */
function decision() {
  // Only while the director is listening: never offer a price or a go-ahead for something it is redrawing.
  if (!V.board || V.agent?.mode === "autopilot" || V.agent?.busy || pending) return null;
  if (V.phase === "board" && !V.pay?.paid) return "pay";
  if (V.phase === "keyframes" && !V.job?.running) return "film";
  return null;
}

function renderDecision() {
  const which = decision();
  const card = $("#decide");
  const was = card.dataset.which || "";
  card.dataset.which = which || "";
  card.hidden = !which;
  $("#decide-pay").hidden = which !== "pay";
  $("#decide-film").hidden = which !== "film";
  if (which !== was) {
    $("#decide-note").textContent = "";
    if (which) enter(card, { opacity: 0, transform: "translateY(10px)" }, { duration: 500 });
  }
  if (which === "pay") {
    const n = V.board.moves.length;
    const res = V.board.resolution || V.controls?.resolution;
    $("#pay-what").textContent = `${V.board.copy.title || "Your site"} · ${n} camera moves · ${res === "1080P" ? "1080p" : "768p"}`;
    $("#pay-price").textContent = usd(V.pay.amount_usd);
    $("#pay-go").innerHTML = !wallet.address ? `<i class="ph ph-wallet"></i> Connect a wallet to pay`
      : wallet.owner ? `<i class="ph ph-aperture"></i> Shoot free: you own the studio`
        : `Pay ${usd(V.pay.amount_usd)} and shoot <i class="ph ph-arrow-right"></i>`;
    $("#pay-wallet").textContent = wallet.address ? `${short(wallet.address)}${wallet.owner ? " · owner" : ""}` : "";
    $("#pm-amount").textContent = V.pay.amount_usd.toFixed(2);
    $("#pm-to").textContent = V.pay.to;
  }
  if (which === "film") {
    const missing = V.board.keyframes.filter((k) => !k.url).length;
    const left = V.allowance?.reshoots ?? 0;
    $("#film-body").textContent = missing
      ? `${missing === 1 ? "One still" : `${missing} stills`} did not come out. Ask me to reshoot, or redo it yourself.`
      : `Redo any still you do not love (${left} reshoot${left === 1 ? "" : "s"} left), tell me what to change, or film it now. It is already paid for.`;
    const go = $("#film-go");
    if (!go.dataset.busy) go.innerHTML = `Film it <i class="ph ph-film-reel"></i>`;
    go.disabled = Boolean(missing);
  }
}

$("#pay-go").addEventListener("click", async () => {
  const go = $("#pay-go");
  go.disabled = true;
  try {
    if (!wallet.address) await connectWallet();
    else if (wallet.owner) await ownerSignIn();
    else await payUsdc();
  } catch (e) {
    say(e.code === 4001 ? "You cancelled that in your wallet." : `Could not pay: ${e.message}`, "warn");
  } finally {
    go.disabled = false;
    if (V) renderDecision();
  }
});

// Writing the words takes a moment before the camera rolls: the decision shows in the thread at once.
$("#film-go").addEventListener("click", async () => {
  const go = $("#film-go");
  go.disabled = true;
  go.dataset.busy = "1";
  go.innerHTML = `<i class="ph ph-circle-notch spin"></i> Writing the words`;
  pending = "The stills are good. Film it.";
  renderChat();
  try {
    const v = await api("/api/film", { session: V.id, approve: true });
    pending = null;
    render(v);
  } catch (e) {
    pending = null;
    renderChat();
    say(`Could not start filming: ${e.message}`, "warn");
  } finally {
    delete go.dataset.busy;
    if (V) renderDecision();
  }
});

// What the camera is doing right now, shown live at the end of the conversation.
let liveText = "";
function ledger(text) { liveText = text; renderLive(); }

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
        if (v.phase === "deliver") setProgress(1);
        liveText = "";
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
    ledger(`Refilming the move ${m.from} to ${m.to}`);
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

/* ---------------- the director: a conversation docked beside the work ---------------- */
const ACTION_ICON = { pay: "ph-wallet", render: "ph-film-reel", reshoot: "ph-arrow-clockwise", inspect: "ph-eye",
  storyboard: "ph-film-slate", brief: "ph-note-pencil", shoot: "ph-aperture", film: "ph-film-reel", refilm: "ph-film-reel",
  rewrite: "ph-pen-nib", layout: "ph-text-aa", set_words: "ph-pen-nib" };
const GAP = 15 * 60;   // a quiet stretch this long gets a time stamp
let threadKey = "";    // which messages the thread was last built from
let pending = null;    // sent, not yet back from the server

const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const scroller = () => $("#dock-scroll");
const nearBottom = () => { const s = scroller(); return s.scrollHeight - s.scrollTop - s.clientHeight < 90; };
let pinned = true;     // reading the latest: new lines keep the thread scrolled to the end
function toBottom(smooth = false) {
  const s = scroller();
  s.scrollTo({ top: s.scrollHeight, behavior: smooth && !reduceMotion ? "smooth" : "auto" });
  pinned = true;
  $("#jump").hidden = true;
}

function li(cls, text) {
  const el = document.createElement("li");
  el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function you(text, cls = "") {
  const el = li(`you${cls}`);
  el.append(Object.assign(document.createElement("div"), { className: "bubble", textContent: text }));
  return el;
}

function step(m, i) {
  const el = li(`step${m.ok === false ? " bad" : ""}`);
  const icon = document.createElement("i");
  icon.className = `ph ${m.ok === false ? "ph-warning" : ACTION_ICON[m.tool] || "ph-check"}`;
  el.append(icon);
  if (m.detail) {
    const d = document.createElement("details");
    d.dataset.i = i;
    d.innerHTML = "<summary></summary><div class=\"detail\"></div>";
    d.querySelector("summary").textContent = m.text;
    d.querySelector(".detail").textContent = m.detail;
    el.append(d);
  } else el.append(Object.assign(document.createElement("span"), { textContent: m.text }));
  return el;
}

// You on the right; the director's words in plain text with its work folded in as small steps.
function buildThread(log, msgs, firstNew, open) {
  let steps = null;
  let prev = null;
  msgs.forEach((m, i) => {
    const fresh = i >= firstNew;
    if (!prev || m.at - prev.at > GAP) log.append(li("stamp", clock(m.at)));
    let el = null;
    if (m.who === "action") {
      if (!steps) {
        el = li("steps");
        steps = document.createElement("ol");
        el.append(steps);
        if (!prev || prev.who === "you") el.classList.add("turn");
      }
      const s = step(m, i);
      steps.append(s);
      if (open.has(String(i))) s.querySelector("details")?.setAttribute("open", "");
      if (fresh && !el) enter(s, { opacity: 0, transform: "translateY(6px)" }, { duration: 400 });
    } else {
      steps = null;
      if (m.who === "you") el = you(m.text);
      else {
        el = li("agent", m.text);
        if (!prev || prev.who === "you") el.classList.add("turn");
      }
    }
    if (el) {
      log.append(el);
      if (fresh) enter(el, { opacity: 0, transform: "translateY(10px)" }, { duration: 450 });
    }
    prev = m;
  });
}

// The last row: the director typing, or the camera at work.
function renderLive() {
  const log = $("#chat-log");
  log.querySelector(".live")?.remove();
  if (!V) return;
  const running = V.job?.running;
  let row = null;
  if (pending || V.agent?.busy) {
    row = li("live typing");
    row.innerHTML = "<span></span><span></span><span></span>";
    row.setAttribute("aria-label", "The director is thinking");
  } else if (running || V.agent?.waiting === "slot") {
    row = li("live working");
    const b = V.board;
    const what = V.agent?.waiting === "slot" ? "Waiting for the camera"
      : liveText || (V.phase === "shooting" ? "Shooting the stills" : "Filming the camera moves");
    let count = "";
    if (b && running) {
      count = V.phase === "shooting"
        ? `${b.keyframes.filter((k) => k.url).length} of ${b.keyframes.length} stills`
        : `${b.moves.filter((m) => m.url).length} of ${b.moves.length} moves`;
    }
    row.innerHTML = "<i class=\"ph ph-circle-notch spin\"></i><span></span><em></em>";
    row.querySelector("span").textContent = what;
    row.querySelector("em").textContent = count;
  }
  if (row) log.append(row);
  if (pinned) toBottom();
}

function statusLine() {
  const a = V.agent || {};
  if (pending || a.busy) return "Thinking…";
  if (V.job?.running) return V.phase === "shooting" ? "Shooting your stills" : "Filming your camera moves";
  if (a.waiting === "slot") return "Waiting for the camera";
  if (V.phase === "board" && !V.pay?.paid) return "Storyboard ready";
  if (V.phase === "keyframes") return "Waiting for you to look at the stills";
  if (V.phase === "deliver") return "Your site is ready";
  return "Here when you need me";
}

// Bigger or smaller, with the price it would be, before paying. Each sends a plain request to the director.
function sizeChips() {
  if (!prices || !V.board || V.agent?.mode === "autopilot") return [];
  const n = V.board.moves.length;
  const res = V.board.resolution || V.controls?.resolution || "768P";
  const out = [];
  if (n < 6) out.push([`Longer · ${n + 1} moves · ${usd(priceFor(n + 1, res))}`, `Make it longer: ${n + 1} camera moves.`]);
  if (n > 2) out.push([`Shorter · ${n - 1} moves · ${usd(priceFor(n - 1, res))}`, `Make it shorter: ${n - 1} camera moves.`]);
  out.push(res === "1080P" ? [`Standard 768p · ${usd(priceFor(n, "768P"))}`, "Make it standard quality, 768p."]
    : [`Sharper · 1080p · ${usd(priceFor(n, "1080P"))}`, "Make it sharper: 1080p."]);
  return out;
}

// One-tap replies that fit where the film is: [label, what gets sent].
function suggestions() {
  if (pending || V.agent?.busy || V.job?.running) return [];
  const same = (t) => [t, t];
  return {
    board: V.pay?.paid ? [] : sizeChips(),
    keyframes: [same("Which still is weakest?")],
    deliver: [same("Sharper words"), same("Gallery-style type"), same("A bigger ending")],
  }[V.phase] || [];
}

function renderSuggest() {
  const box = $("#suggest");
  const list = suggestions();
  const key = list.map((x) => x[1]).join("|");
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = "";
  list.forEach(([label, text]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = label;
    b.addEventListener("click", () => send(text));
    box.append(b);
  });
}

function renderChat() {
  const msgs = V.chat || [];
  const busy = Boolean(V.agent?.busy);
  document.body.classList.toggle("dock-off", V.phase === "pitch" && !msgs.length && !busy && !pending);
  document.body.classList.toggle("agent-working", busy);
  $("#dock").classList.toggle("thinking", busy || Boolean(pending));
  const log = $("#chat-log");
  const key = `${V.id}:${msgs.length}:${msgs.at(-1)?.at ?? ""}`;
  if (key !== threadKey) {
    const sameFilm = threadKey.startsWith(`${V.id}:`);
    const firstNew = sameFilm ? Number(threadKey.split(":")[1]) : msgs.length;  // a fresh load does not animate
    const open = new Set([...log.querySelectorAll("details[open]")].map((d) => d.dataset.i));
    log.innerHTML = "";
    buildThread(log, msgs, firstNew, open);
    threadKey = key;
    if (sameFilm && !pinned && msgs.length > firstNew) $("#jump").hidden = false;
  }
  log.querySelector(".pending")?.remove();
  if (pending) log.append(you(pending, " pending"));
  if (!msgs.length && !pending) log.append(li("empty", "Ask for anything in plain words: another angle, a calmer ending, new words. The director answers here and does the work."));
  $("#dock-status").textContent = statusLine();
  renderSuggest();
  renderDecision();
  if (pinned || pending) toBottom();
  renderLive();
}

// Below 900px the conversation is a sheet that slides up over the work.
function openDock(open) {
  $("#dock").classList.toggle("open", open);
  $("#dock-toggle").setAttribute("aria-expanded", String(open));
  $("#dock-toggle").setAttribute("aria-label", open ? "Hide the conversation" : "Show the conversation");
  if (open) toBottom();
}

async function send(text) {
  if (V.phase === "pitch" && !V.pitch) { $("#pitch-input").value = text; return $("#pitch-go").click(); }
  pending = text;
  renderChat();
  toBottom(true);
  try {
    const v = await api("/api/chat", { session: V.id, text });
    pending = null;
    render(v, { quiet: true });
  } catch (err) {
    pending = null;
    renderChat();
    $("#chat-input").value = text;
    fitComposer();
    say(`Could not send that: ${err.message}`, "warn");
  }
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

function fitComposer() {
  const t = $("#chat-input");
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, 160)}px`;
  $("#chat-send").disabled = !t.value.trim();
}

$("#chat-input").addEventListener("input", fitComposer);
$("#chat-input").addEventListener("focus", () => openDock(true));
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
});
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  fitComposer();
  send(text);
});
$("#dock-head").addEventListener("click", () => openDock(!$("#dock").classList.contains("open")));
$("#jump").addEventListener("click", () => toBottom(true));
$("#dock-scroll").addEventListener("scroll", () => { pinned = nearBottom(); if (pinned) $("#jump").hidden = true; }, { passive: true });
new ResizeObserver(() => { if (pinned) toBottom(); }).observe($("#chat-log"));
// On phones the sheet floats over the work: the page leaves room for it, however tall the decision card is.
new ResizeObserver(([e]) => { document.body.style.setProperty("--sheet", `${Math.ceil(e.borderBoxSize[0].blockSize)}px`); }).observe($("#dock"));
fitComposer();

/* ---------------- new film ---------------- */
$("#new-btn").addEventListener("click", async () => {
  if (events) { events.close(); events = null; }
  history.replaceState(null, "", "/studio");
  $("#pitch-input").value = "";
  copy = null;
  shownScene = null;
  threadKey = "";
  pending = null;
  liveText = "";
  openDock(false);
  render(await api("/api/session", {}));
});

/* ---------------- guided tour ---------------- */
const TOUR = [
  { title: "Welcome to the studio", body: "Pitch any site: a product, your portfolio, an event, something strange. An agent turns it into a scroll film with words. This shows the path." },
  { target: "#pitch-box", title: "Pitch it in one go", body: "Say what it is and how it should feel. Add images when your subject must look exactly right. If you say enough, you skip every question." },
  { target: "#stepper", title: "You make two calls; the director does the rest", body: "You agree the price once the storyboard is drafted, and you look at the stills before anything is filmed. In between, the director shoots, checks every still itself, reshoots weak ones and films the moves." },
  { target: "#dock", title: "Talk to it any time", body: "Once you pitch, the director keeps a conversation beside your film. Ask for changes in plain words: another angle, a calmer ending, new words, a different type style. You can close the tab; your film waits here.", last: true },
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
    else if (!v && store.get(SID_KEY)) v = await api(`/api/session?session=${store.get(SID_KEY)}`).catch(() => null);
    v ||= await api("/api/session", {});
  } catch (e) { say(`Could not reach the studio: ${e.message}`, "warn"); return; }
  const prefill = new URLSearchParams(location.search).get("pitch");
  if (prefill && v.phase !== "pitch") v = await api("/api/session", {});
  if (prefill) { $("#pitch-input").value = prefill.slice(0, 3000); history.replaceState(null, "", "/studio"); }
  render(v, { quiet: true });
  if (store.get("frameline-tour-2") !== "done" && v.phase === "pitch") setTimeout(openTour, reduceMotion ? 0 : 900);
})();
