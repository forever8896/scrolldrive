#!/usr/bin/env python3
"""Frameline pipeline: brief -> keyframes -> film -> scroll-driven cinematic site.

All generation runs on Venice, paid from the studio agent's Venice balance, which the
agent tops up over x402 through `1claw pay` (`python3 scripts/venice.py topup`).

Keyframes: each shot is either generated from text, or composed from the user's
references and/or the previous keyframe (so the world stays consistent between shots).
Film: MiniMax H3 Max moves the camera from each keyframe exactly onto the next.

Usage:
  python3 scripts/scrollsite.py configs/x.json                          # plan, spends nothing
  python3 scripts/scrollsite.py configs/x.json --yes --stage keyframes  # shoot stills
  python3 scripts/scrollsite.py configs/x.json --yes --stage keyframes --only B   # redo one
  python3 scripts/scrollsite.py configs/x.json --yes --stage film       # film + site
  python3 scripts/scrollsite.py configs/x.json --yes                    # everything
Resumable: finished shots are never paid for twice (unless named with --only).
"""
import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import venice  # noqa: E402
import x402pay  # noqa: E402

EDIT_MODEL = "nano-banana-pro-edit"
GEN_MODEL = "nano-banana-pro"
DEFAULT_VIDEO_MODEL = "minimax-h3-max-image-to-video"
IMAGE_PRICE_FALLBACK = 0.18
MAX_PARALLEL = 4  # stills generated at once

ROOT = x402pay.ROOT
TEMPLATE = os.path.join(ROOT, "templates", "scroll-site.html")
FONTS = {  # site typography the director may choose; one family per site
    "grotesk": ("Geist", "Geist:wght@300;400;600"),
    "display": ("Syne", "Syne:wght@400;600;700"),
    "editorial": ("Newsreader", "Newsreader:ital,opsz,wght@0,6..72,300;0,6..72,500;1,6..72,400"),
    "mono": ("JetBrains Mono", "JetBrains+Mono:wght@300;400;600"),
}


