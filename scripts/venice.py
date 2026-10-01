#!/usr/bin/env python3
"""Venice client for the studio agent, with no Venice account or API key.

Identity: every request carries X-Sign-In-With-X, a Sign-In-With-Ethereum message signed
by the agent's 1Claw-held key (EIP-191 personal_sign; a login, it cannot move funds).
Money: the agent tops up its Venice balance over x402 (USDC on Base) through `1claw pay`,
so 1Claw enforces the payee allowlist and spend caps; generations then spend from that balance.

CLI:
  python3 scripts/venice.py balance
  python3 scripts/venice.py topup            # pays Venice's minimum top-up ($5) via 1claw pay
"""
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import x402pay

API = "https://api.venice.ai"
DOMAIN = "api.venice.ai"
ADDRESS = "0x9e55D4a950D002564Bb18e34807571794fe0343e"  # studio agent, EIP-55 checksummed
MAX_TOPUP_USD = 10.0


class VeniceError(Exception):
    pass


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def siwx(url):
    """X-Sign-In-With-X header for one request (fresh nonce, 5-minute expiry)."""
    now = datetime.now(timezone.utc)
    message = (
        f"{DOMAIN} wants you to sign in with your Ethereum account:\n{ADDRESS}\n\n"
        f"Sign in to Venice AI\n\n"
        f"URI: {url}\nVersion: 1\nChain ID: 8453\nNonce: {secrets.token_hex(8)}\n"
        f"Issued At: {_iso(now)}\nExpiration Time: {_iso(now + timedelta(minutes=5))}"
    )
    signature = x402pay.sign_message(message)
    return base64.b64encode(json.dumps({
        "address": ADDRESS, "message": message, "signature": signature,
        "timestamp": int(now.timestamp() * 1000), "chainId": 8453,
    }).encode()).decode()


def request(path, body=None, method="POST", auth=True, headers=None, timeout=300):
    url = f"{API}/api/v1{path}"
    h = {"Content-Type": "application/json"}
    if auth:
        h["X-Sign-In-With-X"] = siwx(url)
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def balance():
    s, h, raw = request(f"/x402/balance/{ADDRESS}", method="GET")
    if s >= 300:
        raise VeniceError(f"balance {s}: {raw[:300]!r}")
    return _json(raw)


def _oneclaw(path, body):
    s, _, raw = x402pay.post(f"{x402pay.API}/v1/agents/{x402pay.AGENT_ID}{path}", body,
                             x402pay.auth_headers(), timeout=60)
    d = _json(raw) or {}
    if s >= 300:
        raise VeniceError(f"1claw pay{path} -> {s}: {raw[:400]!r}")
    return d


def topup():
    """Top up the Venice balance through `1claw pay` (unattended mode). 1Claw enforces the
    payee allowlist and the per-payment and daily caps server-side. Venice's challenge asks
    for its minimum top-up ($5), and that exact amount is what gets signed."""
    url = f"{API}/api/v1/x402/top-up"
    s, h, raw = request("/x402/top-up", body={}, auth=False)
    if s != 402:
        raise VeniceError(f"expected 402 from top-up, got {s}: {raw[:300]!r}")
    challenge_b64 = (x402pay.header(h, "PAYMENT-REQUIRED") or "").strip() or base64.b64encode(raw).decode()
    prep = _oneclaw("/pay/prepare", {"challenge_b64": challenge_b64, "method": "POST",
                                    "resource_url": url, "mode": "auto"})
    if prep.get("authorization") != "allow":
        raise VeniceError(f"1Claw did not allow this payment: {prep.get('authorization')}")
    signed = _oneclaw("/pay/sign", {"session_id": prep["session_id"], "mode": "auto"})
    header = signed["payment_header"]
    # One signature, several envelopes. 1Claw charges its daily limit at signing, so we try
    # every envelope Venice might accept with the SAME signed authorization. A failed
    # verification never settles, so at most one of these can ever move money.
    try:
        env = json.loads(base64.b64decode(header + "=="))
    except ValueError:
        env = {}
    payload = env.get("payload") or {}
    req = json.loads(base64.b64decode(challenge_b64 + "=="))["accepts"][0] if challenge_b64 else {}
    enc = lambda o: base64.b64encode(json.dumps(o).encode()).decode()
    variants = [
        ("X-402-Payment", enc({"x402Version": 2, "scheme": "exact", "network": "base", "payload": payload})),
        ("X-402-Payment", header),
        ("X-PAYMENT", header),
        ("PAYMENT-SIGNATURE", enc({"x402Version": 2, "accepted": req, "payload": payload})),
        ("PAYMENT-SIGNATURE", header),
        ("X-PAYMENT", enc({"x402Version": 1, "scheme": "exact", "network": "base", "payload": payload})),
    ]
    debug = os.path.join(x402pay.ROOT, "outputs", f"venice-topup-{int(time.time())}.json")
    fd = os.open(debug, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps({"envelope_from_1claw": {k: v for k, v in env.items() if k != "payload"},
                             "payload_keys": list(payload), "challenge_requirement": req}, indent=2).encode())
    os.close(fd)
    attempts = []
    for name, value in variants:
        s, h, raw = request("/x402/top-up", auth=False, headers={name: value})
        attempts.append({"header": name, "status": s, "response": raw[:200].decode(errors="replace")})
        if s < 300:
            break
    ok = s < 300
    print(json.dumps(attempts, indent=1))
    try:
        _oneclaw(f"/pay/{signed['payment_id']}/result",
                 {"outcome": "succeeded" if ok else "failed", "http_status": s})
    except VeniceError:
        pass  # reporting is best-effort; the vault already charged the limit at signing
    with open(x402pay.LOG, "a") as f:
        f.write(json.dumps({"ts": int(time.time()), "label": "venice top-up via 1claw pay",
                            "usd": signed.get("amount_usd"), "pay_to": signed.get("pay_to"),
                            "status": s, "response": raw[:300].decode(errors="replace")}) + "\n")
    if not ok:
        raise VeniceError(f"top-up failed {s}: {raw[:400]!r}")
    return {"paid_usd": signed.get("amount_usd"), "venice": _json(raw)}


