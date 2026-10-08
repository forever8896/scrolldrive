#!/usr/bin/env python3
"""Frameline studio server.

  python3 app/server.py            # http://localhost:8080

Flow: pitch (+ optional images) -> only the questions left -> brief with controls ->
storyboard and quote -> shoot keyframes (approve) -> review, redo any shot -> film (approve)
-> copywriter writes the words -> deliver, with the words editable live in the preview.

Sessions are saved to disk and renders run as detached processes writing a log, so a server
restart or a closed tab never loses work: reopen the studio and it picks up where it was.
LLM calls go through 1Claw Shroud; rendering spends the agent's Venice balance and only
starts after an explicit approval in the UI.
"""
import base64
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import agent  # noqa: E402
import copywriter  # noqa: E402
import director  # noqa: E402
import evm  # noqa: E402
import intake  # noqa: E402
import ledger  # noqa: E402
import scrollsite  # noqa: E402
import typographer  # noqa: E402
import venice  # noqa: E402
import x402pay  # noqa: E402

STATIC = os.path.join(APP, "static")
RUNS = os.path.join(ROOT, "outputs", "runs")
UPLOADS = os.path.join(ROOT, "outputs", "uploads")
SESSIONS_DIR = os.path.join(ROOT, "outputs", "sessions")
CONFIGS = os.path.join(ROOT, "configs")
MAX_REFS = 6
SHOWCASE = "psyduck-plush-bba2"  # the film the landing page is built around
PAY_GUARDRAILS = {"allowlist": ["Venice top-up (0x2670…293f)"], "max_per_payment_usd": 10, "daily_limit_usd": 15}

# Payment. Customers pay the studio agent's wallet in USDC on Base before anything is generated;
# the server reads the transfer from the chain. Owner wallets sign a message instead and pay nothing.
PAY_TO = x402pay.AGENT_ADDRESS
USDC = x402pay.BASE_USDC
CHAIN_ID = 8453
OWNER_WALLETS = {a.strip().lower() for a in os.environ.get(
    "OWNER_WALLETS", "0x446716454a67222351ef867576a60668eB3172Ce").split(",") if a.strip()}
# Accepted payments live in 1Claw Agent Memory (scripts/ledger.py), so a transaction unlocks
# one film ever, across restarts, redeploys, and the runtime and local studio alike.
PAYMENT_MAX_AGE_HOURS = float(os.environ.get("PAYMENT_MAX_AGE_HOURS", 24))
PAID_STEPS = {"/api/shoot", "/api/redo", "/api/film", "/api/refilm"}
# Optional owner pass for paid steps (header X-Studio-Code), e.g. for scripts.
ACCESS_CODE = os.environ.get("STUDIO_ACCESS_CODE", "")
# The AI steps are free to try but rate limited per visitor, to protect the LLM quota.
LLM_STEPS = {"/api/intake", "/api/storyboard", "/api/rewrite", "/api/layout", "/api/chat", "/api/resize"}
LLM_LIMIT_PER_HOUR = int(os.environ.get("LLM_LIMIT_PER_HOUR", 40))
RATE = {}

SESSIONS = {}
LOCK = threading.Lock()
PRICES = {}


# --- sessions (one JSON file each) --------------------------------------------------

def new_session():
    sid = secrets.token_hex(8)
    s = {"id": sid, "created": time.time(), "phase": "pitch", "pitch": "", "refs": [], "brief": None,
         "answers": [], "controls": None, "name": None, "job": None, "error": None}
    SESSIONS[sid] = s
    save(s)
    return s


def save(s):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    tmp = os.path.join(SESSIONS_DIR, f"{s['id']}.json.tmp")
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, os.path.join(SESSIONS_DIR, f"{s['id']}.json"))


def load_sessions():
    if not os.path.isdir(SESSIONS_DIR):
        return
    for f in os.listdir(SESSIONS_DIR):
        if f.endswith(".json"):
            try:
                s = json.load(open(os.path.join(SESSIONS_DIR, f)))
                SESSIONS[s["id"]] = s
            except (ValueError, KeyError, OSError):
                pass


def cfg_path(name):
    return os.path.join(CONFIGS, f"{name}.json")


def load_cfg(name):
    return json.load(open(cfg_path(name)))


def save_cfg(cfg):
    json.dump(cfg, open(cfg_path(cfg["name"]), "w"), indent=2)


def run_dir(name):
    return os.path.join(RUNS, name)


def run_url(path):
    return "/runs/" + os.path.relpath(path, RUNS).replace(os.sep, "/")


# --- prices ----------------------------------------------------------------------------

def prices():
    if not PRICES or time.time() - PRICES.get("at", 0) > 3600:
        PRICES.update({
            "image_usd": scrollsite.image_price(scrollsite.EDIT_MODEL),
            "clip_usd": {r: venice.video_quote(scrollsite.DEFAULT_VIDEO_MODEL, "5s", r) for r in ("768P", "1080P")},
            "fee_usd": director.PRICE_FEE_USD, "multiplier": director.PRICE_MULTIPLIER, "at": time.time()})
    return {k: v for k, v in PRICES.items() if k != "at"}


def customer_prices():
    """What a site costs the customer, by quality and number of camera moves. Generation
    costs stay on the server."""
    p = prices()
    return {"prices": {res: {n: director.studio_price((n + 1) * p["image_usd"] + n * p["clip_usd"][res])
                             for n in range(director.MIN_SCENES, director.MAX_SCENES + 1)}
                       for res in ("768P", "1080P")}}


def customer_error(msg):
    """Errors shown in the studio never carry generation costs."""
    if "budget" in msg:
        return "This film has used all of its reshoots."
    return re.sub(r"\s*\(?\$[0-9.]+\)?", "", msg)


# --- payment ---------------------------------------------------------------------------------

def price_usd(s):
    if not s.get("name") or not os.path.exists(cfg_path(s["name"])):
        return None
    n = len(load_cfg(s["name"])["keyframes"])
    cfg = load_cfg(s["name"])
    p = prices()
    return director.studio_price(n * p["image_usd"] + (n - 1) * p["clip_usd"][cfg["video"].get("resolution", "768P")])


