#!/usr/bin/env python3
"""Pay an x402 (v2, exact/EIP-3009, USDC on Base) endpoint with the studio agent's
1Claw-held key, via the Intents API typed-data signer instead of `1claw pay`.

Usage:
  python3 scripts/x402pay.py URL 'JSON_BODY'          # quote only, pays nothing
  python3 scripts/x402pay.py URL 'JSON_BODY' --yes    # pay and fetch

Guardrails enforced here (1claw pay would enforce them server-side):
  payTo must be in PAYTO_ALLOWLIST, amount <= MAX_USD, Base USDC only.
1Claw itself only lets this agent sign EIP-712 for the Base USDC contract.
Importable: quote(url, body) and pay(url, body) are used by scrollsite.py.
"""
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

API = "https://api.1claw.co"
AGENT_ID = "ca018685-ec2c-4e96-8a48-ffad19b33922"  # studio
AGENT_ADDRESS = "0x9e55d4a950d002564bb18e34807571794fe0343e"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYTO_ALLOWLIST = {
    "0x7c655d3d3944bd27994dd81a036c3afb19806c40": "Vaaya",
    "0x3c5cbe28eca3b96023c45d3f877da834f1c7d5fa": "Locus",
    "0x2670b922ef37c7df47158725c0cc407b5382293f": "Venice (top-up)",
}
MAX_USD = 2.00
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "payments.jsonl")
OUT_DIR = os.path.join(ROOT, "outputs")


class Refused(Exception):
    """A payment our own guardrails will not make."""


def agent_token():
    """A short-lived JWT for the studio agent; the signing endpoint only accepts agent tokens.
    Sources, in order: ONECLAW_AGENT_ID + ONECLAW_AGENT_API_KEY in the environment (on a 1Claw
    runtime, from a vault-backed variable), the local key file from studio-rotate-key.py, and
    finally the token a 1Claw runtime injects (ONECLAW_AGENT_TOKEN)."""
    env = {k: os.environ[k] for k in ("ONECLAW_AGENT_ID", "ONECLAW_AGENT_API_KEY") if os.environ.get(k)}
    path = os.path.expanduser("~/.config/1claw/agents/studio.env")
    if len(env) < 2 and os.path.exists(path):
        for line in open(path):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                env[k] = v
    if not env.get("ONECLAW_AGENT_API_KEY"):
        if os.environ.get("ONECLAW_AGENT_TOKEN"):
            return os.environ["ONECLAW_AGENT_TOKEN"]
        raise SystemExit("No agent key: run `python3 scripts/studio-rotate-key.py` first.")
    s, _, raw = post(
        f"{API}/v1/auth/agent-token",
        {"agent_id": env.get("ONECLAW_AGENT_ID", AGENT_ID), "api_key": env["ONECLAW_AGENT_API_KEY"]},
        timeout=30,
    )
    data = json.loads(raw or b"{}")
    tok = data.get("access_token") or data.get("token")
    if s >= 300 or not tok:
        raise SystemExit(f"agent-token exchange failed ({s}): {raw[:300]!r}")
    return tok


def post(url, body, headers=None, timeout=600):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def header(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v


def quote(url, body):
    """Fetch the 402 challenge (free) and check it against our guardrails.
    Returns (usd, requirement, challenge, echo_headers)."""
    status, hdrs, raw = post(url, body, timeout=60)
    if status != 402:
        raise Refused(f"Expected 402 from {url}, got {status}: {raw[:300]!r}")
    challenge = json.loads(base64.b64decode(header(hdrs, "PAYMENT-REQUIRED") + "=="))
    options = [
        a for a in challenge["accepts"]
        if a.get("scheme") == "exact"
        and a.get("network") in ("eip155:8453", "base")
        and a.get("asset", "").lower() == BASE_USDC.lower()
    ]
    if not options:
        raise Refused(f"No Base USDC exact option in: {challenge['accepts']}")
    req = options[0]
    usd = int(req["amount"]) / 1e6
    if req["payTo"].lower() not in PAYTO_ALLOWLIST:
        raise Refused(f"payTo {req['payTo']} is not allowlisted")
    if usd > MAX_USD:
        raise Refused(f"${usd:.4f} exceeds MAX_USD ${MAX_USD:.2f}")
    echo = {}
    rid = header(hdrs, "x-locus-request-id")
    if rid:
        echo["x-locus-request-id"] = rid  # Locus asks for it back on the paid retry
    return usd, req, challenge, echo


def agent_sign(body):
    """Ask 1Claw to sign with the studio agent's key (the key never leaves 1Claw)."""
    s, _, raw = post(f"{API}/v1/agents/{AGENT_ID}/sign", body,
                     {"Authorization": f"Bearer {agent_token()}"}, timeout=60)
    signed = json.loads(raw or b"{}")
    if s >= 300 or "signature" not in signed:
        raise Refused(f"1Claw refused to sign ({s}): {raw[:500]!r}")
    if signed.get("from", "").lower() != AGENT_ADDRESS:
        raise Refused(f"Signer mismatch: {signed.get('from')}")
    return signed["signature"]


def sign_message(message):
    """EIP-191 personal_sign (used for Sign-In-With-X logins; cannot move funds)."""
    # 1Claw expects the message hex-encoded (UTF-8 bytes); it applies the EIP-191 prefix.
    return agent_sign({"intent_type": "personal_sign", "chain": "base",
                       "message": "0x" + message.encode().hex()})


def transfer_authorization(req, amount_atomic=None):
    """EIP-3009 TransferWithAuthorization for Base USDC, signed by the agent via 1Claw.
    Checks payTo against the allowlist first. Returns (authorization, signature)."""
    if req["payTo"].lower() not in PAYTO_ALLOWLIST:
        raise Refused(f"payTo {req['payTo']} is not allowlisted")
    if req.get("asset", "").lower() != BASE_USDC.lower():
        raise Refused(f"asset {req.get('asset')} is not Base USDC")
    now = int(time.time())
    authorization = {
        "from": AGENT_ADDRESS,
        "to": req["payTo"],
        "value": str(amount_atomic if amount_atomic is not None else req["amount"]),
        "validAfter": str(now - 600),
        "validBefore": str(now + int(req.get("maxTimeoutSeconds", 300))),
        "nonce": "0x" + secrets.token_hex(32),
    }
    extra = req.get("extra") or {}
    typed_data = _transfer_typed_data(req["asset"], extra, authorization)
    return authorization, agent_sign({"intent_type": "typed_data", "chain": "base", "typed_data": typed_data})


def _transfer_typed_data(asset, extra, authorization):
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": extra.get("name", "USD Coin"),
            "version": extra.get("version", "2"),
            "chainId": 8453,
            "verifyingContract": asset,
        },
        "message": authorization,
    }


