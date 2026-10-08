"""Durable storage for the studio, so a restart or a redeploy loses nothing.

The container's disk is temporary. Two kinds of data outlive it:
- State (sessions and film configs, small JSON) goes to 1Claw Agent Memory, compressed and split under its
  64 KB value limit. It needs nothing beyond the studio agent's own key.
- Media (stills, camera moves, the site, the zip, uploaded images) goes to an S3-compatible bucket (Cloudflare
  R2, S3, ...) when one is configured: S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, and
  S3_REGION (default "auto"). Without one, media stays on the disk as before.

A background syncer copies whatever changed every few seconds; on start, state is restored at once and media
is fetched in the background (and on demand, when a file is asked for before it has arrived).
Runs on the 1Claw runtime (Cloud Run sets K_SERVICE); locally only with STUDIO_SYNC=1, into its own namespace.
"""
import base64
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x402pay  # noqa: E402

LIVE = bool(os.environ.get("K_SERVICE"))
ENABLED = LIVE or os.environ.get("STUDIO_SYNC") == "1"
NAMESPACE = os.environ.get("STUDIO_STATE_NAMESPACE") or ("frameline-state" if LIVE else "frameline-state-local")
PART = 60000  # characters per memory value, under the 64 KB limit
MEDIA_SKIP = (".log", ".tmp", "concat.txt")  # rebuilt or only useful while rendering
MEDIA_SKIP_PREFIX = ("check-",)


def log(msg):
    sys.stderr.write(f"storage: {msg}\n")


# --- state: 1Claw Agent Memory ---------------------------------------------------------------------

