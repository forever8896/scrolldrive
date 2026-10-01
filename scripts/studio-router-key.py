#!/usr/bin/env python3
"""Mint a Shroud router key (sk-shroud-v1-...) for the `studio` agent and store it privately.

Run in your own terminal: python3 scripts/studio-router-key.py
Appends SHROUD_ROUTER_KEY to ~/.config/1claw/agents/studio.env (mode 600); never printed.
The key has a $5 spend cap; revoke any time with `1claw agent revoke-router-key`.
"""
import json
import os
import urllib.error
import urllib.request

AGENT_ID = "ca018685-ec2c-4e96-8a48-ffad19b33922"  # studio
ENV = os.path.expanduser("~/.config/1claw/agents/studio.env")


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

    return find(cfg)


def find_key(o):
    if isinstance(o, dict):
        for v in o.values():
            if isinstance(v, str) and v.startswith("sk-shroud-v1"):
                return v
            r = find_key(v)
            if r:
                return r


body = {"name": "director", "spend_cap_usd": 5, "max_concurrent_streams": 5}
req = urllib.request.Request(
    f"https://api.1claw.co/v1/agents/{AGENT_ID}/router-keys",
    data=json.dumps(body).encode(),
    method="POST",
    headers={"Authorization": f"Bearer {user_token()}", "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
except urllib.error.HTTPError as e:
    raise SystemExit(f"router-key mint failed ({e.code}): {e.read().decode()[:400]}")

key = find_key(data)
if not key:
    raise SystemExit(f"No sk-shroud-v1 key in response; fields were: {list(data)}")
lines = [l for l in (open(ENV).read().splitlines() if os.path.exists(ENV) else [])
         if not l.startswith("SHROUD_ROUTER_KEY=")]
lines.append(f"SHROUD_ROUTER_KEY={key}")
fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, ("\n".join(lines) + "\n").encode())
os.close(fd)
print(f"Router key saved to {ENV} (mode 600, $5 spend cap). Not printed.")
