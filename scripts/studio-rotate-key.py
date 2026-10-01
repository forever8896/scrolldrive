#!/usr/bin/env python3
"""Issue a new API key for the `studio` agent and store it privately.

Run in your own terminal: python3 scripts/studio-rotate-key.py
The key is written to ~/.config/1claw/agents/studio.env (mode 600) and never printed.
Rotating invalidates any previous key for this agent.
"""
import json
import os
import urllib.error
import urllib.request

AGENT_ID = "ca018685-ec2c-4e96-8a48-ffad19b33922"  # studio
OUT = os.path.expanduser("~/.config/1claw/agents/studio.env")


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
            if isinstance(v, str) and v.startswith("ocv_"):
                return v
            r = find_key(v)
            if r:
                return r


req = urllib.request.Request(
    f"https://api.1claw.co/v1/agents/{AGENT_ID}/rotate-key",
    data=b"{}",
    method="POST",
    headers={"Authorization": f"Bearer {user_token()}", "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
except urllib.error.HTTPError as e:
    raise SystemExit(f"rotate-key failed ({e.code}): {e.read().decode()[:400]}")

key = find_key(data)
if not key:
    raise SystemExit(f"No ocv_ key in response; fields were: {list(data)}")
os.makedirs(os.path.dirname(OUT), mode=0o700, exist_ok=True)
fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.write(fd, f"ONECLAW_AGENT_ID={AGENT_ID}\nONECLAW_AGENT_API_KEY={key}\n".encode())
os.close(fd)
print(f"New studio key saved to {OUT} (mode 600). Not printed.")