# --- helpers ---------------------------------------------------------------------

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def _ffmpeg():
    """The system ffmpeg when there is one; else the static build that the imageio-ffmpeg
    package ships (1Claw's Python runtime image has no ffmpeg and cannot apt-install)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise SystemExit("ffmpeg not found: install it, or `pip install imageio-ffmpeg`")


FFMPEG = _ffmpeg()


def ff(*args):
    return subprocess.run([FFMPEG, "-y", "-loglevel", "error", *args], check=True)


def probe_duration(path):
    """Duration in seconds, read from ffmpeg's own header dump (no ffprobe needed)."""
    err = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if not m:
        raise RuntimeError(f"could not read the duration of {path}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def ssim(a, b):
    """Structural match of two frames at 160x90 with a light blur (1.0 = identical).
    Low resolution ignores fine texture and codec noise; a clip's end frame scores ~0.9
    against its target keyframe and ~0.1 against the others."""
    f = "scale=160:90:force_original_aspect_ratio=increase,crop=160:90,gblur=sigma=1,format=gray"
    r = subprocess.run([FFMPEG, "-loglevel", "info", "-i", a, "-i", b, "-lavfi",
                        f"[0:v]{f}[x];[1:v]{f}[y];[x][y]ssim", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"All:([0-9.]+)", r)
    return float(m.group(1)) if m else None


def jpeg_data_url(path):
    """Images go to Venice as JPEG data URLs (small, no public hosting needed)."""
    jpg = path.rsplit(".", 1)[0] + ".send.jpg"
    if not os.path.exists(jpg) or os.path.getmtime(jpg) < os.path.getmtime(path):
        ff("-i", path, "-vf", "scale='min(1600,iw)':-2", "-q:v", "3", jpg)
    return "data:image/jpeg;base64," + base64.b64encode(open(jpg, "rb").read()).decode()


def image_price(model):
    for kind in ("inpaint", "image"):
        try:
            with urllib.request.urlopen(f"{venice.API}/api/v1/models?type={kind}", timeout=30) as r:
                for m in json.load(r)["data"]:
                    if m["id"] == model:
                        p = m["model_spec"]["pricing"]
                        v = (p.get("resolutions") or {}).get("1K", {}).get("usd") \
                            or (p.get("inpaint") or {}).get("usd") or (p.get("generation") or {}).get("usd")
                        if v:
                            return float(v)
        except Exception:
            pass
    return IMAGE_PRICE_FALLBACK


def references(cfg, run_dir):
    """Local copies of the user's reference images (URLs are downloaded once)."""
    refs = list(cfg.get("references") or ([cfg["product_image"]] if cfg.get("product_image") else []))
    out = []
    for i, src in enumerate(refs):
        if src.startswith("http"):
            local = os.path.join(run_dir, f"ref-{i + 1}.jpg")
            if not os.path.exists(local):
                urllib.request.urlretrieve(src, local + ".src")
                ff("-i", local + ".src", "-q:v", "2", local)
            out.append(local)
        else:
            out.append(src)
    return out


def keyframe_path(run_dir, kid):
    for f in os.listdir(run_dir):
        if f.startswith(f"kf-{kid}.") and ".send." not in f:
            return os.path.join(run_dir, f)
    return None


# --- run state (resumable, never pays twice) --------------------------------------

class State:
    """Run state on disk. Thread-safe: stills and moves are generated in parallel."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.d = json.load(open(path)) if os.path.exists(path) else {"spent_usd": 0.0, "steps": {}}

    def get(self, key):
        with self.lock:
            return self.d["steps"].get(key)

    def put(self, key, value):
        with self.lock:
            self.d["steps"][key] = value
            self.save()

    def spend(self, usd):
        with self.lock:
            self.d["spent_usd"] = round(self.d["spent_usd"] + usd, 6)
            self.save()

    def reserve(self, budget, usd, label):
        """Check the budget and count the spend in one step, so parallel work cannot overshoot."""
        with self.lock:
            check_budget(self, budget, usd, label)
            self.spend(usd)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            json.dump(self.d, open(tmp, "w"), indent=2)
            os.replace(tmp, self.path)


class Budget(Exception):
    pass


def check_budget(state, budget, usd, label):
    if state.d["spent_usd"] + usd > budget + 1e-9:
        raise Budget(f"{label}: ${usd:.2f} would exceed the run budget "
                     f"(${state.d['spent_usd']:.2f} of ${budget:.2f} spent)")


# --- stage 1: keyframes -------------------------------------------------------------

def keyframes(cfg, state, run_dir, budget, only=None):
    """Shoot the stills. Shot A first; the rest in parallel, each composed from the user's
    references and, when chained, from shot A, which anchors the world (place, light,
    palette) without the drift that chaining shot to shot accumulates. Experimental films
    keep shot-to-shot chaining, since their stills are meant to evolve one from the next."""
    refs = references(cfg, run_dir)
    price = image_price(EDIT_MODEL)
    kfs = cfg["keyframes"]
    sequential = cfg.get("type") == "experimental" or cfg.get("chain_mode") == "previous"
    frames = {}

    def shoot(kf, chained_from):
        kid = kf["id"]
        path = keyframe_path(run_dir, kid)
        if only and kid in only and path:
            os.remove(path)  # redo: this shot is re-shot on request
            path = None
        if not path:
            inputs = [refs[i - 1] for i in (kf.get("refs") or []) if 1 <= i <= len(refs)]
            if kf.get("chain") and chained_from:
                inputs.append(chained_from)
            mode = f"composed from {len(inputs)} image(s)" if inputs else "generated from text"
            log(f"Keyframe {kid}: nano-banana-pro on Venice, {mode} (${price:.2f})")
            state.reserve(budget, price, f"keyframe {kid}")
            try:
                if inputs:
                    img = venice.image_multi_edit(EDIT_MODEL, kf["prompt"], [jpeg_data_url(p) for p in inputs],
                                                  cfg.get("aspect_ratio", "16:9"))
                else:
                    img = venice.image_generate(GEN_MODEL, kf["prompt"], cfg.get("aspect_ratio", "16:9"))
            except Exception:
                state.spend(-price)  # nothing was generated, so nothing was spent
                raise
            path = os.path.join(run_dir, f"kf-{kid}.png")
            open(path, "wb").write(img)
        log(f"Keyframe {kid}: {path}")
        return path

    if sequential:
        prev = None
        for kf in kfs:
            prev = frames.setdefault(kf["id"], {"path": shoot(kf, prev)})["path"]
        return frames

    anchor = shoot(kfs[0], None)
    frames[kfs[0]["id"]] = {"path": anchor}
    for p in [anchor] + refs:
        jpeg_data_url(p)  # prepare shared inputs once, before threads read them
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = {kf["id"]: pool.submit(shoot, kf, anchor) for kf in kfs[1:]}
        for kid, fut in futures.items():
            frames[kid] = {"path": fut.result()}
    return {k["id"]: frames[k["id"]] for k in kfs}


# --- stage 2: film --------------------------------------------------------------------

def clips(cfg, state, run_dir, budget, frames):
    """Film every camera move at once: each needs only its two finished stills, so all are
    queued together and checked together; the film takes about as long as its slowest move."""
    ids = [k["id"] for k in cfg["keyframes"]]
    v = cfg.get("video", {})
    model = v.get("model", DEFAULT_VIDEO_MODEL)
    duration, resolution = f"{v.get('duration', 5)}s", v.get("resolution", "768P")
    price = venice.video_quote(model, duration, resolution)
    out, pending = [], {}
    for a, b in zip(ids, ids[1:]):
        # A clip belongs to the exact pair of keyframe files it was shot from; a redone
        # keyframe makes its neighbouring clips stale, so they are filmed again.
        stamp = f"{os.path.getmtime(frames[a]['path']):.0f}-{os.path.getmtime(frames[b]['path']):.0f}-{resolution}"
        key = f"venice-clip:{a}-{b}:{stamp}"
        path = os.path.join(run_dir, f"clip-{a}-{b}.mp4")
        out.append({"from": a, "to": b, "path": path})
        if state.get(key) and os.path.exists(path) and state.get(key).get("written") == stamp:
            log(f"Clip {a}->{b}: {path}")
            continue
        if not state.get(key):
            prompt = (cfg.get("transitions") or {}).get(f"{a}-{b}") or cfg.get("transition_prompt")
            log(f"Clip {a}->{b}: {model} {resolution} {duration}, first->last frame (${price:.2f})")
            check_budget(state, budget, price, f"clip {a}->{b}")
            qid = venice.video_queue(model, prompt, jpeg_data_url(frames[a]["path"]),
                                     jpeg_data_url(frames[b]["path"]), duration, resolution)
            state.put(key, {"queue_id": qid, "model": model})  # saved first: never re-pays
            state.spend(price)
        pending[(a, b)] = (key, path, stamp)
    deadline = time.time() + 15 * 60
    while pending:
        for (a, b), (key, path, stamp) in list(pending.items()):
            status, video = venice.video_retrieve(model, state.get(key)["queue_id"])
            if video:
                open(path, "wb").write(video)
                state.put(key, {**state.get(key), "written": stamp})
                log(f"Clip {a}->{b}: {path}")
                del pending[(a, b)]
        if not pending:
            break
        if time.time() > deadline:
            raise RuntimeError(f"{len(pending)} move(s) still rendering after 15 min; re-run to keep checking")
        log(f"  clip {', '.join(f'{a}->{b}' for a, b in pending)}: {status}; checking again in 10s (free)")
        time.sleep(10)
    return out


def join_check(run_dir, frames, clip_list):
    results = []
    fmt = lambda v: "n/a" if v is None else f"{v:.3f}"
    for c in clip_list:
        first = os.path.join(run_dir, f"check-{c['from']}-{c['to']}-first.png")
        last = os.path.join(run_dir, f"check-{c['from']}-{c['to']}-last.png")
        ff("-i", c["path"], "-frames:v", "1", first)
        # Last VIDEO frame: overwrite-per-frame over the final half second (audio may run longer).
        ff("-sseof", "-0.5", "-i", c["path"], "-an", "-update", "1", "-q:v", "2", last)
        r = {"clip": f"{c['from']}->{c['to']}", "start_ssim": ssim(first, frames[c["from"]]["path"]),
             "end_ssim": ssim(last, frames[c["to"]]["path"])}
        r["lands_on_end_frame"] = (r["end_ssim"] or 0) >= 0.8
        results.append(r)
        log(f"Join check {r['clip']}: start SSIM {fmt(r['start_ssim'])}, end SSIM {fmt(r['end_ssim'])}"
            f" -> {'lands on end frame' if r['lands_on_end_frame'] else 'DOES NOT land on end frame'}")
    return results


def encode(run_dir, site_dir, clip_list, frames, first_id):
    concat = os.path.join(run_dir, "concat.txt")
    with open(concat, "w") as f:
        for c in clip_list:
            f.write(f"file '{os.path.abspath(c['path'])}'\n")
    # Fill the frame (crop, never pad): bars baked into the film show up as black side bands.
    norm = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,fps=24,setsar=1"
    # Desktop scrub master: every frame a keyframe, so any scroll position seeks instantly.
    ff("-f", "concat", "-safe", "0", "-i", concat, "-vf", norm, "-an", "-c:v", "libx264", "-crf", "28",
       "-x264-params", "keyint=1:scenecut=0", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
       os.path.join(site_dir, "film.mp4"))
    # Mobile: normal encode, played as a muted loop (never scrubbed on touch).
    ff("-f", "concat", "-safe", "0", "-i", concat, "-vf", norm.replace("1280:720", "960:540"), "-an",
       "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
       os.path.join(site_dir, "film-mobile.mp4"))
    ff("-i", frames[first_id]["path"], "-vf", "scale=1280:-2", "-q:v", "3", os.path.join(site_dir, "poster.jpg"))
    return probe_duration(os.path.join(site_dir, "film.mp4"))


def hex_or(v, fallback):
    return v if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) else fallback


def acts_from_beats(beats):
    """Place beat i around the i-th keyframe: keyframes sit at even steps of the film."""
    n = len(beats)
    if not n:
        return []
    acts, half = [], 0.42 / max(1, n - 1) if n > 1 else 0.5
    for i, text in enumerate(beats):
        c = i / (n - 1) if n > 1 else 0.5
        f, t = max(0.0, c - half), min(1.01, c + half)
        if i == 0:
            f = 0.0
        if i == n - 1:
            t = 1.01
        acts.append({"from": round(f, 3), "to": round(t, 3), "text": text})
    return acts


PRESETS = ("cinema", "editorial", "gallery", "poster", "whisper")
POSITIONS = ("tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br")
SIZES, ENTERS = ("s", "m", "l", "xl"), ("rise", "fade", "blur", "wipe", "type")
TEXTLAYER_CSS = os.path.join(ROOT, "templates", "textlayer.css")
TEXTLAYER_JS = os.path.join(ROOT, "templates", "textlayer.js")


def normalize_layout(copy, n_beats):
    """How the words sit on the film: one style per site, and per line a position on a 3x3
    grid, a size and an entrance. Older sites only had a left/center/right alignment."""
    lay = copy.get("layout") if isinstance(copy.get("layout"), dict) else {}
    align = copy.get("align")
    side = {"right": "r", "left": "l"}.get(align, "c")
    default_pos = "b" + side
    beats = []
    for i in range(n_beats):
        b = (lay.get("beats") or [])[i] if i < len(lay.get("beats") or []) else {}
        b = b if isinstance(b, dict) else {}
        beats.append({"pos": b.get("pos") if b.get("pos") in POSITIONS else default_pos,
                      "size": b.get("size") if b.get("size") in SIZES else "m",
                      "enter": b.get("enter") if b.get("enter") in ENTERS else "rise"})
    hero = lay.get("hero") if lay.get("hero") in POSITIONS else ("br" if align == "right" else "bl")
    return {"preset": lay.get("preset") if lay.get("preset") in PRESETS else "cinema", "hero": hero, "beats": beats}


def build_site(cfg, site_dir, duration):
    page = open(TEMPLATE).read()
    copy = cfg.get("copy", {})
    theme = cfg.get("theme") or {}
    family, font_query = FONTS.get(theme.get("font"), FONTS["grotesk"])
    esc = lambda s: html.escape(str(s or ""))
    sections = "".join(
        f'<section class="block"><h2 data-edit="sections.{i}.heading">{esc(s.get("heading"))}</h2>'
        f'<p data-edit="sections.{i}.body">{esc(s.get("body"))}</p></section>'
        for i, s in enumerate((copy.get("sections") or [])[:4]) if isinstance(s, dict))
    cta = copy.get("cta") if isinstance(copy.get("cta"), dict) else {"label": copy.get("cta") or ""}
    acts = acts_from_beats(copy["beats"]) if copy.get("beats") else copy.get("acts", [])
    layout = normalize_layout(copy, len(acts))
    script_json = lambda o: json.dumps(o).replace("</", "<\\/")
    href = str(cta.get("href") or "#")
    if not re.match(r"^(https?://|mailto:|#)", href):
        href = "#"
    replacements = {
        "__TITLE__": esc(copy.get("title") or cfg["name"]),
        "__TAGLINE__": esc(copy.get("tagline")),
        "__CTA__": esc(cta.get("label") or "Get in touch"),
        "__CTA_HREF__": esc(href),
        "__SECTIONS__": sections,
        "__DURATION__": f"{duration:.3f}",
        "__ACTS__": script_json(acts),
        "__LAYOUT__": script_json(layout),
        "__HERO__": layout["hero"],
        "__TEXTLAYER_CSS__": open(TEXTLAYER_CSS).read(),
        "__TEXTLAYER_JS__": open(TEXTLAYER_JS).read(),
        "__FONT_FAMILY__": family,
        "__FONT_QUERY__": font_query,
        "__BG__": hex_or(theme.get("bg"), "#0d0c0b"),
        "__INK__": hex_or(theme.get("ink"), "#f3eee6"),
        "__MUTED__": hex_or(theme.get("muted"), "#a79f93"),
        "__ACCENT__": hex_or(theme.get("accent"), "#e07a3f"),
    }
    for k, v in replacements.items():
        page = page.replace(k, v)
    open(os.path.join(site_dir, "index.html"), "w").write(page)
    open(os.path.join(site_dir, "DEPLOY.md"), "w").write(
        "# Deploy\n\nThis folder is a static site. Upload it as-is to any static host:\n\n"
        "- **Vercel:** `npx vercel deploy --prod` in this folder\n"
        "- **Netlify:** drag the folder onto app.netlify.com/drop\n"
        "- **Cloudflare Pages:** `npx wrangler pages deploy .`\n"
        "- **Any web server:** serve the folder; make sure `.mp4` is sent as `video/mp4` "
        "and HTTP range requests are enabled (needed for scrubbing).\n")


def zip_site(site_dir, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(site_dir)):
            z.write(os.path.join(site_dir, name), name)


# --- plan -------------------------------------------------------------------------------

def plan(cfg, run_dir=None, quiet=False):
    """Price the run from Venice's public price list and free video quote."""
    n_kf, n_clips = len(cfg["keyframes"]), len(cfg["keyframes"]) - 1
    have = sum(1 for k in cfg["keyframes"] if run_dir and os.path.isdir(run_dir) and keyframe_path(run_dir, k["id"]))
    v = cfg.get("video", {})
    img_usd = image_price(EDIT_MODEL)
    clip_usd = venice.video_quote(v.get("model", DEFAULT_VIDEO_MODEL), f"{v.get('duration', 5)}s", v.get("resolution", "768P"))
    kf_total, film_total = (n_kf - have) * img_usd, n_clips * clip_usd
    if not quiet:
        print(f"Plan for '{cfg['name']}' (Venice, paid from the agent's Venice balance):")
        print(f"  keyframes: {n_kf - have} to shoot x ${img_usd:.2f} = ${kf_total:.2f}" + (f"  [{have} done]" if have else ""))
        print(f"  film:      {n_clips} camera moves x ${clip_usd:.2f} = ${film_total:.2f}  ({v.get('resolution', '768P')})")
        print(f"  total ${kf_total + film_total:.2f}")
    return {"keyframes_usd": round(kf_total, 2), "film_usd": round(film_total, 2),
            "image_usd": img_usd, "clip_usd": clip_usd, "total_usd": round(kf_total + film_total, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--stage", choices=["keyframes", "film", "all"], default="all")
    ap.add_argument("--only", default="", help="comma-separated keyframe ids to (re)shoot")
    ap.add_argument("--refilm", default="", help="a camera move to film again, e.g. B-C")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    run_dir = os.path.join(ROOT, "outputs", "runs", cfg["name"])
    os.makedirs(run_dir, exist_ok=True)
    budget = float(cfg.get("budget_usd", 3.0))
    plan(cfg, run_dir)
    if not a.yes:
        print("Nothing spent. Re-run with --yes to generate.")
        return
    state = State(os.path.join(run_dir, "venice-state.json"))
    only = {x.strip() for x in a.only.split(",") if x.strip()} or None
    t0 = time.time()
    try:
        frames = keyframes(cfg, state, run_dir, budget, only=only if a.stage != "film" else None)
        if a.stage == "keyframes":
            print(f"Keyframes ready. Venice spend so far: ${state.d['spent_usd']:.2f}")
            return
        if a.refilm:
            # Forget this move's finished clip, so it is queued and paid for again.
            pair = a.refilm.strip().upper()
            with state.lock:
                for key in [k for k in state.d["steps"] if k.startswith(f"venice-clip:{pair}:")]:
                    del state.d["steps"][key]
                state.save()
            old = os.path.join(run_dir, f"clip-{pair}.mp4")
            if os.path.exists(old):
                os.remove(old)
            log(f"Refilming move {pair}")
        clip_list = clips(cfg, state, run_dir, budget, frames)
    except (Budget, venice.VeniceError, x402pay.Refused, RuntimeError) as e:
        raise SystemExit(f"Stopped: {e}\nRe-running resumes without paying for finished steps.")
    site_dir = os.path.join(run_dir, "site")
    os.makedirs(site_dir, exist_ok=True)
    joins = join_check(run_dir, frames, clip_list)
    duration = encode(run_dir, site_dir, clip_list, frames, cfg["keyframes"][0]["id"])
    build_site(cfg, site_dir, duration)
    zip_path = os.path.join(run_dir, f"{cfg['name']}-site.zip")
    zip_site(site_dir, zip_path)
    json.dump({"name": cfg["name"], "venice_spend_usd": state.d["spent_usd"], "minutes": round((time.time() - t0) / 60, 1),
               "film_seconds": round(duration, 2), "joins": joins, "site": site_dir, "zip": zip_path},
              open(os.path.join(run_dir, "report.json"), "w"), indent=2)
    print()
    print(f"Done. Venice spend this run: ${state.d['spent_usd']:.2f}")
    print(f"Site:  {site_dir}/index.html")
    print(f"Zip:   {zip_path}")


if __name__ == "__main__":
    main()
