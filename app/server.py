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
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
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
LLM_STEPS = {"/api/intake", "/api/storyboard", "/api/rewrite", "/api/layout"}
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


# --- HTTP ---------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Frameline/0.2"

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else "") and "/api/events" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, status_code=200):
        data = json.dumps(obj).encode()
        self.send_response(status_code)
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
            s = self.session(q, data)
            if not s:
                return self.send_json({"error": "Unknown session. Reload the page."}, 400)
            handler = {"/api/ref/remove": self.api_ref_remove, "/api/intake": self.api_intake,
                       "/api/answer": self.api_answer, "/api/storyboard": self.api_storyboard,
                       "/api/shoot": self.api_shoot, "/api/redo": self.api_redo, "/api/refilm": self.api_refilm, "/api/film": self.api_film,
                       "/api/copy": self.api_copy, "/api/layout": self.api_layout, "/api/undo": self.api_undo, "/api/rewrite": self.api_rewrite, "/api/back": self.api_back,
                       "/api/pay/challenge": self.api_pay_challenge, "/api/pay/owner": self.api_pay_owner,
                       "/api/pay/confirm": self.api_pay_confirm}.get(p)
            if p in PAID_STEPS and not (is_paid(s) or self.has_code()):
                return self.send_json({"error": "This step needs payment first.", "need_payment": True}, 402)
            if handler:
                return handler(s, data)
        except Exception as e:  # noqa: BLE001  surface errors to the UI instead of dropping the socket
            return self.send_json({"error": f"{type(e).__name__}: {e}"[:400]}, 500)
        self.send_error(404)

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
        return self.send_json(view(s))

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
        pitch = str(data.get("pitch", "")).strip()[:3000]
        if len(pitch) < 3:
            return self.send_json({"error": "Tell me a little about what we are making."}, 400)
        s["pitch"] = pitch
        try:
            b = intake.ask(pitch, s["refs"])
        except RuntimeError as e:
            return self.send_json({"error": str(e)[:300]}, 502)
        s["brief"], s["answers"] = b, []
        s["controls"] = {"type": b["type"], "style": b["style"], "scenes": b["scenes"], "resolution": "768P"}
        s["phase"] = "questions" if b["questions"] else "confirm"
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
        if s.get("job") and job_state(s["job"])[0]:
            return self.send_json({"error": "A render is running."}, 409)
        if to in ("pitch", "confirm", "board"):
            s["phase"] = to
            save(s)
        return self.send_json(view(s))

    def api_storyboard(self, s, data):
        if not s.get("brief"):
            return self.send_json({"error": "Start with a pitch."}, 400)
        c = data.get("controls") or s["controls"] or {}
        controls = {"type": c.get("type") if c.get("type") in director.TYPES else "product",
                    "style": c.get("style") if c.get("style") in director.STYLES else "",
                    "scenes": max(director.MIN_SCENES, min(director.MAX_SCENES, int(c.get("scenes") or 3))),
                    "resolution": c.get("resolution") if c.get("resolution") in ("768P", "1080P") else "768P"}
        s["controls"] = controls
        for k in ("name", "subject", "mood", "audience", "goal"):
            if isinstance(data.get("brief", {}).get(k), str):
                s["brief"][k] = data["brief"][k].strip()[:200]
        b = s["brief"]
        base = re.sub(r"[^a-z0-9-]+", "-", str(b.get("name") or b.get("subject") or "film").lower()).strip("-")[:28]
        slug = f"{base or 'film'}-{secrets.token_hex(2)}"
        brief = intake.director_brief(s["pitch"], b, s["answers"])
        try:
            director.direct(slug, brief, refs=s["refs"], ptype=controls["type"], style=controls["style"],
                            scenes=controls["scenes"], resolution=controls["resolution"],
                            notes=str(data.get("notes") or "")[:400] or None)
        except SystemExit as e:
            return self.send_json({"error": str(e)[:400]}, 502)
        pay = s.get("payment") or {}
        if pay.get("kind") == "usdc" and not pay.get("used") and pay.get("usd", 0) + 1e-9 >= (price_usd({"name": slug}) or 1e9):
            pay["board"] = slug  # paid but nothing generated yet: the payment moves to the new storyboard
        s["name"], s["phase"], s["job"], s["error"] = slug, "board", None, None
        save(s)
        return self.send_json(view(s))

    def start(self, s, stage, only=None, refilm=None):
        cfg = load_cfg(s["name"])
        p = prices()
        n = len(cfg["keyframes"])
        clip = p["clip_usd"][cfg["video"].get("resolution", "768P")]
        need = (p["image_usd"] * (1 if only else n) if stage == "keyframes" else clip * (1 if refilm else n - 1))
        with LOCK:
            if any_running():
                return self.send_json({"error": "Another film is rendering. Try again in a minute."}, 409)
            try:
                topup_if_needed(need)
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": "The studio's generation balance is low and the agent could not "
                                                f"top it up: {str(e)[:200]}"}, 503)
            launch(s, stage, only, refilm)
            if s.get("payment"):
                s["payment"]["used"] = True
                save(s)
        return self.send_json(view(s))

    def api_shoot(self, s, data):
        if not data.get("approve") or not s.get("name"):
            return self.send_json({"error": "Shooting needs a storyboard and explicit approval."}, 400)
        return self.start(s, "keyframes")

    def api_redo(self, s, data):
        if not s.get("name"):
            return self.send_json({"error": "No storyboard."}, 400)
        cfg = load_cfg(s["name"])
        kf = next((k for k in cfg["keyframes"] if k["id"] == data.get("id")), None)
        if not kf:
            return self.send_json({"error": "Unknown keyframe."}, 400)
        if s["phase"] != "keyframes":
            return self.send_json({"error": "Stills can be reshot before the film is made."}, 409)
        if allowance(s["name"])["reshoots"] < 1:
            return self.send_json({"error": "This film has used all of its reshoots."}, 409)
        prompt = str(data.get("prompt") or "").strip()
        if prompt:
            kf["prompt"] = re.sub(r"\s*[\u2014\u2013]\s*", ", ", prompt)[:2000]
            save_cfg(cfg)
        return self.start(s, "keyframes", only=kf["id"])

    def api_refilm(self, s, data):
        """Film one camera move again after delivery, optionally with a new direction.
        The words and layout stay; the site and zip are rebuilt with the new move."""
        if not s.get("name"):
            return self.send_json({"error": "No film."}, 400)
        if s["phase"] != "deliver":
            return self.send_json({"error": "Moves can be refilmed once the film is made."}, 409)
        cfg = load_cfg(s["name"])
        move = str(data.get("id") or "").upper()
        if move not in cfg["transitions"] and not any(f"{a['id']}-{b['id']}" == move
                                                       for a, b in zip(cfg["keyframes"], cfg["keyframes"][1:])):
            return self.send_json({"error": "Unknown camera move."}, 400)
        if allowance(s["name"])["refilms"] < 1:
            return self.send_json({"error": "This film has used all of its refilms."}, 409)
        prompt = re.sub(r"\s*[\u2014\u2013]\s*", ", ", str(data.get("prompt") or "").strip())[:800]
        if prompt:
            if not prompt.lower().startswith("one continuous"):
                prompt = "One continuous slow camera move, no cuts: " + prompt
            cfg["transitions"][move] = prompt
            save_cfg(cfg)
        return self.start(s, "film", refilm=move)

    def api_film(self, s, data):
        if not data.get("approve") or not s.get("name"):
            return self.send_json({"error": "Filming needs explicit approval."}, 400)
        if any_running():
            return self.send_json({"error": "Another film is rendering. Try again in a minute."}, 409)
        cfg = load_cfg(s["name"])
        try:  # the words are written once the pictures exist
            copy = copywriter.write(cfg)
            cfg.setdefault("copy_history", []).append(cfg.get("copy"))
            cfg["copy"] = copy
            save_cfg(cfg)
        except RuntimeError:
            pass  # the director's copy stays; it can be rewritten after delivery
        resp = self.start(s, "film")
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
        return resp

    def api_copy(self, s, data):
        if not s.get("name"):
            return self.send_json({"error": "No film."}, 400)
        cfg = load_cfg(s["name"])
        cfg["copy"] = clean_copy(data.get("copy") or {}, cfg)
        save_cfg(cfg)
        rebuild_site(cfg)
        return self.send_json({"copy": cfg["copy"], "saved": time.time()})

    def api_rewrite(self, s, data):
        if not s.get("name"):
            return self.send_json({"error": "No film."}, 400)
        cfg = load_cfg(s["name"])
        try:
            copy = copywriter.write(cfg, note=str(data.get("note") or "")[:400])
        except RuntimeError as e:
            return self.send_json({"error": str(e)[:300]}, 502)
        cfg.setdefault("copy_history", []).append(cfg.get("copy"))
        cfg["copy"] = copy
        save_cfg(cfg)
        rebuild_site(cfg)
        return self.send_json({"copy": copy})

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
        if not s.get("name"):
            return self.send_json({"error": "No film."}, 400)
        cfg = load_cfg(s["name"])
        try:
            lay = typographer.lay_out(cfg, note=str(data.get("note") or "")[:400])
        except RuntimeError as e:
            return self.send_json({"error": str(e)[:300]}, 502)
        cfg.setdefault("copy_history", []).append(json.loads(json.dumps(cfg.get("copy"))))  # a snapshot, not a reference
        cfg["copy"]["layout"] = lay
        save_cfg(cfg)
        rebuild_site(cfg)
        return self.send_json({"copy": cfg["copy"]})

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
    print(f"Frameline studio at http://{host}:{port} (payments to {PAY_TO}, {len(OWNER_WALLETS)} owner wallet(s))")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