def _mem(method, key="", body=None, query=""):
    url = f"{x402pay.API}/v1/agents/{x402pay.AGENT_ID}/memory/{NAMESPACE}" + (f"/{urllib.parse.quote(key)}" if key else "") + query
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                 headers={**x402pay.auth_headers(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def put_state(key, text):
    """Store a JSON document under key: zlib + base64, split into parts when it is large."""
    blob = base64.b64encode(zlib.compress(text.encode(), 9)).decode()
    parts = [blob[i:i + PART] for i in range(0, len(blob), PART)] or [""]
    for i, p in enumerate(parts[1:], 1):
        _mem("PUT", f"{key}~{i}", {"value": p, "tier": "durable"})
    _mem("PUT", key, {"value": json.dumps({"n": len(parts), "p": parts[0]}), "tier": "durable"})  # head last


def all_state():
    """Every stored document, {key: text}."""
    rows, offset = {}, 0
    while True:
        page = _mem("GET", query=f"?limit=500&offset={offset}").get("entries") or []
        for e in page:
            rows[e["key"]] = e["value"]
        if len(page) < 500:
            break
        offset += len(page)
    out = {}
    for key, value in rows.items():
        if "~" in key:
            continue
        try:
            head = json.loads(value)
            blob = head["p"] + "".join(rows[f"{key}~{i}"] for i in range(1, head["n"]))
            out[key] = zlib.decompress(base64.b64decode(blob)).decode()
        except (ValueError, KeyError, TypeError, zlib.error):
            log(f"could not read {key}")
    return out


# --- media: an S3-compatible bucket (SigV4) --------------------------------------------------------

S3 = {k: os.environ.get(f"S3_{k.upper()}", "") for k in ("endpoint", "bucket", "access_key_id", "secret_access_key")}
S3["region"] = os.environ.get("S3_REGION", "auto")
MEDIA = all(S3[k] for k in ("endpoint", "bucket", "access_key_id", "secret_access_key"))


def _s3(method, key="", body=b"", query=None, headers=None):
    host = urllib.parse.urlparse(S3["endpoint"]).netloc
    path = f"/{S3['bucket']}/" + urllib.parse.quote(key, safe="/-_.~")
    q = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
                 for k, v in sorted((query or {}).items()))
    now = datetime.datetime.now(datetime.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    h = {"host": host, "x-amz-date": amz, "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
         **{k.lower(): v for k, v in (headers or {}).items()}}
    signed = ";".join(sorted(h))
    canonical = "\n".join([method, path, q, "".join(f"{k}:{h[k].strip()}\n" for k in sorted(h)), signed,
                           h["x-amz-content-sha256"]])
    scope = f"{day}/{S3['region']}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + S3["secret_access_key"]).encode()
    for part in (day, S3["region"], "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = f"AWS4-HMAC-SHA256 Credential={S3['access_key_id']}/{scope}, SignedHeaders={signed}, Signature={sig}"
    req = urllib.request.Request(S3["endpoint"].rstrip("/") + path + (f"?{q}" if q else ""), data=body or None,
                                 method=method, headers={k: v for k, v in h.items() if k != "host"})
    return urllib.request.urlopen(req, timeout=300)


def put_media(key, path):
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"  # never urllib's form default
    with open(path, "rb") as f:
        _s3("PUT", key, f.read(), headers={"content-type": ctype, "x-amz-meta-mtime": str(os.path.getmtime(path))}).close()


def get_media(key, path):
    """Download one object to path, keeping its original modified time (the board compares them)."""
    with _s3("GET", key) as r:
        data, mtime = r.read(), r.headers.get("x-amz-meta-mtime")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    if mtime:
        os.utime(path, (float(mtime), float(mtime)))


def list_media(prefix):
    keys, token = [], None
    while True:
        q = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
        with _s3("GET", query=q) as r:
            root = ET.fromstring(r.read())
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        keys += [c.findtext(f"{ns}Key") for c in root.findall(f"{ns}Contents")]
        token = root.findtext(f"{ns}NextContinuationToken")
        if not token:
            return keys


# --- the syncer ------------------------------------------------------------------------------------

class Syncer:
    """Copies changed state and media to durable storage, and brings them back after a restart."""

    def __init__(self, root, sessions_dir, configs_dir, media_dirs):
        self.root, self.sessions_dir, self.configs_dir = root, sessions_dir, configs_dir
        self.media_dirs = media_dirs  # {"runs": path, "uploads": path}
        self.seen = {}
        self.fetching = set()
        self.lock = threading.Lock()

    def state_files(self):
        for d, prefix in ((self.sessions_dir, "s."), (self.configs_dir, "c.")):
            for f in os.listdir(d) if os.path.isdir(d) else []:
                if f.endswith(".json"):
                    yield prefix + f[:-5], os.path.join(d, f)

    def media_files(self):
        for name, d in self.media_dirs.items():
            for dirpath, _, files in os.walk(d):
                for f in files:
                    if f.endswith(MEDIA_SKIP) or f.startswith(MEDIA_SKIP_PREFIX) or ".send." in f:
                        continue
                    path = os.path.join(dirpath, f)
                    yield f"{name}/{os.path.relpath(path, d).replace(os.sep, '/')}", path

    def changed(self, key, path):
        try:
            m = os.path.getmtime(path)
        except OSError:
            return False
        if self.seen.get(key) == m or time.time() - m < 2:  # unchanged, or still being written
            return False
        self.seen[key] = m
        return True

    def tick(self):
        for key, path in list(self.state_files()):
            if self.changed(key, path):
                try:
                    put_state(key, open(path).read())
                except Exception as e:  # noqa: BLE001  retried on the next tick
                    self.seen.pop(key, None)
                    log(f"state {key}: {e}")
        if MEDIA:
            for key, path in list(self.media_files()):
                if self.changed(key, path):
                    try:
                        put_media(key, path)
                    except Exception as e:  # noqa: BLE001
                        self.seen.pop(key, None)
                        log(f"media {key}: {e}")

    def run(self, every=5):
        while True:
            time.sleep(every)
            with self.lock:
                self.tick()

    def restore_state(self, fix_session):
        """Bring back sessions and configs the disk does not have. fix_session(dict) adapts paths."""
        try:
            docs = all_state()
        except Exception as e:  # noqa: BLE001  the studio still starts; state comes back on the next start
            log(f"could not restore state: {e}")
            return 0
        n = 0
        for key, text in docs.items():
            kind, name = key.split(".", 1)
            d = self.sessions_dir if kind == "s" else self.configs_dir
            path = os.path.join(d, f"{name}.json")
            if os.path.exists(path):
                continue
            if kind == "s":
                text = json.dumps(fix_session(json.loads(text)), indent=1)
            os.makedirs(d, exist_ok=True)
            open(path, "w").write(text)
            self.seen[key] = os.path.getmtime(path)
            n += 1
        return n

    def restore_media(self):
        if not MEDIA:
            return
        for name, d in self.media_dirs.items():
            try:
                keys = list_media(f"{name}/")
            except Exception as e:  # noqa: BLE001
                log(f"could not list media: {e}")
                return
            for key in keys:
                self.fetch(key, os.path.join(d, key[len(name) + 1:]))

    def fetch(self, key, path):
        """Bring one media file back if the disk does not have it. Safe to call from a request."""
        if os.path.exists(path) or not MEDIA:
            return os.path.exists(path)
        with self.lock:
            if os.path.exists(path):
                return True
            try:
                get_media(key, path)
                self.seen[key] = os.path.getmtime(path)
                return True
            except Exception as e:  # noqa: BLE001
                if not (isinstance(e, urllib.error.HTTPError) and e.code == 404):
                    log(f"fetch {key}: {e}")
                return False

    def media_key(self, path):
        """The bucket key for a local media path, or None."""
        for name, d in self.media_dirs.items():
            if os.path.abspath(path).startswith(os.path.abspath(d) + os.sep):
                return f"{name}/{os.path.relpath(path, d).replace(os.sep, '/')}"
        return None