def allowance(name):
    """How many reshoots or refilms the film's budget still covers, after holding back
    the cost of everything it still needs (missing stills, moves not filmed yet). Counts
    only: the customer never sees costs."""
    cfg = load_cfg(name)
    rd = run_dir(name)
    p = prices()
    img, clip = p["image_usd"], p["clip_usd"][cfg["video"].get("resolution", "768P")]
    try:
        spent = float(json.load(open(os.path.join(rd, "venice-state.json")))["spent_usd"])
    except (OSError, ValueError, KeyError):
        spent = 0.0
    b = board_view(name)
    needed = sum(img for k in b["keyframes"] if not k["url"]) + sum(clip for m in b["moves"] if not m["url"])
    spare = max(0.0, float(cfg.get("budget_usd", 0)) - spent - needed)
    return {"reshoots": int((spare + 1e-9) // img), "refilms": int((spare + 1e-9) // clip)}


def is_paid(s):
    pay = s.get("payment") or {}
    return pay.get("kind") == "owner" or (pay.get("kind") == "usdc" and pay.get("board") == s.get("name"))


def topup_if_needed(need_usd):
    """Before an approved render: if the agent's Venice balance cannot cover it, the agent tops
    itself up from its wallet through 1claw pay (1Claw enforces the payee and the caps)."""
    bal = float(venice.balance()["data"]["balanceUsd"])
    if bal >= need_usd + 0.05:
        return None
    venice.topup()
    return float(venice.balance()["data"]["balanceUsd"])


# --- detached render jobs -------------------------------------------------------------

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def job_state(job):
    """(running, exit_code). A job is a shell that runs the pipeline and appends 'EXIT n'."""
    try:
        tail = open(job["log"], "rb").read()[-400:].decode(errors="replace")
    except OSError:
        return False, -1
    m = re.search(r"^EXIT (\d+)\s*$", tail, re.M)
    if m:
        return False, int(m[1])
    return (True, None) if pid_alive(job.get("pid")) else (False, -1)


def any_running():
    return any(s.get("job") and job_state(s["job"])[0] for s in SESSIONS.values())


def launch(s, stage, only=None, refilm=None):
    name = s["name"]
    os.makedirs(run_dir(name), exist_ok=True)
    log = os.path.join(run_dir(name), f"job-{stage}-{int(time.time())}.log")
    args = [sys.executable, "-u", os.path.join(ROOT, "scripts", "scrollsite.py"), cfg_path(name), "--yes",
            "--stage", stage] + (["--only", only] if only else []) + (["--refilm", refilm] if refilm else [])
    cmd = " ".join(f"'{a}'" for a in args) + '; echo "EXIT $?"'
    out = open(log, "w")
    # Own session: the render survives a server restart and a closed tab.
    proc = subprocess.Popen(["sh", "-c", cmd], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()  # reap, so pid checks stay truthful
    s["job"] = {"stage": stage, "log": log, "pid": proc.pid, "only": only, "refilm": refilm, "started": time.time()}
    s["error"] = None
    s.setdefault("agent", {})["waiting"] = "job"  # the director agent picks up when it finishes
    s["phase"] = "shooting" if stage == "keyframes" else "filming"
    save(s)


def last_error(job):
    try:
        lines = open(job["log"], errors="replace").read().splitlines()
    except OSError:
        return "the render log is missing"
    for line in reversed(lines):
        if "Stopped:" in line:
            return customer_error(line.split("Stopped:", 1)[1].strip()[:300])
    tail = [l for l in lines if l.strip() and not l.startswith("EXIT")][-1:]
    return (tail[0] if tail else "the render stopped")[:300]


def settle(s):
    """Move a session past a finished job. Called whenever a session is read."""
    job = s.get("job")
    if not job or s["phase"] not in ("shooting", "filming"):
        return
    running, code = job_state(job)
    if running:
        return
    if code == 0:
        s["phase"] = "keyframes" if job["stage"] == "keyframes" else "deliver"
        if s["phase"] == "deliver":
            try:
                rebuild_site(load_cfg(s["name"]))  # picks up the typographer's layout if it finished late
            except Exception:  # noqa: BLE001
                pass
    else:
        s["error"] = last_error(job)
        if job["stage"] == "keyframes":
            has_all = s["name"] and all(scrollsite.keyframe_path(run_dir(s["name"]), k["id"])
                                        for k in load_cfg(s["name"])["keyframes"])
            s["phase"] = "keyframes" if job.get("only") or has_all else "board"
        else:
            s["phase"] = "deliver" if job.get("refilm") else "keyframes"  # a failed refilm keeps the film
    save(s)


# --- views --------------------------------------------------------------------------------

def board_view(name):
    cfg = load_cfg(name)
    rd = run_dir(name)
    p = prices()
    kfs, mt = [], {}
    for k in cfg["keyframes"]:
        path = scrollsite.keyframe_path(rd, k["id"]) if os.path.isdir(rd) else None
        mt[k["id"]] = os.path.getmtime(path) if path else None
        kfs.append({**k, "url": f"{run_url(path)}?v={int(mt[k['id']])}" if path else None})
    try:
        joins = {j["clip"].replace("->", "-"): j for j in json.load(open(os.path.join(rd, "report.json"))).get("joins", [])}
    except (OSError, ValueError):
        joins = {}
    moves = []
    for a, b in zip(cfg["keyframes"], cfg["keyframes"][1:]):
        key = f"{a['id']}-{b['id']}"
        clip = os.path.join(rd, f"clip-{key}.mp4")
        # Current unless a still was reshot after the clip. The minute of slack keeps files that
        # arrive together (a fresh git clone on the runtime) from looking out of date.
        fresh = os.path.exists(clip) and mt[a["id"]] and mt[b["id"]] and \
            os.path.getmtime(clip) + 60 >= max(mt[a["id"]], mt[b["id"]])
        moves.append({"id": key, "from": a["id"], "to": b["id"], "prompt": cfg["transitions"].get(key, ""),
                      "url": f"{run_url(clip)}?v={int(os.path.getmtime(clip))}" if fresh else None,
                      "lands": joins.get(key, {}).get("lands_on_end_frame") if fresh else None})
    res = cfg["video"].get("resolution", "768P")
    n = len(cfg["keyframes"])
    site = os.path.join(rd, "site", "index.html")
    zip_path = os.path.join(rd, f"{name}-site.zip")
    return {
        "name": name, "type": cfg.get("type"), "copy": cfg.get("copy", {}), "theme": cfg.get("theme", {}),
        "keyframes": kfs, "moves": moves, "resolution": res,
        "price_usd": director.studio_price(n * p["image_usd"] + (n - 1) * p["clip_usd"][res]),
        "site": f"{run_url(site)}?v={int(os.path.getmtime(site))}" if os.path.exists(site) else None,
        "zip": run_url(zip_path) if os.path.exists(zip_path) else None,
    }


def view(s):
    settle(s)
    out = {k: s.get(k) for k in ("id", "phase", "pitch", "brief", "answers", "controls", "error", "name")}
    out["refs"] = ["/uploads/" + os.path.basename(p) for p in s["refs"]]
    out["job"] = None
    if s.get("job"):
        running, code = job_state(s["job"])
        out["job"] = {"stage": s["job"]["stage"], "running": running, "code": code, "only": s["job"].get("only")}
    if s.get("name") and os.path.exists(cfg_path(s["name"])):
        out["board"] = board_view(s["name"])
        out["allowance"] = allowance(s["name"])
        usd = price_usd(s)
        out["pay"] = {"paid": is_paid(s), "to": PAY_TO, "token": USDC, "chain_id": CHAIN_ID,
                      "amount_usd": usd, "amount_units": int(round(usd * 1e6))}
    pay = s.get("payment") or {}
    out["payment"] = {k: pay.get(k) for k in ("kind", "address", "usd", "tx")} if pay else None
    a = s.get("agent") or {}
    out["chat"] = [{k: m.get(k) for k in ("who", "text", "at", "tool", "ok", "detail")} for m in (s.get("chat") or [])[-80:]]
    out["agent"] = {"busy": bool(a.get("busy")), "waiting": a.get("waiting"), "mode": a.get("mode", "chat"),
                    "inspection": a.get("inspection"),
                    "doing": ({**a["doing"], "for_s": round(time.time() - a["doing"]["since"])} if a.get("doing") else None)}
    return out


# --- log parsing for live events ------------------------------------------------------------

LINE_PATTERNS = [
    (re.compile(r"Keyframe (\w): (\S+) on Venice"), lambda m: {"type": "kf_start", "id": m[1]}),
    (re.compile(r"Keyframe (\w): (/\S+)$"), lambda m: {"type": "kf_done", "id": m[1], "url": run_url(m[2])}),
    (re.compile(r"Clip (\w)->(\w): (\S+) (\S+) (\S+), first->last frame \(\$([0-9.]+)\)"),
     lambda m: {"type": "clip_start", "from": m[1], "to": m[2]}),
    (re.compile(r"clip (\w)->(\w): (\w+); checking"), lambda m: {"type": "clip_wait", "from": m[1], "to": m[2]}),
    (re.compile(r"Clip (\w)->(\w): (/\S+\.mp4)$"),
     lambda m: {"type": "clip_done", "from": m[1], "to": m[2], "url": run_url(m[3])}),
    (re.compile(r"Join check (\w)->(\w): .*end SSIM ([0-9.]+|n/a) -> (.+)$"),
     lambda m: {"type": "join", "from": m[1], "to": m[2], "ssim": m[3], "ok": "DOES NOT" not in m[4]}),
    (re.compile(r"Site:\s+(\S+)"), lambda m: {"type": "site", "url": run_url(m[1])}),
    (re.compile(r"Stopped: (.+)"), lambda m: {"type": "error", "message": customer_error(m[1][:300])}),
]


def parse_line(line):
    text = re.sub(r"^\d\d:\d\d:\d\d ", "", line).strip()
    evs = []
    for pat, make in LINE_PATTERNS:
        m = pat.search(text)
        if m:
            e = make(m)
            if "url" in e and os.path.exists(os.path.join(RUNS, e["url"][len("/runs/"):])):
                e["url"] += f"?v={int(time.time())}"
            evs.append(e)
    return evs


# --- copy -------------------------------------------------------------------------------------

def rebuild_site(cfg):
    rd = run_dir(cfg["name"])
    site_dir = os.path.join(rd, "site")
    if not os.path.exists(os.path.join(site_dir, "film.mp4")):
        return
    try:
        duration = json.load(open(os.path.join(rd, "report.json")))["film_seconds"]
    except (OSError, ValueError, KeyError):
        duration = scrollsite.probe_duration(os.path.join(site_dir, "film.mp4"))
    scrollsite.build_site(cfg, site_dir, duration)
    scrollsite.zip_site(site_dir, os.path.join(rd, f"{cfg['name']}-site.zip"))


def clean_copy(new, cfg):
    """User edits: generous limits, plain text only; structure follows the config."""
    old = cfg.get("copy") or {}
    t = lambda v, n: re.sub(r"[ \t]+", " ", str(v or "")).strip()[:n]
    n_beats = len(old.get("beats") or old.get("acts") or cfg["keyframes"])
    beats = [t(b, 160) for b in (new.get("beats") or [])][:n_beats]
    beats += [""] * (n_beats - len(beats))
    sections = [{"heading": t(s.get("heading"), 80), "body": t(s.get("body"), 700)}
                for s in (new.get("sections") or [])[:4] if isinstance(s, dict)]
    cta = new.get("cta") if isinstance(new.get("cta"), dict) else {}
    href = t(cta.get("href"), 300) or "#"
    if not re.match(r"^(https?://|mailto:|#)", href):
        href = "#"
    return {**old, "title": t(new.get("title"), 80), "tagline": t(new.get("tagline"), 200), "beats": beats,
            "acts": scrollsite.acts_from_beats(beats), "sections": sections,
            "cta": {"label": t(cta.get("label"), 40) or "Get in touch", "href": href},
            "layout": {**scrollsite.normalize_layout({"layout": new.get("layout") or old.get("layout"), "align": old.get("align")}, n_beats),
                       **{k: (new.get("layout") or old.get("layout") or {}).get(k) for k in ("note", "why") if (new.get("layout") or old.get("layout") or {}).get(k)}}}


def status():
    out = {"guardrails": PAY_GUARDRAILS, "agent": x402pay.AGENT_ADDRESS}
    try:
        addr = x402pay.AGENT_ADDRESS[2:].lower()
        body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": x402pay.BASE_USDC, "data": "0x70a08231" + "0" * 24 + addr}, "latest"]}
        req = urllib.request.Request("https://mainnet.base.org", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "frameline/0.2"})
        with urllib.request.urlopen(req, timeout=15) as r:
            out["wallet_usdc"] = round(int(json.loads(r.read())["result"], 16) / 1e6, 2)
    except Exception as e:  # noqa: BLE001
        out["wallet_error"] = str(e)[:400]
    try:
        out["venice_usd"] = round(float(venice.balance()["data"]["balanceUsd"]), 2)
    except Exception as e:  # noqa: BLE001
        out["venice_error"] = str(e)[:400]
    return out


def list_runs():
    out = []
    if os.path.isdir(RUNS):
        for name in sorted(os.listdir(RUNS)):
            site = os.path.join(RUNS, name, "site", "index.html")
            if os.path.exists(site):
                title = name
                if os.path.exists(cfg_path(name)):
                    title = load_cfg(name).get("copy", {}).get("title") or name
                kfs = sorted(f for f in os.listdir(os.path.join(RUNS, name))
                             if re.fullmatch(r"kf-[A-Z]\.(png|jpg|jpeg|webp)", f))
                out.append({"name": name, "title": title, "site": f"/runs/{name}/site/index.html",
                            "keyframes": [f"/runs/{name}/{f}" for f in kfs],
                            "poster": f"/runs/{name}/site/poster.jpg", "film": f"/runs/{name}/site/film-mobile.mp4",
                            "zip": f"/runs/{name}/{name}-site.zip", "studio": f"/studio?run={name}",
                            "mtime": os.path.getmtime(site)})
    return sorted(out, key=lambda r: -r["mtime"])


def showcase():
    """The home page's example film: its media, words, layout and shots."""
    name = SHOWCASE
    cfg, rd = load_cfg(name), run_dir(name)
    report = json.load(open(os.path.join(rd, "report.json")))
    joins = {j["clip"].replace("->", "-"): j for j in report.get("joins", [])}
    kfs = [{"id": k["id"], "url": run_url(scrollsite.keyframe_path(rd, k["id"])),
            "gist": copywriter.shot_gist(k["prompt"]), "refs": bool(k.get("refs")), "chain": k.get("chain")}
           for k in cfg["keyframes"]]
    moves = [{"id": f"{a['id']}-{b['id']}", "url": run_url(os.path.join(rd, f"clip-{a['id']}-{b['id']}.mp4")),
              "prompt": cfg["transitions"].get(f"{a['id']}-{b['id']}", "").replace("One continuous slow camera move, no cuts: ", ""),
              "ssim": joins.get(f"{a['id']}-{b['id']}", {}).get("end_ssim")}
             for a, b in zip(cfg["keyframes"], cfg["keyframes"][1:])]
    voices_path = os.path.join(rd, "voices.json")
    return {
        "name": name, "pitch": cfg.get("brief", "").split("\n")[0], "copy": cfg["copy"],
        "layout": scrollsite.normalize_layout(cfg["copy"], len(cfg["copy"].get("beats") or cfg["copy"].get("acts", []))),
        "film": run_url(os.path.join(rd, "site", "film.mp4")), "film_mobile": run_url(os.path.join(rd, "site", "film-mobile.mp4")),
        "poster": run_url(os.path.join(rd, "site", "poster.jpg")), "site": run_url(os.path.join(rd, "site", "index.html")),
        "duration": report.get("film_seconds"), "minutes": report.get("minutes"),
        "keyframes": kfs, "moves": moves,
        "director_model": cfg.get("director_model"), "copy_model": cfg["copy"].get("model"),
        "voices": json.load(open(voices_path)) if os.path.exists(voices_path) else [],
    }


# --- the studio's steps, shared by the buttons and the director agent --------------------------

class Refusal(Exception):
    """A step that cannot run now; the message is safe to show the customer."""
    def __init__(self, msg, status=400, **extra):
        super().__init__(msg)
        self.status, self.extra = status, extra


def core_intake(s, pitch):
    pitch = str(pitch or "").strip()[:3000]
    if len(pitch) < 3:
        raise Refusal("Tell me a little about what we are making.")
    s["pitch"] = pitch
    try:
        b = intake.ask(pitch, s["refs"])
    except RuntimeError as e:
        raise Refusal(str(e)[:300], 502)
    s["brief"], s["answers"] = b, []
    s["controls"] = {"type": b["type"], "style": b["style"], "scenes": b["scenes"], "resolution": "768P"}
    save(s)
    return b


def core_storyboard(s, controls=None, brief_edits=None, notes=""):
    if not s.get("brief"):
        raise Refusal("Start with a pitch.")
    if s.get("job") and job_state(s["job"])[0]:
        raise Refusal("A render is running.", 409)
    c = {**(s.get("controls") or {}), **{k: v for k, v in (controls or {}).items() if v not in (None, "")}}
    locked = (s.get("payment") or {}).get("locked") or {}  # an agent paid for this size up front
    c.update(locked)
    controls = {"type": c.get("type") if c.get("type") in director.TYPES else "product",
                "style": c.get("style") if c.get("style") in director.STYLES else "",
                "scenes": max(director.MIN_SCENES, min(director.MAX_SCENES, int(c.get("scenes") or 3))),
                "resolution": c.get("resolution") if c.get("resolution") in ("768P", "1080P") else "768P"}
    s["controls"] = controls
    for k in ("name", "subject", "mood", "audience", "goal"):
        if isinstance((brief_edits or {}).get(k), str):
            s["brief"][k] = brief_edits[k].strip()[:200]
    b = s["brief"]
    base = re.sub(r"[^a-z0-9-]+", "-", str(b.get("name") or b.get("subject") or "film").lower()).strip("-")[:28]
    slug = f"{base or 'film'}-{secrets.token_hex(2)}"
    brief = intake.director_brief(s["pitch"], b, s["answers"])
    try:
        director.direct(slug, brief, refs=s["refs"], ptype=controls["type"], style=controls["style"],
                        scenes=controls["scenes"], resolution=controls["resolution"],
                        notes=str(notes or "")[:400] or None)
    except SystemExit as e:
        raise Refusal(str(e)[:400], 502)
    pay = s.get("payment") or {}
    if pay.get("kind") == "usdc" and not pay.get("used") and \
            (pay.get("locked") or pay.get("usd", 0) + 1e-9 >= (price_usd({"name": slug}) or 1e9)):
        pay["board"] = slug  # paid but nothing generated yet: the payment moves to the new storyboard
    s["name"], s["phase"], s["job"], s["error"] = slug, "board", None, None
    save(s)


def core_start(s, stage, only=None, refilm=None):
    if not (is_paid(s) or s.get("code_ok")):
        raise Refusal("This step needs payment first.", 402, need_payment=True)
    cfg = load_cfg(s["name"])
    p = prices()
    n = len(cfg["keyframes"])
    clip = p["clip_usd"][cfg["video"].get("resolution", "768P")]
    need = (p["image_usd"] * (1 if only else n) if stage == "keyframes" else clip * (1 if refilm else n - 1))
    with LOCK:
        if any_running():
            raise Refusal("Another film is rendering. Try again in a minute.", 409, busy=True)
        try:
            topup_if_needed(need)
        except Exception as e:  # noqa: BLE001
            raise Refusal(f"The studio's generation balance is low and the agent could not top it up: {str(e)[:200]}", 503)
        launch(s, stage, only, refilm)
        if s.get("payment"):
            s["payment"]["used"] = True
            save(s)


def core_shoot(s):
    if not s.get("name"):
        raise Refusal("Shooting needs a storyboard.")
    if s["phase"] not in ("board", "keyframes"):
        raise Refusal("The stills are already shot.", 409)
    core_start(s, "keyframes")


def core_redo(s, kid, prompt=""):
    if not s.get("name"):
        raise Refusal("No storyboard.")
    cfg = load_cfg(s["name"])
    kf = next((k for k in cfg["keyframes"] if k["id"] == str(kid or "").upper()), None)
    if not kf:
        raise Refusal("Unknown still.")
    if s["phase"] != "keyframes":
        raise Refusal("Stills can be reshot before the film is made.", 409)
    if allowance(s["name"])["reshoots"] < 1:
        raise Refusal("This film has used all of its reshoots.", 409)
    prompt = str(prompt or "").strip()
    if prompt:
        kf["prompt"] = re.sub(r"\s*[—–]\s*", ", ", prompt)[:2000]
        save_cfg(cfg)
    core_start(s, "keyframes", only=kf["id"])


def core_refilm(s, move, prompt=""):
    """Film one camera move again after delivery, optionally with a new direction.
    The words and layout stay; the site and zip are rebuilt with the new move."""
    if not s.get("name"):
        raise Refusal("No film.")
    if s["phase"] != "deliver":
        raise Refusal("Moves can be refilmed once the film is made.", 409)
    cfg = load_cfg(s["name"])
    move = str(move or "").upper().replace("->", "-")
    if not any(f"{a['id']}-{b['id']}" == move for a, b in zip(cfg["keyframes"], cfg["keyframes"][1:])):
        raise Refusal("Unknown camera move.")
    if allowance(s["name"])["refilms"] < 1:
        raise Refusal("This film has used all of its refilms.", 409)
    prompt = re.sub(r"\s*[—–]\s*", ", ", str(prompt or "").strip())[:800]
    if prompt:
        if not prompt.lower().startswith("one continuous"):
            prompt = "One continuous slow camera move, no cuts: " + prompt
        cfg["transitions"][move] = prompt
        save_cfg(cfg)
    core_start(s, "film", refilm=move)


def core_film(s):
    if not s.get("name") or s["phase"] != "keyframes":
        raise Refusal("Filming starts once the stills are in.", 409)
    if any(not k["url"] for k in board_view(s["name"])["keyframes"]):
        raise Refusal("Some stills are missing; reshoot them first.", 409)
    if not (is_paid(s) or s.get("code_ok")):
        raise Refusal("This step needs payment first.", 402, need_payment=True)
    if any_running():
        raise Refusal("Another film is rendering. Try again in a minute.", 409, busy=True)
    cfg = load_cfg(s["name"])
    try:  # the words are written once the pictures exist
        copy = copywriter.write(cfg)
        cfg.setdefault("copy_history", []).append(cfg.get("copy"))
        cfg["copy"] = copy
        save_cfg(cfg)
    except RuntimeError:
        pass  # the director's copy stays; it can be rewritten after delivery
    core_start(s, "film")
    name = s["name"]

    def typeset():  # while the camera rolls, the typographer lays out the words on the stills
        try:
            lay = typographer.lay_out(load_cfg(name))
            c = load_cfg(name)
            c["copy"]["layout"] = lay
            save_cfg(c)
            rebuild_site(c)
        except Exception:  # noqa: BLE001  the default layout stays
            pass
    threading.Thread(target=typeset, daemon=True).start()


def _film_cfg(s):
    if not s.get("name"):
        raise Refusal("No film.")
    return load_cfg(s["name"])


def core_rewrite(s, note=""):
    cfg = _film_cfg(s)
    try:
        copy = copywriter.write(cfg, note=str(note or "")[:400])
    except RuntimeError as e:
        raise Refusal(str(e)[:300], 502)
    cfg.setdefault("copy_history", []).append(cfg.get("copy"))
    cfg["copy"] = copy
    save_cfg(cfg)
    rebuild_site(cfg)
    return copy


def core_layout(s, note=""):
    cfg = _film_cfg(s)
    try:
        lay = typographer.lay_out(cfg, note=str(note or "")[:400])
    except RuntimeError as e:
        raise Refusal(str(e)[:300], 502)
    cfg.setdefault("copy_history", []).append(json.loads(json.dumps(cfg.get("copy"))))  # a snapshot, not a reference
    cfg["copy"]["layout"] = lay
    save_cfg(cfg)
    rebuild_site(cfg)
    return cfg["copy"]


def core_set_copy(s, new_copy, snapshot=False):
    cfg = _film_cfg(s)
    if snapshot:
        cfg.setdefault("copy_history", []).append(json.loads(json.dumps(cfg.get("copy"))))
    cfg["copy"] = clean_copy(new_copy or {}, cfg)
    save_cfg(cfg)
    rebuild_site(cfg)
    return cfg["copy"]


def core_set_words(s, args):
    """The agent's exact edits: only the fields it names change."""
    cfg = _film_cfg(s)
    cur = json.loads(json.dumps(cfg.get("copy") or {}))
    cur.setdefault("beats", [a.get("text", "") for a in cur.get("acts", [])])
    for k in ("title", "tagline"):
        if isinstance(args.get(k), str):
            cur[k] = args[k]
    cta = dict(cur.get("cta") or {})
    if isinstance(args.get("cta_label"), str):
        cta["label"] = args["cta_label"]
    if isinstance(args.get("cta_href"), str):
        cta["href"] = args["cta_href"]
    cur["cta"] = cta
    if isinstance(args.get("beats"), list):
        cur["beats"] = [str(b) for b in args["beats"]]
    return core_set_copy(s, cur, snapshot=True)


# --- the director agent ----------------------------------------------------------------------------

AGENT_SELF_RESHOOTS = int(os.environ.get("AGENT_SELF_RESHOOTS", 2))  # reshoots on its own judgement, per film
AGENT_STEPS_PER_TURN = 8
AGENT_STEPS_PER_FILM = int(os.environ.get("AGENT_STEPS_PER_FILM", 90))
CHAT_LOCK = threading.RLock()
ORDER_LOCK = threading.Lock()
WAITING_TOOLS = {"shoot", "reshoot", "film", "refilm"}


def rendering(s):
    return bool(s.get("job")) and job_state(s["job"])[0]


def agent_of(s):
    return s.setdefault("agent", {"busy": False, "waiting": None, "mode": "chat", "self_reshoots": 0,
                                  "steps": 0, "inspection": None, "events": []})


def chat(s, who, text, **extra):
    """who: you (the customer), agent (what it says), action (what it did, shown small)."""
    if not text:
        return
    with CHAT_LOCK:
        s.setdefault("chat", []).append({"who": who, "text": str(text)[:1500], "at": time.time(), **extra})
        s["chat"] = s["chat"][-200:]
        save(s)


def film_state(s):
    """What the agent sees: the film, the money rules, never the costs."""
    a = agent_of(s)
    st = {"phase": s["phase"], "pitch": s.get("pitch", "")[:1500], "images_attached": len(s.get("refs") or []),
          "paid": is_paid(s) or bool(s.get("code_ok")), "last_error": s.get("error"),
          "rendering_now": (s["job"]["stage"] if rendering(s) else None)}
    if s.get("brief"):
        b = s["brief"]
        st["brief"] = {k: b.get(k) for k in ("name", "subject", "mood", "audience", "goal", "type", "style", "scenes")}
        st["open_questions"] = [q.get("question") for q in b.get("questions") or []][:4]
    if s.get("controls"):
        st["controls"] = s["controls"]
    if a.get("mode") != "autopilot":  # fixed prices by size; the only numbers the director may quote
        st["price_list_usd"] = {res: {f"{n} camera moves": usd for n, usd in t.items()}
                                for res, t in customer_prices()["prices"].items()}
    if s.get("name") and os.path.exists(cfg_path(s["name"])):
        bv = board_view(s["name"])
        st["price_usd"] = price_usd(s)
        st["size_now"] = {"camera_moves": len(bv["moves"]), "stills": len(bv["keyframes"]), "quality": bv["resolution"]}
        st["title"] = bv["copy"].get("title")
        st["shots"] = [{"id": k["id"], "shot": copywriter.shot_gist(k["prompt"]), "done": bool(k["url"])} for k in bv["keyframes"]]
        st["moves"] = [{"id": m["id"], "filmed": bool(m["url"])} for m in bv["moves"]]
        st["allowance_left"] = allowance(s["name"])
        st["your_reshoots_left"] = max(0, AGENT_SELF_RESHOOTS - a.get("self_reshoots", 0))
        if bv["site"]:
            st["words"] = {k: bv["copy"].get(k) for k in ("title", "tagline", "beats", "cta")}
            st["site_ready"] = True
    if a.get("inspection"):
        st["your_inspection"] = a["inspection"]
    if (s.get("payment") or {}).get("locked"):
        st["paid_for"] = s["payment"]["locked"]
    return st


def conversation(s):
    lines = []
    for m in (s.get("chat") or [])[-30:]:
        who = {"you": "customer", "agent": "you said", "action": "result"}.get(m["who"], m["who"])
        why = f" ({m['detail'][:400]})" if m.get("ok") is False and m.get("detail") else ""
        lines.append(f"[{who}] {m['text']}{why}")
    return lines or ["(nothing yet)"]


def run_tool(s, tool, args):
    """Run one tool for the agent. Returns (result text for the agent, waits_for_render)."""
    a = agent_of(s)
    if tool == "brief":
        b = core_intake(s, args.get("pitch") or s.get("pitch"))
        s["phase"] = "confirm"
        save(s)
        return (f"Brief: {json.dumps({k: b.get(k) for k in ('name', 'subject', 'mood', 'audience', 'goal', 'type', 'style', 'scenes')})}. "
                f"Open questions: {[q['question'] for q in b.get('questions') or []]}"), False
    if tool == "storyboard":
        if not s.get("brief"):
            core_intake(s, s.get("pitch"))
        size = args.get("scenes") or args.get("moves") or args.get("camera_moves")
        size = int(re.search(r"\d+", str(size))[0]) if size and re.search(r"\d+", str(size)) else None
        res = str(args.get("resolution") or "").upper().replace("1080", "1080P").replace("PP", "P").replace("768", "768P").replace("PP", "P")
        core_storyboard(s, {"type": args.get("type"), "style": args.get("style"), "scenes": size,
                            "resolution": res if res in ("768P", "1080P") else None}, notes=args.get("notes", ""))
        a["inspection"] = None
        a["self_reshoots"] = 0
        bv = board_view(s["name"])
        shots = "; ".join(f"{k['id']}: {copywriter.shot_gist(k['prompt'])}" for k in bv["keyframes"])
        paid = is_paid(s) or s.get("code_ok")
        return (f"Storyboard '{bv['copy'].get('title')}': {len(bv['moves'])} camera moves at {bv['resolution']}, "
                f"{len(bv['keyframes'])} stills: {shots}. "
                + ("Already paid." if paid else f"Price ${price_usd(s):.2f}; the customer has not paid yet.")), False
    if tool == "shoot":
        core_shoot(s)
        a["inspection"] = None
        return "Shooting the stills now.", True
    if tool == "inspect":
        if not s.get("name"):
            raise Refusal("No stills yet.")
        rd = run_dir(s["name"])
        stills = [(k["id"], scrollsite.keyframe_path(rd, k["id"])) for k in load_cfg(s["name"])["keyframes"]]
        stills = [(i, p) for i, p in stills if p]
        if not stills:
            raise Refusal("No stills yet.")
        try:
            res = agent.critique(load_cfg(s["name"]), stills)
        except agent.AgentError as e:
            raise Refusal(f"Could not inspect: {e}", 502)
        a["inspection"] = res
        save(s)
        return "Scores: " + "; ".join(f"{x['id']} {x['score']}/10{(' (' + x['issue'] + ')') if x['issue'] else ''}"
                                       for x in res["shots"]) + f". {res['overall']}", False
    if tool == "reshoot":
        mine = s.get("_turn_trigger") != "customer"
        if mine and a.get("self_reshoots", 0) >= AGENT_SELF_RESHOOTS:
            raise Refusal(f"You have used your {AGENT_SELF_RESHOOTS} reshoots on your own judgement; ask the customer.", 409)
        core_redo(s, args.get("id"), args.get("prompt", ""))
        if mine:
            a["self_reshoots"] = a.get("self_reshoots", 0) + 1
        a["inspection"] = None
        chat(s, "action", f"Reshooting still {str(args.get('id')).upper()}: {str(args.get('reason') or '')[:200]}",
             tool="reshoot")
        return f"Reshooting {str(args.get('id')).upper()}.", True
    if tool == "film":
        core_film(s)
        return "Writing the words, then filming the camera moves.", True
    if tool == "refilm":
        core_refilm(s, args.get("id"), args.get("prompt", ""))
        return f"Refilming {str(args.get('id')).upper()}.", True
    if tool == "rewrite":
        c = core_rewrite(s, args.get("note", ""))
        return f"New words: title '{c.get('title')}', tagline '{c.get('tagline')}'.", False
    if tool == "layout":
        c = core_layout(s, args.get("note", ""))
        return f"New layout: {(c.get('layout') or {}).get('note', 'done')}", False
    if tool == "set_words":
        c = core_set_words(s, args)
        return f"Words set: title '{c.get('title')}', tagline '{c.get('tagline')}'.", False
    return "", False


TOOL_TRIES = {"brief": "read the pitch", "storyboard": "draft the storyboard", "inspect": "check the stills",
              "reshoot": "reshoot that still", "shoot": "start shooting", "film": "start filming", "refilm": "refilm that move",
              "rewrite": "rewrite the words", "layout": "lay out the words", "set_words": "change the words"}
TOOL_LABELS = {"brief": "Read the pitch", "storyboard": "Drafted the storyboard", "inspect": "Checked every still",
               "rewrite": "Rewrote the words", "layout": "Re-laid out the words", "set_words": "Changed the words",
               "shoot": "Started shooting the stills", "film": "Started filming", "refilm": "Started refilming a move"}


def true_prices(text):
    """The director may quote prices, but only real ones: a sentence with any other amount is dropped."""
    real = {v for t in customer_prices()["prices"].values() for v in t.values()}
    def honest(sentence):
        return all(any(abs(float(a.replace(",", "")) - v) < 0.005 for v in real)
                   for a in re.findall(r"\$\s?(\d[\d,]*(?:\.\d+)?)", sentence))
    return " ".join(x for x in re.split(r"(?<=[.!?])\s+", text) if honest(x))


def agent_turn(s, trigger):
    """Let the agent work until it waits for a render, the customer, or has nothing to do."""
    a = agent_of(s)
    s["_turn_trigger"] = trigger
    try:
        resize = a.pop("resize", None)
        if resize:
            a["doing"] = {"tool": "storyboard", "since": time.time()}
            save(s)
            try:
                result, _ = run_tool(s, "storyboard", {**resize, "notes": "Keep the same story and look; only the size changes."})
                chat(s, "action", TOOL_LABELS["storyboard"], tool="storyboard", detail=result[:600])
            except Refusal as e:
                chat(s, "action", f"Could not {TOOL_TRIES['storyboard']}", tool="storyboard", ok=False, detail=str(e)[:600])
        for _ in range(AGENT_STEPS_PER_TURN):
            if a.get("steps", 0) >= AGENT_STEPS_PER_FILM:
                chat(s, "agent", "I have done a lot of work on this film already; tell me what to change next.")
                a["waiting"] = "customer"
                return
            a["steps"] = a.get("steps", 0) + 1
            a.pop("doing", None)  # deciding again: no tool running
            try:
                d = agent.decide(film_state(s), conversation(s), mode=a.get("mode", "chat"),
                                 self_reshoots=AGENT_SELF_RESHOOTS)
            except agent.AgentError as e:
                chat(s, "agent", "I lost my train of thought for a moment. Say anything to wake me.")
                s["error"] = str(e)[:300]
                a["waiting"] = "customer"
                return
            if d["say"]:
                chat(s, "agent", true_prices(d["say"]))
            if d["tool"] in ("ask", "done"):
                a["waiting"] = "customer"
                if d["tool"] == "done" and a.get("mode") == "autopilot":
                    a["finished"] = s["phase"] == "deliver"
                return
            a["doing"] = {"tool": d["tool"], "since": time.time()}  # shown live while a slow step runs
            save(s)
            try:
                result, waits = run_tool(s, d["tool"], d["args"])
            except Exception as e:  # noqa: BLE001  never end a turn in silence
                if isinstance(e, Refusal):
                    e2 = e
                else:
                    sys.stderr.write(f"agent tool {d['tool']}: {type(e).__name__}: {e}\n")
                    e2 = Refusal(f"{type(e).__name__}: {e}"[:300], 502)
                e = e2
                if e.extra.get("busy"):
                    if rendering(s):
                        chat(s, "action", f"Could not {TOOL_TRIES.get(d['tool'], d['tool'])} yet: this film is still rendering.",
                             tool=d["tool"], ok=False)
                        return
                    chat(s, "action", "Waiting for the camera: another film is rendering.", tool=d["tool"])
                    a["waiting"] = "slot"
                    return
                chat(s, "action", f"Could not {TOOL_TRIES.get(d['tool'], d['tool'])}", tool=d["tool"], ok=False,
                     detail=str(e)[:600])
                if e.extra.get("need_payment") and a.get("mode") == "chat":
                    a["waiting"] = "payment"
                continue
            if d["tool"] in TOOL_LABELS:
                chat(s, "action", TOOL_LABELS[d["tool"]], tool=d["tool"], detail=result[:600])
            if waits:
                a["waiting"] = "job"
                return
        a["waiting"] = "customer"
    finally:
        if rendering(s):
            a["waiting"] = "job"  # whatever was said, the render's outcome still wakes the agent
        a["busy"] = False
        a.pop("doing", None)
        s.pop("_turn_trigger", None)
        save(s)


def customer_did(s, text):
    """A decision made with a button: it reads in the conversation as the customer's own words,
    and the director is woken when the render it started finishes."""
    chat(s, "you", text)
    a = agent_of(s)
    if rendering(s) and not a.get("busy"):
        a["waiting"] = "job"
        save(s)


def kick(s, trigger="customer"):
    """Wake the agent in the background. 'customer' when a person spoke or acted."""
    a = agent_of(s)
    with CHAT_LOCK:
        if a.get("busy"):
            a["again"] = trigger
            return
        a["busy"], a["waiting"] = True, None
        save(s)

    def run():
        t = trigger
        while True:
            agent_turn(s, t)
            with CHAT_LOCK:
                t = a.pop("again", None)
                if not t:
                    return
                a["busy"] = True
    threading.Thread(target=run, daemon=True).start()


def render_outcome(s):
    """A finished render, told to the agent as a result line."""
    if s.get("error"):
        return f"The render stopped: {s['error']}"
    if s["phase"] == "keyframes":
        b = board_view(s["name"])
        done = sum(1 for k in b["keyframes"] if k["url"])
        return f"The stills are in ({done} of {len(b['keyframes'])})."
    if s["phase"] == "deliver":
        return "The film is finished; the site and zip are ready."
    return f"The render ended; the film is at '{s['phase']}'."


def watcher():
    """Wakes agents whose render finished, and retries ones waiting for the camera."""
    while True:
        time.sleep(3)
        for s in list(SESSIONS.values()):
            a = s.get("agent") or {}
            try:
                if a.get("waiting") == "job" and not a.get("busy") and s.get("job"):
                    settle(s)
                    if not job_state(s["job"])[0]:
                        a["waiting"] = None
                        chat(s, "action", render_outcome(s), tool="render", ok=not s.get("error"))
                        kick(s, "self")
                elif a.get("waiting") == "slot" and not a.get("busy") and not any_running():
                    a["waiting"] = None
                    kick(s, "self")
            except Exception as e:  # noqa: BLE001  one broken session must not stop the others
                sys.stderr.write(f"watcher: {s.get('id')}: {e}\n")


# --- films ordered by other agents (x402) ----------------------------------------------------------

FACILITATOR = os.environ.get("X402_FACILITATOR", "https://facilitator.payai.network")
ORDER_FIELDS = "brief (required), scenes 2-6, resolution 768P|1080P, type, style, images (up to 6 https URLs)"


def order_terms(data):
    """The film an agent asks for, and its price. Raises Refusal on a bad order."""
    brief = str(data.get("brief") or "").strip()
    if len(brief) < 10:
        raise Refusal(f"Describe the site in 'brief'. Fields: {ORDER_FIELDS}.")
    scenes = max(director.MIN_SCENES, min(director.MAX_SCENES, int(data.get("scenes") or 3)))
    res = data.get("resolution") if data.get("resolution") in ("768P", "1080P") else "768P"
    images = [u for u in (data.get("images") or []) if isinstance(u, str) and u.startswith("https://")][:MAX_REFS]
    terms = {"brief": brief[:3000], "scenes": scenes, "resolution": res, "images": images,
             "type": data.get("type") if data.get("type") in director.TYPES else None,
             "style": data.get("style") if data.get("style") in director.STYLES else None}
    terms["price_usd"] = customer_prices()["prices"][res][scenes]
    return terms


def x402_requirement(terms, url):
    return {"scheme": "exact", "network": "eip155:8453", "amount": str(int(round(terms["price_usd"] * 1e6))),
            "asset": USDC, "payTo": PAY_TO, "maxTimeoutSeconds": 600,
            "extra": {"name": "USD Coin", "version": "2"}, "resource": url}


def x402_challenge(terms, url):
    req = x402_requirement(terms, url)
    req.pop("resource")
    return {"x402Version": 2, "error": "Payment required",
            "resource": {"url": url, "mimeType": "application/json",
                         "description": f"A scroll-driven film website, {terms['scenes']} camera moves at {terms['resolution']}, "
                                        "made end to end by the Frameline director agent."},
            "accepts": [req]}


def facilitate(step, payload, requirement):
    body = {"x402Version": payload.get("x402Version", 2), "paymentPayload": payload, "paymentRequirements": requirement}
    req = urllib.request.Request(f"{FACILITATOR}/{step}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "frameline/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"isValid": False, "success": False, "invalidReason": f"facilitator answered {e.code}"}


def take_x402(header_value, terms, url):
    """Verify and settle an x402 'exact' payment on Base. Returns a payment record."""
    try:
        payload = json.loads(base64.b64decode(header_value + "=" * (-len(header_value) % 4)))
    except ValueError:
        raise Refusal("The payment header is not base64 JSON.", 402)
    want = x402_requirement(terms, url)
    if payload.get("x402Version", 2) == 1:  # v1 clients: same payment, older field names
        want = {"scheme": "exact", "network": "base", "maxAmountRequired": want["amount"], "resource": url,
                "description": "Frameline film", "mimeType": "application/json", "payTo": PAY_TO,
                "maxTimeoutSeconds": 600, "asset": USDC, "extra": want["extra"]}
    else:
        want.pop("resource")
    auth = ((payload.get("payload") or {}).get("authorization") or {})
    if str(auth.get("to", "")).lower() != PAY_TO.lower():
        raise Refusal("That payment is not to the studio.", 402)
    if int(auth.get("value") or 0) < int(round(terms["price_usd"] * 1e6)):
        raise Refusal(f"This film is {terms['price_usd']:.2f} USDC.", 402)
    nonce = str(auth.get("nonce") or "").lower()
    v = facilitate("verify", payload, want)
    if not v.get("isValid"):
        raise Refusal(f"The payment did not verify: {v.get('invalidReason') or v}", 402)
    st = facilitate("settle", payload, want)
    if not st.get("success") or not st.get("transaction"):
        raise Refusal(f"The payment did not settle: {st.get('errorReason') or st}", 402)
    return {"kind": "usdc", "via": "x402", "address": str(st.get("payer") or auth.get("from") or "").lower(),
            "usd": int(auth["value"]) / 1e6, "tx": str(st["transaction"]).lower(), "nonce": nonce,
            "receipt": {"success": True, "transaction": st["transaction"], "network": st.get("network", "eip155:8453"),
                        "payer": st.get("payer")}}


def take_transfer(tx, terms):
    """A plain USDC transfer to the studio wallet, for agents without x402."""
    tx = str(tx or "").strip().lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", tx):
        raise Refusal("X-Payment-Tx must be a transaction hash.", 402)
    got = evm.usdc_payment(tx, USDC, PAY_TO)
    if got is None:
        raise Refusal("That transfer is not confirmed yet; retry in a few seconds.", 402, pending=True)
    if not got["ok"]:
        raise Refusal(got["reason"], 402)
    if time.time() - got["block_time"] > PAYMENT_MAX_AGE_HOURS * 3600:
        raise Refusal(f"That payment is older than {PAYMENT_MAX_AGE_HOURS:g} hours.", 402)
    if got["units"] < int(round(terms["price_usd"] * 1e6)):
        raise Refusal(f"That transfer is {got['units'] / 1e6:.2f} USDC; this film is {terms['price_usd']:.2f}.", 402)
    return {"kind": "usdc", "via": "transfer", "address": got["from"], "usd": got["units"] / 1e6, "tx": tx}


def fetch_image(url):
    req = urllib.request.Request(url, headers={"User-Agent": "frameline/0.3"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(15 * 1024 * 1024 + 1)
    if len(raw) > 15 * 1024 * 1024:
        raise ValueError("over 15 MB")
    os.makedirs(UPLOADS, exist_ok=True)
    pid = secrets.token_hex(6)
    src, dst = os.path.join(UPLOADS, f"{pid}.src"), os.path.join(UPLOADS, f"{pid}.jpg")
    open(src, "wb").write(raw)
    r = subprocess.run([scrollsite.FFMPEG, "-y", "-loglevel", "error", "-i", src, "-vf", "scale='min(1600,iw)':-2",
                        "-q:v", "3", dst], capture_output=True)
    os.remove(src)
    if r.returncode != 0 or not os.path.exists(dst):
        raise ValueError("not an image")
    return dst


def start_order(terms, payment, base):
    """A paid order becomes a session the director agent runs on autopilot."""
    s = new_session()
    s["pitch"] = terms["brief"]
    s["controls"] = {"type": terms["type"] or "product", "style": terms["style"] or "", "scenes": terms["scenes"],
                     "resolution": terms["resolution"]}
    s["payment"] = {**{k: v for k, v in payment.items() if k != "receipt"}, "board": None,
                    "locked": {"scenes": terms["scenes"], "resolution": terms["resolution"]}}
    s["order"] = {"at": time.time(), "terms": terms}
    a = agent_of(s)
    a["mode"] = "autopilot"
    save(s)

    def go():
        notes = []
        for u in terms["images"]:
            try:
                s["refs"].append(fetch_image(u))
            except Exception as e:  # noqa: BLE001
                notes.append(f"{u[:80]} could not be used ({str(e)[:60]})")
        save(s)
        paid = "on the owner's pass, no charge" if payment["kind"] == "owner" else f"and paid {payment['usd']:.2f} USDC"
        chat(s, "you", f"[An AI agent ordered this film {paid}.] {terms['brief']}"
             + (f"\nWanted: type {terms['type']}." if terms["type"] else "") + (f" Look: {terms['style']}." if terms["style"] else "")
             + (f"\nImages attached: {len(s['refs'])}." if s["refs"] else "") + (f" Not usable: {'; '.join(notes)}." if notes else ""))
        kick(s, "customer")
    threading.Thread(target=go, daemon=True).start()
    return s


def order_view(s, base):
    a = s.get("agent") or {}
    running = rendering(s)
    done = s["phase"] == "deliver" and not running and not a.get("busy")
    stuck = not done and not running and not a.get("busy") and a.get("waiting") == "customer"
    step = {"pitch": "Reading the brief", "questions": "Reading the brief", "confirm": "Planning the shots",
            "board": "Storyboard drafted", "shooting": "Shooting the stills", "keyframes": "Checking the stills",
            "filming": "Filming the camera moves", "deliver": "Finishing" if not done else "Done"}.get(s["phase"], s["phase"])
    out = {"id": s["id"], "state": "done" if done else "needs_attention" if stuck else "working", "step": step,
           "notes": [m["text"] for m in (s.get("chat") or []) if m["who"] in ("agent", "action")][-12:],
           "page": f"{base}/f/{s['id']}", "edit": f"{base}/studio?session={s['id']}"}
    if s.get("name") and os.path.exists(cfg_path(s["name"])):
        b = board_view(s["name"])
        rd = run_dir(s["name"])
        out["title"] = b["copy"].get("title")
        out["stills"] = [base + k["url"].split("?")[0] for k in b["keyframes"] if k["url"]]
        if b["site"]:
            out["result"] = {"site": base + b["site"].split("?")[0], "zip": base + b["zip"] if b["zip"] else None,
                             "film": base + run_url(os.path.join(rd, "site", "film.mp4")),
                             "film_mobile": base + run_url(os.path.join(rd, "site", "film-mobile.mp4")),
                             "poster": base + run_url(os.path.join(rd, "site", "poster.jpg")),
                             "page": out["page"], "edit": out["edit"]}
    if s.get("error") and not done:
        out["error"] = s["error"]
    return out


def llms_txt(base):
    c = service_card(base)
    return (f"# Frameline\n\n> {c['what']}\n\n## Hire it (for agents)\n\n"
            f"- Order: POST {c['order']['url']} with JSON: {c['order']['body']}.\n"
            f"- Pay: {c['order']['payment']}\n- Status: GET {c['status']['url']} ({c['status']['states']})\n"
            f"- Reply to the director: POST {c['reply']['url']} with {c['reply']['body']}. {c['reply']['when']}\n"
            f"- Time: {c['time']}\n- Service card (JSON): {base}/api/agent\n\n## About\n\n{c['operator']}\n")


def service_card(base):
    """What another agent needs to know to hire the studio."""
    p = customer_prices()["prices"]
    return {
        "name": "Frameline", "kind": "x402 service",
        "what": "Make a scroll-driven cinematic website from a brief: stills, a continuous film between them, "
                "words laid over it. Delivered as a hosted page, a live site, an mp4 and a zip.",
        "order": {"method": "POST", "url": f"{base}/api/agent/films", "body": ORDER_FIELDS,
                  "payment": "x402 v2 'exact' USDC on Base (eip155:8453): POST without payment for the 402 challenge, "
                             "then again with PAYMENT-SIGNATURE (or X-PAYMENT). Or send USDC to payTo and retry "
                             "with header X-Payment-Tx: <hash>.",
                  "pay_to": PAY_TO, "asset": USDC},
        "prices_usd": {res: {f"{n} moves": v for n, v in t.items()} for res, t in p.items()},
        "status": {"method": "GET", "url": f"{base}/api/agent/films/<id>",
                   "states": "working, done, needs_attention; when done, 'result' has site, film, zip and the page "
                             "to hand to a person, who can edit the words at 'edit'."},
        "reply": {"method": "POST", "url": f"{base}/api/agent/films/<id>/reply", "body": '{"text": "..."}',
                  "when": "The film needs attention, or you want a change (new words, another angle). No new payment."},
        "time": "About 5 to 10 minutes per film.",
        "operator": "The studio is itself a 1Claw agent: it signs with 1Claw-held keys, pays its own generation bills "
                    "within 1Claw guardrails, and keeps its payment ledger in 1Claw memory.",
    }


# --- HTTP ---------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Frameline/0.2"

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else "") and "/api/events" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, status_code=200, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status_code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def send_file(self, path):
        if not path or not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m:
            start = int(m[1]) if m[1] else 0
            end = min(int(m[2]) if m[2] else size - 1, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if path.endswith((".html", ".js", ".css")):
            self.send_header("Cache-Control", "no-cache")
        if path.endswith(".zip"):
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    def safe_join(self, base, rel):
        p = os.path.realpath(os.path.join(base, urllib.parse.unquote(rel)))
        return p if p.startswith(os.path.realpath(base) + os.sep) else None

    def session(self, q, data=None):
        sid = (q.get("session") or [""])[0] or (data or {}).get("session", "")
        return SESSIONS.get(sid)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        p, q = url.path, urllib.parse.parse_qs(url.query)
        try:
            if p in ("/", "/index.html"):
                return self.send_file(os.path.join(STATIC, "index.html"))
            if p in ("/textlayer.css", "/textlayer.js"):
                return self.send_file(os.path.join(ROOT, "templates", p[1:]))
            if p in ("/docs", "/docs/"):
                return self.send_file(os.path.join(STATIC, "docs.html"))
            if p in ("/studio", "/studio/"):
                return self.send_file(os.path.join(STATIC, "studio.html"))
            for prefix, base in (("/static/", STATIC), ("/runs/", RUNS), ("/uploads/", UPLOADS)):
                if p.startswith(prefix):
                    return self.send_file(self.safe_join(base, p[len(prefix):]))
            if p in ("/api/agent", "/.well-known/x402"):
                return self.send_json(service_card(self.base_url()))
            if p == "/llms.txt":
                return self.send_text(llms_txt(self.base_url()))
            m = re.fullmatch(r"/api/agent/films/([0-9a-f]{16})", p)
            if m:
                s = SESSIONS.get(m[1])
                return self.send_json(order_view(s, self.base_url())) if s else self.send_json({"error": "No such film."}, 404)
            if re.fullmatch(r"/f/[0-9a-f]{16}", p):
                return self.send_file(os.path.join(STATIC, "film.html"))
            if p == "/api/runs":
                return self.send_json({"runs": list_runs()})
            if p == "/api/status":
                return self.send_json({**status(), "runs": list_runs()})
            if p == "/api/showcase":
                return self.send_json(showcase())
            if p == "/api/prices":
                return self.send_json(customer_prices())
            if p == "/api/session":
                s = self.session(q)
                return self.send_json(view(s)) if s else self.send_json({"error": "Unknown session."}, 404)
            if p == "/api/events":
                s = self.session(q)
                return self.api_events(s) if s else self.send_error(404)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": f"{type(e).__name__}: {e}"[:400]}, 500)
        self.send_error(404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        p, q = url.path, urllib.parse.parse_qs(url.query)
        try:
            if p == "/api/session":
                return self.send_json(view(new_session()))
            if p == "/api/upload":
                return self.api_upload(self.session(q))
            if p in LLM_STEPS and not self.rate_ok():
                return self.send_json({"error": "That is a lot of drafting for one hour. Try again a little later."}, 429)
            data = json.loads(self.body() or b"{}")
            if p == "/api/open":
                return self.api_open(data)
            if p == "/api/agent/films":
                return self.api_order(data)
            m = re.fullmatch(r"/api/agent/films/([0-9a-f]{16})/reply", p)
            if m:
                return self.api_order_reply(SESSIONS.get(m[1]), data)
            s = self.session(q, data)
            if not s:
                return self.send_json({"error": "Unknown session. Reload the page."}, 400)
            if self.has_code():
                s["code_ok"] = True
            handler = {"/api/chat": self.api_chat, "/api/resize": self.api_resize, "/api/agent/settings": self.api_agent_settings, "/api/ref/remove": self.api_ref_remove, "/api/intake": self.api_intake,
                       "/api/answer": self.api_answer, "/api/storyboard": self.api_storyboard,
                       "/api/shoot": self.api_shoot, "/api/redo": self.api_redo, "/api/refilm": self.api_refilm, "/api/film": self.api_film,
                       "/api/copy": self.api_copy, "/api/layout": self.api_layout, "/api/undo": self.api_undo, "/api/rewrite": self.api_rewrite, "/api/back": self.api_back,
                       "/api/pay/challenge": self.api_pay_challenge, "/api/pay/owner": self.api_pay_owner,
                       "/api/pay/confirm": self.api_pay_confirm}.get(p)
            if p in PAID_STEPS and not (is_paid(s) or self.has_code()):
                return self.send_json({"error": "This step needs payment first.", "need_payment": True}, 402)
            if handler:
                return handler(s, data)
        except Refusal as e:
            return self.send_json({"error": customer_error(str(e)), **e.extra}, e.status)
        except Exception as e:  # noqa: BLE001  surface errors to the UI instead of dropping the socket
            return self.send_json({"error": f"{type(e).__name__}: {e}"[:400]}, 500)
        self.send_error(404)

    def base_url(self):
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        local = re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", host)
        proto = self.headers.get("X-Forwarded-Proto") or ("http" if local else "https")  # runtimes sit behind TLS
        return f"{proto}://{host}"

    def send_text(self, text, ctype="text/plain; charset=utf-8"):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api_order(self, data):
        """Another agent orders a film. No payment: the x402 challenge. With payment: the film starts."""
        try:
            terms = order_terms(data)
        except Refusal as e:
            return self.send_json({"error": str(e), "service": service_card(self.base_url())}, e.status)
        base = self.base_url()
        url = base + "/api/agent/films"
        sig = self.headers.get("PAYMENT-SIGNATURE") or self.headers.get("X-PAYMENT")
        txh = self.headers.get("X-Payment-Tx")
        extra = {}
        try:
            if self.has_code():
                payment = {"kind": "owner", "address": "access-code", "usd": 0}
            elif sig:
                try:
                    peek = json.loads(base64.b64decode(sig + "=" * (-len(sig) % 4)))
                    nonce = str(((peek.get("payload") or {}).get("authorization") or {}).get("nonce") or "").lower()
                except ValueError:
                    nonce = ""
                with ORDER_LOCK:
                    again = next((x for x in SESSIONS.values() if nonce and (x.get("payment") or {}).get("nonce") == nonce), None)
                    if again:  # a retry of a payment already taken: the same film
                        return self.send_json(order_view(again, base), 202)
                    payment = take_x402(sig, terms, url)
                    self.record_payment(payment)
                extra["PAYMENT-RESPONSE"] = base64.b64encode(json.dumps(payment["receipt"]).encode()).decode()
            elif txh:
                with ORDER_LOCK:
                    if ledger.is_used(str(txh).strip().lower()):
                        return self.send_json({"error": "That payment has already been used."}, 409)
                    payment = take_transfer(txh, terms)
                    self.record_payment(payment)
            else:
                ch = x402_challenge(terms, url)
                return self.send_json({**ch, "price_usd": terms["price_usd"], "service": service_card(base)}, 402,
                                      {"PAYMENT-REQUIRED": base64.b64encode(json.dumps(ch).encode()).decode()})
        except Refusal as e:
            return self.send_json({"error": str(e), **e.extra}, e.status)
        except ledger.LedgerUnavailable as e:
            return self.send_json({"error": f"Could not check the payment record right now; retry. ({e})"}, 503)
        s = start_order(terms, payment, base)
        return self.send_json(order_view(s, base), 202, extra)

    def api_order_reply(self, s, data):
        """The ordering agent answers the director, e.g. when the film needs attention. Same film, no new payment."""
        if not s or not s.get("order"):
            return self.send_json({"error": "No such film."}, 404)
        text = str(data.get("text") or "").strip()[:2000]
        if not text:
            return self.send_json({"error": 'Send JSON {"text": "..."}.'}, 400)
        chat(s, "you", f"[The ordering agent replies.] {text}")
        kick(s, "customer")
        return self.send_json(order_view(s, self.base_url()), 202)

    def record_payment(self, payment):
        try:
            ledger.record({"tx": payment["tx"], "from": payment["address"], "usd": payment["usd"], "via": payment["via"],
                           "session": None, "board": "agent-order", "at": time.time()})
        except ledger.LedgerUnavailable as e:  # the money is in; the film goes ahead, and this is logged
            sys.stderr.write(f"ledger: could not record {payment['tx']}: {e}\n")

    def api_agent_settings(self, s, data):
        a = agent_of(s)
        if "review" in data:
            a["review"] = bool(data["review"])
        save(s)
        return self.send_json(view(s))

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def rate_ok(self):
        now, ip = time.time(), self.client_ip()
        hits = [t for t in RATE.get(ip, []) if now - t < 3600]
        if len(hits) >= LLM_LIMIT_PER_HOUR and not self.has_code():
            RATE[ip] = hits
            return False
        RATE[ip] = hits + [now]
        return True

    def has_code(self):
        return bool(ACCESS_CODE) and secrets.compare_digest(self.headers.get("X-Studio-Code", ""), ACCESS_CODE)

    # --- payment api ---
    def api_pay_challenge(self, s, data):
        """An owner wallet proves itself by signing a one-time message; everyone else pays."""
        addr = str(data.get("address") or "").lower()
        if not re.fullmatch(r"0x[0-9a-f]{40}", addr):
            return self.send_json({"error": "That is not a wallet address."}, 400)
        if addr not in OWNER_WALLETS:
            return self.send_json({"owner": False})
        message = (f"Frameline: sign in as the studio owner.\n\nThis is free and moves no funds.\n\n"
                   f"Wallet: {addr}\nSession: {s['id']}\nNonce: {secrets.token_hex(12)}\n"
                   f"Issued: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        s["challenge"] = {"address": addr, "message": message, "expires": time.time() + 600}
        save(s)
        return self.send_json({"owner": True, "message": message})

    def api_pay_owner(self, s, data):
        ch = s.get("challenge") or {}
        if not ch or time.time() > ch.get("expires", 0):
            return self.send_json({"error": "The sign-in request expired. Try again."}, 400)
        try:
            signer = evm.personal_recover(ch["message"], str(data.get("signature") or ""))
        except (ValueError, TypeError) as e:
            return self.send_json({"error": f"That signature could not be read: {e}"}, 400)
        if signer != ch["address"] or signer not in OWNER_WALLETS:
            return self.send_json({"error": "That signature is not from an owner wallet."}, 403)
        s["payment"] = {"kind": "owner", "address": signer, "usd": 0, "at": time.time()}
        s.pop("challenge", None)
        save(s)
        self.paid(s, "The studio owner signed in: no charge.")
        return self.send_json(view(s))

    def paid(self, s, line):
        """Paying is the approval to shoot: the stills start straight away, and the director picks up when they land."""
        chat(s, "action", line, tool="pay")
        a = agent_of(s)
        if s["phase"] == "board" and not rendering(s):
            try:
                core_shoot(s)
                chat(s, "action", TOOL_LABELS["shoot"], tool="shoot")
                if not a.get("busy"):
                    a["waiting"] = "job"
                    save(s)
                return
            except Refusal as e:
                if e.extra.get("busy") and not a.get("busy"):
                    chat(s, "action", "Waiting for the camera: another film is rendering.", tool="shoot")
                    a["waiting"] = "slot"  # the watcher wakes the director, who shoots
                    save(s)
                    return
        if a.get("waiting") in ("payment", "customer") or any(m["who"] == "you" for m in s.get("chat") or []):
            kick(s, "customer")

    def api_pay_confirm(self, s, data):
        """Accept a USDC transfer to the studio wallet, read from Base, once per transaction."""
        tx = str(data.get("tx") or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{64}", tx):
            return self.send_json({"error": "That is not a transaction hash."}, 400)
        usd = price_usd(s)
        if usd is None:
            return self.send_json({"error": "Draft a storyboard first."}, 400)
        if is_paid(s):
            return self.send_json(view(s))
        with LOCK:
            try:
                if ledger.is_used(tx):
                    return self.send_json({"error": "That payment has already been used."}, 409)
            except ledger.LedgerUnavailable as e:
                return self.send_json({"error": f"Could not check the payment record right now. Try again. ({e})"}, 503)
            try:
                got = evm.usdc_payment(tx, USDC, PAY_TO)
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": f"Could not read Base right now: {str(e)[:160]}"}, 502)
            if got is None:
                return self.send_json({"status": "pending"})
            if not got["ok"]:
                return self.send_json({"error": got["reason"]}, 400)
            # The durable record makes each payment single-use, so a recent payment can be claimed
            # by a new session too (for example after a restart lost the one it was made in).
            if time.time() - got["block_time"] > PAYMENT_MAX_AGE_HOURS * 3600:
                return self.send_json({"error": f"That payment is older than {PAYMENT_MAX_AGE_HOURS:g} hours."}, 400)
            if got["units"] < int(round(usd * 1e6)):
                return self.send_json({"error": f"That transfer is {got['units'] / 1e6:.2f} USDC; this site is {usd:.2f}."}, 400)
            entry = {"tx": tx, "from": got["from"], "usd": got["units"] / 1e6, "session": s["id"],
                     "board": s["name"], "at": time.time()}
            try:
                ledger.record(entry)
            except ledger.LedgerUnavailable as e:
                return self.send_json({"error": f"Could not record the payment safely, so it is not used yet. Try again. ({e})"}, 503)
            s["payment"] = {"kind": "usdc", "address": got["from"], "usd": entry["usd"], "tx": tx, "board": s["name"]}
            save(s)
        self.paid(s, f"Payment received: {entry['usd']:.2f} USDC.")
        return self.send_json(view(s))

    # --- api ---
    def api_open(self, data):
        name = str(data.get("run") or "")
        if not re.fullmatch(r"[a-z0-9-]{1,60}", name) or not os.path.exists(cfg_path(name)):
            return self.send_json({"error": "No such film."}, 404)
        for s in SESSIONS.values():  # reuse the newest session already on this film
            if s.get("name") == name:
                return self.send_json(view(s))
        cfg = load_cfg(name)
        s = new_session()
        has_site = os.path.exists(os.path.join(run_dir(name), "site", "index.html"))
        s.update({"name": name, "pitch": cfg.get("brief", ""), "refs": [r for r in cfg.get("references", []) if os.path.exists(r)],
                  "phase": "deliver" if has_site else "keyframes"})
        save(s)
        return self.send_json(view(s))

    def api_upload(self, s):
        if not s:
            return self.send_json({"error": "Unknown session."}, 400)
        if len(s["refs"]) >= MAX_REFS:
            return self.send_json({"error": f"Up to {MAX_REFS} images."}, 400)
        raw = self.body()
        if not raw or len(raw) > 15 * 1024 * 1024:
            return self.send_json({"error": "Send one image under 15 MB."}, 400)
        os.makedirs(UPLOADS, exist_ok=True)
        pid = secrets.token_hex(6)
        src, dst = os.path.join(UPLOADS, f"{pid}.src"), os.path.join(UPLOADS, f"{pid}.jpg")
        open(src, "wb").write(raw)
        r = subprocess.run([scrollsite.FFMPEG, "-y", "-loglevel", "error", "-i", src, "-vf", "scale='min(1600,iw)':-2",
                            "-q:v", "3", dst], capture_output=True)
        os.remove(src)
        if r.returncode != 0 or not os.path.exists(dst):
            return self.send_json({"error": "That file could not be read as an image."}, 400)
        s["refs"].append(dst)
        save(s)
        return self.send_json(view(s))

    def api_ref_remove(self, s, data):
        i = int(data.get("index", -1))
        if 0 <= i < len(s["refs"]):
            s["refs"].pop(i)
            save(s)
        return self.send_json(view(s))

    def api_intake(self, s, data):
        core_intake(s, data.get("pitch"))
        s["phase"] = "questions" if s["brief"]["questions"] else "confirm"
        save(s)
        return self.send_json(view(s))

    def api_answer(self, s, data):
        qs = {q["id"]: q for q in (s["brief"] or {}).get("questions", [])}
        answers = []
        for a in data.get("answers") or []:
            q = qs.get(a.get("id"))
            if q:
                answers.append({"id": q["id"], "field": q["field"], "question": q["question"],
                                "answer": str(a.get("answer") or "").strip()[:300]})
                if q["field"] in ("subject", "mood", "audience", "goal", "name") and a.get("answer"):
                    s["brief"][q["field"]] = str(a["answer"]).strip()[:200]
        s["answers"] = answers
        s["phase"] = "confirm"
        save(s)
        return self.send_json(view(s))

    def api_back(self, s, data):
        """Step back to an earlier screen (never past a running job)."""
        to = data.get("to")
        if rendering(s):
            return self.send_json({"error": "A render is running."}, 409)
        if to in ("pitch", "confirm", "board"):
            s["phase"] = to
            save(s)
        return self.send_json(view(s))

    def api_storyboard(self, s, data):
        core_storyboard(s, data.get("controls"), data.get("brief"), data.get("notes"))
        return self.send_json(view(s))

    def api_shoot(self, s, data):
        if not data.get("approve"):
            return self.send_json({"error": "Shooting needs explicit approval."}, 400)
        core_shoot(s)
        customer_did(s, "Shoot the stills.")
        return self.send_json(view(s))

    def api_redo(self, s, data):
        core_redo(s, data.get("id"), data.get("prompt"))
        customer_did(s, f"Reshoot still {str(data.get('id')).upper()}: {str(data.get('prompt') or '')[:300]}")
        return self.send_json(view(s))

    def api_refilm(self, s, data):
        core_refilm(s, data.get("id"), data.get("prompt"))
        customer_did(s, f"Refilm the move {str(data.get('id')).upper()}: {str(data.get('prompt') or '')[:300]}")
        return self.send_json(view(s))

    def api_film(self, s, data):
        if not data.get("approve"):
            return self.send_json({"error": "Filming needs explicit approval."}, 400)
        core_film(s)
        customer_did(s, "The stills are good. Film it.")
        return self.send_json(view(s))

    def api_copy(self, s, data):
        copy = core_set_copy(s, data.get("copy"))
        return self.send_json({"copy": copy, "saved": time.time()})

    def api_rewrite(self, s, data):
        return self.send_json({"copy": core_rewrite(s, data.get("note"))})

    def api_undo(self, s, data):
        """Step back to the words and layout before the agent's last rewrite or layout."""
        if not s.get("name"):
            return self.send_json({"error": "No film."}, 400)
        cfg = load_cfg(s["name"])
        history = [c for c in cfg.get("copy_history") or [] if isinstance(c, dict)]
        if not history:
            return self.send_json({"error": "Nothing to undo."}, 400)
        cfg["copy"] = history.pop()
        cfg["copy_history"] = history
        save_cfg(cfg)
        rebuild_site(cfg)
        return self.send_json({"copy": cfg["copy"], "left": len(history)})

    def api_layout(self, s, data):
        return self.send_json({"copy": core_layout(s, data.get("note"))})

    def api_resize(self, s, data):
        """Longer, shorter or sharper, picked from the priced options: redrawn at exactly that size."""
        if is_paid(s) or s["phase"] not in ("board", "confirm") or rendering(s):
            return self.send_json({"error": "The size is set once it is paid for."}, 409)
        size = {}
        if data.get("scenes") is not None:
            size["scenes"] = max(director.MIN_SCENES, min(director.MAX_SCENES, int(data["scenes"])))
        if data.get("resolution") in ("768P", "1080P"):
            size["resolution"] = data["resolution"]
        if not size:
            return self.send_json({"error": "Pick a size."}, 400)
        customer_did(s, str(data.get("text") or "Change the size.")[:200])
        agent_of(s)["resize"] = size
        kick(s, "customer")
        return self.send_json(view(s))

    def api_chat(self, s, data):
        """The customer talks to the director agent; it works in the background."""
        text = str(data.get("text") or "").strip()[:2000]
        if not text:
            return self.send_json({"error": "Say something."}, 400)
        if not s.get("pitch") and s["phase"] == "pitch":
            s["pitch"] = text[:3000]
        chat(s, "you", text)
        kick(s, "customer")
        return self.send_json(view(s))

    def api_events(self, s):
        """Replays the current job's log as events, then follows it live until it ends."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        job = s.get("job")
        pos, buf = 0, ""
        try:
            while job:
                try:
                    with open(job["log"], errors="replace") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                except OSError:
                    chunk = ""
                buf += chunk
                *lines, buf = buf.split("\n")
                for line in lines:
                    for e in parse_line(line):
                        self.wfile.write(f"data: {json.dumps(e)}\n\n".encode())
                running, _ = job_state(job)
                if not running and not chunk:
                    break
                self.wfile.write(b": tick\n\n")
                self.wfile.flush()
                time.sleep(0.6)
            v = view(s)
            self.wfile.write(f"data: {json.dumps({'type': 'finished', 'view': v})}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    # Locally: 127.0.0.1:8080. On a 1Claw runtime: HOST=0.0.0.0 and the PORT the runtime sets.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "127.0.0.1")
    mimetypes.add_type("video/mp4", ".mp4")
    load_sessions()
    for s in SESSIONS.values():  # a restart interrupts any agent mid-thought; renders carry on
        if (s.get("agent") or {}).get("busy"):
            s["agent"]["busy"], s["agent"]["waiting"] = False, "job" if s.get("job") else "customer"
    threading.Thread(target=watcher, daemon=True).start()
    print(f"Frameline studio at http://{host}:{port} (payments to {PAY_TO}, {len(OWNER_WALLETS)} owner wallet(s))")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
