"""Just enough Ethereum, in plain Python: Keccak-256, recovering the signer of a
personal_sign message, and reading a USDC payment from a Base transaction receipt.

No dependencies, so the app still runs on a bare Python (locally and on a 1Claw runtime).
Tested against eth-account in development; see the check at the bottom.
"""
import json
import urllib.request

# --- Keccak-256 (the original padding Ethereum uses, not NIST SHA3-256) -----------------------

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(v, n):
    return ((v << n) | (v >> (64 - n))) & _M if n else v


def _keccak_f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


# --- secp256k1 signer recovery ---------------------------------------------------------------

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        lam = 3 * p[0] * p[0] * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k, p):
    out = None
    while k:
        if k & 1:
            out = _add(out, p)
        p = _add(p, p)
        k >>= 1
    return out


def _address(point):
    return "0x" + keccak256(point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big"))[-20:].hex()


def recover(msg_hash: bytes, signature: bytes) -> str:
    """Address that signed msg_hash; signature is 65 bytes r || s || v (v 0/1 or 27/28)."""
    if len(signature) != 65:
        raise ValueError("signature must be 65 bytes")
    r, s, v = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:64], "big"), signature[64]
    v = v - 27 if v >= 27 else v
    if v not in (0, 1) or not (0 < r < _N and 0 < s < _N):
        raise ValueError("malformed signature")
    y = pow((pow(r, 3, _P) + 7) % _P, (_P + 1) // 4, _P)
    if (y * y - pow(r, 3, _P) - 7) % _P:
        raise ValueError("r is not on the curve")
    if y % 2 != v:
        y = _P - y
    rinv = pow(r, -1, _N)
    e = int.from_bytes(msg_hash, "big")
    q = _add(_mul((-e * rinv) % _N, _G), _mul((s * rinv) % _N, (r, y)))
    if q is None:
        raise ValueError("bad signature")
    return _address(q)


def personal_recover(message: str, signature_hex: str) -> str:
    """Signer of an EIP-191 personal_sign message, lowercase 0x address."""
    data = message.encode()
    h = keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    return recover(h, bytes.fromhex(signature_hex.removeprefix("0x")))


# --- USDC payments on Base --------------------------------------------------------------------

BASE_RPC = "https://mainnet.base.org"
TRANSFER_TOPIC = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()


def rpc(method, params, url=BASE_RPC):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "frameline/0.3"})
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def usdc_payment(tx_hash, token, to):
    """Read a USDC transfer to `to` from a transaction on Base.
    Returns None while the transaction is not mined yet, else a dict with
    ok, from, units (6 decimals), block_time; ok is False for a failed transaction."""
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None
    if int(receipt["status"], 16) != 1:
        return {"ok": False, "reason": "the transaction failed on chain"}
    paid, payer = 0, None
    for log in receipt.get("logs", []):
        t = log.get("topics", [])
        if (log["address"].lower() == token.lower() and len(t) == 3 and t[0] == TRANSFER_TOPIC
                and "0x" + t[2][-40:] == to.lower()):
            paid += int(log["data"], 16)
            payer = "0x" + t[1][-40:]
    if not paid:
        return {"ok": False, "reason": "no USDC transfer to the studio in that transaction"}
    block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
    if not block:  # Base's public RPC is load-balanced: a fresh receipt can arrive before its block
        return None
    return {"ok": True, "from": payer, "units": paid, "block_time": int(block["timestamp"], 16)}


if __name__ == "__main__":
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert TRANSFER_TOPIC == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    print("keccak ok")
