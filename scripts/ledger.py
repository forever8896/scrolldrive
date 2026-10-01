"""The record of customer payments, kept in 1Claw Agent Memory (durable tier).

Each accepted payment is one entry, keyed by its transaction hash, in the studio agent's
memory. It is encrypted at rest by 1Claw, survives runtime restarts and redeploys, and is
shared by every place the agent runs (the 1Claw runtime and a local studio alike), so a
transaction can unlock a film exactly once, anywhere.

A local JSONL copy is kept as a receipt log; it is never the source of truth.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x402pay  # noqa: E402

NAMESPACE = "frameline-payments"
LOCAL_LOG = os.path.join(x402pay.ROOT, "outputs", "payments-in.jsonl")


class LedgerUnavailable(Exception):
    """1Claw memory could not be read or written; payments must not be accepted blind."""


def _call(method, key="", body=None):
    url = f"{x402pay.API}/v1/agents/{x402pay.AGENT_ID}/memory/{NAMESPACE}" + (f"/{key}" if key else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={**x402pay.auth_headers(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LedgerUnavailable(f"1Claw memory unreachable: {e}")


def is_used(tx):
    status, _ = _call("GET", tx)
    if status == 200:
        return True
    if status == 404:
        return False
    raise LedgerUnavailable(f"1Claw memory answered {status}")


def record(entry):
    """Store an accepted payment. Raises LedgerUnavailable if 1Claw did not store it."""
    status, _ = _call("PUT", entry["tx"], {"value": json.dumps(entry), "tier": "durable"})
    if status not in (200, 201):
        raise LedgerUnavailable(f"1Claw memory refused the write ({status})")
    os.makedirs(os.path.dirname(LOCAL_LOG), exist_ok=True)
    with open(LOCAL_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def entries():
    """Every recorded payment, newest first (for an owner view or reconciliation)."""
    status, data = _call("GET")
    if status != 200 or not data:
        return []
    rows = data.get("entries") or data.get("items") or data if isinstance(data, (dict, list)) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            out.append(json.loads(row["value"]))
        except (KeyError, TypeError, ValueError):
            pass
    return sorted(out, key=lambda e: -e.get("at", 0))


if __name__ == "__main__":
    for e in entries():
        print(f"{e.get('usd', 0):>7.2f} USDC  {e['tx']}  from {e.get('from')}  board {e.get('board')}")