def pay(url, body, label=None):
    """Quote, sign with the agent's 1Claw key, retry with the payment. Returns a dict."""
    usd, req, challenge, echo = quote(url, body)
    now = int(time.time())
    authorization = {
        "from": AGENT_ADDRESS,
        "to": req["payTo"],
        "value": str(req["amount"]),
        "validAfter": str(now - 600),
        "validBefore": str(now + int(req.get("maxTimeoutSeconds", 300))),
        "nonce": "0x" + secrets.token_hex(32),
    }
    extra = req.get("extra") or {}
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": extra.get("name", "USD Coin"),
            "version": extra.get("version", "2"),
            "chainId": 8453,
            "verifyingContract": req["asset"],
        },
        "message": authorization,
    }
    s, _, sraw = post(
        f"{API}/v1/agents/{AGENT_ID}/sign",
        {"intent_type": "typed_data", "chain": "base", "typed_data": typed_data},
        {"Authorization": f"Bearer {agent_token()}"},
        timeout=60,
    )
    signed = json.loads(sraw or b"{}")
    if s >= 300 or "signature" not in signed:
        raise Refused(f"1Claw refused to sign ({s}): {sraw[:500]!r}")
    if signed.get("from", "").lower() != AGENT_ADDRESS:
        raise Refused(f"Signer mismatch: {signed.get('from')}")

    payment = {
        "x402Version": 2,
        "resource": challenge.get("resource"),
        "accepted": req,
        "payload": {"signature": signed["signature"], "authorization": authorization},
    }
    pay_header = base64.b64encode(json.dumps(payment).encode()).decode()
    t0 = time.time()
    status, hdrs, raw = post(url, body, {"PAYMENT-SIGNATURE": pay_header, "X-PAYMENT": pay_header, **echo})
    receipt = header(hdrs, "PAYMENT-RESPONSE") or header(hdrs, "X-PAYMENT-RESPONSE")
    receipt = json.loads(base64.b64decode(receipt + "==")) if receipt else None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{int(t0)}-{secrets.token_hex(3)}.json")
    open(out, "wb").write(raw)
    rec = {
        "ts": int(t0), "label": label, "url": url, "model": body.get("model"), "usd": usd,
        "pay_to": req["payTo"], "status": status, "seconds": round(time.time() - t0, 1),
        "tx": (receipt or {}).get("transaction"), "output": out,
    }
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return {**rec, "receipt": receipt, "data": data, "raw": raw}


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    url, body = sys.argv[1], json.loads(sys.argv[2])
    try:
        if "--yes" not in sys.argv:
            usd, req, _, _ = quote(url, body)
            print(f"Quote: ${usd:.4f} to {req['payTo']} ({PAYTO_ALLOWLIST[req['payTo'].lower()]})")
            print("Quote only. Re-run with --yes to pay.")
            return
        r = pay(url, body)
    except Refused as e:
        raise SystemExit(f"Refused: {e}")
    print(f"Paid ${r['usd']:.4f} -> {r['status']} in {r['seconds']}s; tx {r['tx']}")
    print(f"Response saved to {r['output']}")
    print(r["raw"][:800].decode(errors="replace"))


if __name__ == "__main__":
    main()
