#!/usr/bin/env python3
"""Buy a Frameline film the way another agent would: pay the x402 endpoint with 1Claw pay.

  python3 scripts/buy_film.py <frameline-url>                 # quote -> 1Claw signs -> facilitator verifies (no money moves)
  python3 scripts/buy_film.py <frameline-url> --settle        # the real purchase: USDC moves, the film is made

The buyer is a 1Claw agent that holds no raw key: 1Claw signs an EIP-3009 USDC authorization for it, the
PayAI facilitator verifies and (on --settle) submits it on Base. The buyer wallet needs USDC but no ETH
(the transfer is gasless for the signer). Set the buyer with ONECLAW_AGENT_ID / ONECLAW_AGENT_API_KEY
(or point BUYER_ENV at a file holding them); otherwise it falls back to the studio agent in studio.env.

Without --settle nothing is paid: it stops before the irreversible on-chain step, so it is safe to run.
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
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base USDC
FACILITATOR = os.environ.get("X402_FACILITATOR", "https://facilitator.payai.network")
ORDER = {"brief": "A launch film for a hand-forged chef's knife from a small Oslo smithy. Dark, firelit, deliberate.",
         "scenes": 2, "resolution": "768P", "style": "noir"}  # scenes=2 is the cheapest, to keep a real settle small


def load_env(path):
    out = {}
    for line in open(path) if os.path.exists(path) else []:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            out[k] = v
    return out


def buyer_creds():
    env = {k: os.environ.get(k, "") for k in ("ONECLAW_AGENT_ID", "ONECLAW_AGENT_API_KEY")}
    if not env["ONECLAW_AGENT_API_KEY"]:
        path = os.environ.get("BUYER_ENV") or os.path.expanduser("~/.config/1claw/agents/studio.env")
        env = {**load_env(path), **{k: v for k, v in env.items() if v}}
    if not env.get("ONECLAW_AGENT_API_KEY"):
        sys.exit("No buyer agent: set ONECLAW_AGENT_ID and ONECLAW_AGENT_API_KEY, or BUYER_ENV.")
    return env


def req(method, url, body=None, headers=None, raw=False):
    r = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                               headers={"Content-Type": "application/json", "User-Agent": "frameline-buyer/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            data = resp.read()
            return resp.status, dict(resp.headers), (data if raw else (json.loads(data) if data else {}))
    except urllib.error.HTTPError as e:
        data = e.read()
        return e.code, dict(e.headers), (data if raw else (json.loads(data) if data else {}))


def step(n, title):
    print(f"\n\033[1m{n}. {title}\033[0m")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    settle = "--settle" in sys.argv
    base = (args[0] if args else "http://127.0.0.1:8080").rstrip("/")
    url = f"{base}/api/agent/films"
    env = buyer_creds()

    # The buyer signs in to 1Claw and learns its own wallet address (its key never leaves 1Claw).
    s, _, tok = req("POST", f"{API}/v1/auth/agent-token", {"agent_id": env["ONECLAW_AGENT_ID"], "api_key": env["ONECLAW_AGENT_API_KEY"]})
    if s >= 300:
        sys.exit(f"Buyer sign-in failed ({s}): {tok}")
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    s, _, keys = req("GET", f"{API}/v1/agents/{env['ONECLAW_AGENT_ID']}/signing-keys", headers=auth)
    evm = [k for k in (keys.get("keys") or []) if k.get("is_active") and k.get("chain") in ("ethereum", "base")]
    buyer_wallet = (evm[0]["address"] if evm else "").lower()
    if not buyer_wallet:
        sys.exit("The buyer agent has no EVM signing key. Provision one: POST /v1/agents/<id>/signing-keys {\"chain\":\"ethereum\"}")

    step(1, "Ask Frameline for a film (no payment yet)")
    s, _, challenge = req("POST", url, ORDER)
    if s != 402:
        sys.exit(f"Expected a 402 payment challenge, got {s}: {str(challenge)[:300]}")
    accept = challenge["accepts"][0]
    price = int(accept["amount"]) / 1e6
    pay_to = accept["payTo"]
    print(f"   Frameline replies 402 Payment Required.")
    print(f"   It wants {price:.2f} USDC on Base (eip155:8453) to {pay_to}")
    print(f"   for: {challenge['resource']['description']}")

    step(2, "1Claw pay signs the payment for the buyer")
    now = int(time.time())
    authorization = {"from": buyer_wallet, "to": pay_to, "value": accept["amount"],
                     "validAfter": str(now - 600), "validBefore": str(now + int(accept.get("maxTimeoutSeconds", 600))),
                     "nonce": "0x" + secrets.token_hex(32)}
    typed_data = {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                  "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                                                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                                                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": accept.get("extra", {}).get("name", "USD Coin"), "version": accept.get("extra", {}).get("version", "2"),
                   "chainId": 8453, "verifyingContract": accept["asset"]},
        "message": authorization}
    s, _, signed = req("POST", f"{API}/v1/agents/{env['ONECLAW_AGENT_ID']}/sign",
                       {"intent_type": "typed_data", "chain": "base", "typed_data": typed_data}, auth)
    if s >= 300 or "signature" not in signed:
        sys.exit(f"1Claw refused to sign ({s}): {str(signed)[:300]}")
    print(f"   Buyer wallet (1Claw-held key): {signed.get('from')}")
    print(f"   1Claw signed an EIP-3009 USDC authorization. No funds have moved: this is just the signed permission.")
    payment = {"x402Version": 2, "resource": challenge["resource"]["url"], "accepted": accept,
               "payload": {"signature": signed["signature"], "authorization": authorization}}

    step(3, "The facilitator checks the payment is valid (still no money)")
    want = {k: accept[k] for k in accept}
    s, _, v = req("POST", f"{FACILITATOR}/verify", {"x402Version": 2, "paymentPayload": payment, "paymentRequirements": want})
    if v.get("isValid"):
        print(f"   \033[32mValid.\033[0m The facilitator confirms this is a spendable payment from {v.get('payer')}.")
    else:
        print(f"   The facilitator did not confirm it: {v.get('invalidReason') or v}")
        print("   (A self-pay, an unfunded buyer wallet, or insufficient USDC all show up here.)")

    if not settle:
        print("\n\033[1mStopping here.\033[0m Nothing was paid. The payment above is valid and ready.")
        print("To make the film for real (this moves USDC on Base), run again with --settle.")
        return

    step(4, "Pay and commission the film (USDC moves on Base)")
    header = base64.b64encode(json.dumps(payment).encode()).decode()
    s, h, order = req("POST", url, ORDER, {"PAYMENT-SIGNATURE": header, "X-PAYMENT": header})
    if s >= 300:
        sys.exit(f"Payment/order failed ({s}): {str(order)[:400]}")
    receipt = h.get("PAYMENT-RESPONSE")
    tx = json.loads(base64.b64decode(receipt + "==")).get("transaction") if receipt else order.get("tx")
    print(f"   \033[32mPaid.\033[0m Settlement tx: https://basescan.org/tx/{tx}")
    print(f"   Frameline accepted the order and the director is making the film.")
    print(f"   Watch it: {order.get('page')}")
    print(f"   Status:   {base}/api/agent/films/{order.get('id')}")


if __name__ == "__main__":
    main()
