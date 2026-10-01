#!/usr/bin/env python3
"""Enable unattended `1claw pay` for the `studio` agent, Venice-only.

Run in your own terminal: python3 scripts/studio-pay-settings.py
Asks for one fresh authenticator (TOTP) code, obtains a single-use re-auth token for
purpose `security.agent_pay_guardrails.widen` (per 1Claw), and applies the settings below.
1Claw then enforces them server-side on every payment: only the allowlisted payee,
at most PAY_MAX_USD per payment and PAY_DAILY_USD per day.
"""
import getpass
import json
import os
import urllib.error
import urllib.request

API = "https://api.1claw.co"
AGENT_ID = "ca018685-ec2c-4e96-8a48-ffad19b33922"  # studio
PURPOSE = "security.agent_pay_guardrails.widen"
SETTINGS = {
    "pay_enabled": True,
    "pay_require_passkey": False,  # unattended: signs only for allowlisted payTo, under caps
    "pay_require_approval": False,
    "pay_max_usd": "10.00",        # a Venice top-up is $5 minimum
    "pay_daily_limit_usd": "15.00",
    "pay_payto_allowlist": [
        "0x2670b922ef37c7df47158725c0cc407b5382293f",  # Venice x402 top-up
    ],
}


def user_token():
    cfg = json.load(open(os.path.expanduser("~/.config/1claw/config.json")))

    def find(o):
        if isinstance(o, dict):
            for v in o.values():
                if isinstance(v, str) and v.count(".") == 2 and len(v) > 100:
                    return v
                r = find(v)
                if r:
                    return r

    tok = find(cfg)
    if not tok:
        raise SystemExit("Not logged in: run `npx -y @1claw/cli@0.61.29 login` first.")
    return tok


def call(method, path, token, body=None, extra_headers=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:600]


def main():
    token = user_token()
    code = getpass.getpass("Authenticator code: ").strip()
    # The TOTP code goes in `password` (the field is shared across factors).
    status, res = call("POST", "/v1/auth/reauth/complete", token,
                       {"purpose": PURPOSE, "method": "totp", "password": code})
    if status >= 300:
        raise SystemExit(f"reauth/complete failed ({status}): {res}")
    rat = res.get("reauth_token") or res.get("token")
    if not rat:
        raise SystemExit(f"No re-auth token in response keys: {list(res)}")
    status, res = call("PATCH", f"/v1/agents/{AGENT_ID}/pay/settings", token, SETTINGS,
                       {"X-Auth-Confirm": rat})  # single-use: a second PATCH needs a new code
    print(f"PATCH pay/settings -> {status}")
    print(json.dumps(res, indent=2) if isinstance(res, dict) else res)


if __name__ == "__main__":
    main()