def image_edit(model, prompt, image, aspect_ratio="16:9"):
    """Edit an image (URL or data URL). Returns image bytes."""
    s, h, raw = request("/image/edit", {"model": model, "prompt": prompt, "image": image,
                                        "aspect_ratio": aspect_ratio, "output_format": "png"})
    if s >= 300:
        raise VeniceError(f"image edit {s}: {raw[:400]!r}")
    if raw[:4] == b"\x89PNG" or raw[:3] == b"\xff\xd8\xff" or raw[:4] == b"RIFF":
        return raw
    d = _json(raw) or {}
    b = (d.get("images") or [None])[0] or ((d.get("data") or [{}])[0] or {}).get("b64_json")
    if b:
        return base64.b64decode(b.split(",", 1)[-1])
    url = d.get("download_url") or ((d.get("data") or [{}])[0] or {}).get("url")
    if url:
        return urllib.request.urlopen(url, timeout=180).read()
    raise VeniceError(f"image edit: unexpected response {json.dumps(d)[:300]}")


def _image_bytes(s, raw, what):
    if s >= 300:
        raise VeniceError(f"{what} {s}: {raw[:400]!r}")
    if raw[:4] == b"\x89PNG" or raw[:3] == b"\xff\xd8\xff" or raw[:4] == b"RIFF":
        return raw
    d = _json(raw) or {}
    b = (d.get("images") or [None])[0]
    if b:
        return base64.b64decode(b.split(",", 1)[-1])
    raise VeniceError(f"{what}: unexpected response {json.dumps(d)[:300]}")


def image_generate(model, prompt, aspect_ratio="16:9", resolution="1K"):
    """Text to image. Returns image bytes."""
    s, h, raw = request("/image/generate", {"model": model, "prompt": prompt[:7500], "aspect_ratio": aspect_ratio,
                                            "resolution": resolution, "format": "png", "return_binary": True,
                                            "safe_mode": False})
    return _image_bytes(s, raw, "image generate")


def image_multi_edit(model, prompt, images, aspect_ratio="16:9"):
    """Edit/compose from up to 6 reference images (URLs or data URLs). Returns image bytes."""
    s, h, raw = request("/image/multi-edit", {"modelId": model, "prompt": prompt, "images": list(images)[:6],
                                              "aspect_ratio": aspect_ratio, "output_format": "png"})
    return _image_bytes(s, raw, "image multi-edit")


def video_quote(model, duration="5s", resolution="768P"):
    s, h, raw = request("/video/quote", {"model": model, "duration": duration, "resolution": resolution}, auth=False)
    d = _json(raw) or {}
    if s >= 300 or "quote" not in d:
        raise VeniceError(f"video quote {s}: {raw[:300]!r}")
    return float(d["quote"])


def video_queue(model, prompt, image_url, end_image_url=None, duration="5s", resolution="768P"):
    body = {"model": model, "prompt": prompt, "image_url": image_url, "duration": duration,
            "resolution": resolution}  # H3 Max: audio is not configurable (the rendered track is dropped at encode)
    if end_image_url:
        body["end_image_url"] = end_image_url
    s, h, raw = request("/video/queue", body)
    d = _json(raw) or {}
    if s >= 300 or not (d.get("queue_id") or d.get("id")):
        raise VeniceError(f"video queue {s}: {raw[:400]!r}")
    return d.get("queue_id") or d.get("id")


def video_retrieve(model, queue_id):
    """Returns (status, bytes_or_None). Polling is free on Venice."""
    s, h, raw = request("/video/retrieve", {"queue_id": queue_id, "model": model})
    if s in (404, 425):
        return "processing", None
    if raw[4:8] == b"ftyp" or (x402pay.header(h, "Content-Type") or "").startswith("video"):
        return "done", raw
    d = _json(raw) or {}
    url = d.get("download_url") or (d.get("data") or {}).get("download_url") if isinstance(d, dict) else None
    if url:
        return "done", urllib.request.urlopen(url, timeout=300).read()
    if s >= 400:
        raise VeniceError(f"video retrieve {s}: {raw[:400]!r}")
    return d.get("status", "processing"), None


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "balance"
    try:
        if cmd == "balance":
            print(json.dumps(balance(), indent=2))
        elif cmd == "topup":
            print(json.dumps(topup(), indent=2))
        else:
            raise SystemExit(__doc__)
    except (VeniceError, x402pay.Refused) as e:
        raise SystemExit(f"Venice: {e}")


if __name__ == "__main__":
    main()
