"""FOMO trades of ANY size on Solana, read for free from the public blockchain.

Every FOMO-app trade on Solana is signed by one FOMO wallet (identified by Bitquery from on-chain
activity; FOMO has not confirmed it). That wallet signs ~500 transactions a minute, far too many to
open one by one on the free public RPC. So:

1. Each cycle we only collect the IDs (signatures) of that wallet's newest transactions: 1-3 calls.
   We keep 12 hours of them as short prefixes.
2. For a promising coin we fetch the coin's own latest 1,000 transaction IDs (1 call) and match them
   against FOMO's IDs. Only matches are opened (max 8 per coin) to see which wallet bought or sold.

Free public RPC, no key. Calls are paced (one every 2.5 s, max ~16 per cycle) and pause on 429.
"""
from __future__ import annotations

VERSION = 1  # radar.py checks this, so an outdated upload is reported instead of failing silently

import json
import time
import urllib.error
import urllib.request

RPC = "https://api.mainnet-beta.solana.com"
FOMO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
QUOTE_MINTS = {"So11111111111111111111111111111111111111112",   # wSOL
               "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}  # USDT
PREFIX = 12
INDEX_HOURS = 12
CALLS_PER_CYCLE = 16
GAP = 2.5
COIN_RECHECK = 3 * 3600
MAX_PARSE_PER_COIN = 8


class Rpc:
    def __init__(self, log):
        self.log, self.last, self.calls, self.paused_until = log, 0.0, 0, 0.0

    def budget_left(self):
        return CALLS_PER_CYCLE - self.calls if time.time() >= self.paused_until else 0

    def call(self, method, params):
        if self.budget_left() <= 0:
            return None
        wait = self.last + GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "GemRadar"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                self.paused_until = time.time() + 120  # be polite: back off for 2 minutes
            self.log(f"  Solana RPC {method}: http {e.code}")
            return None
        except Exception as e:
            self.log(f"  Solana RPC {method}: {type(e).__name__}")
            return None
        return data.get("result")


def _st(state):
    st = state.setdefault("fomo_chain", {})
    st.setdefault("index", [])     # [[hour_ts, "concatenated signature prefixes"], ...]
    st.setdefault("newest", None)  # newest FOMO signature already indexed
    st.setdefault("coins", {})     # key -> {"ts", "holders", "buyers", "sellers", "trades", "span_h"}
    st.setdefault("parsed", {})    # signature prefix -> [wallet, side, ts, mint]
    return st


def update_index(state, rpc):
    """Add FOMO's newest transaction IDs to the 12-hour index (1-3 calls)."""
    st = _st(state)
    new, before = [], None
    for _ in range(3):
        params = {"limit": 1000}
        if st["newest"]:
            params["until"] = st["newest"]
        if before:
            params["before"] = before
        page = rpc.call("getSignaturesForAddress", [FOMO_SIGNER, params])
        if not page:
            break
        new += [(s["signature"], s.get("blockTime") or time.time()) for s in page if not s.get("err")]
        if len(page) < 1000 or not st["newest"]:
            break
        before = page[-1]["signature"]
    if not new:
        return 0
    st["newest"] = new[0][0]
    buckets = {b[0]: b[1] for b in st["index"]}
    for sig, ts in new:
        hour = int(ts // 3600 * 3600)
        buckets[hour] = buckets.get(hour, "") + sig[:PREFIX]
    cutoff = time.time() - INDEX_HOURS * 3600
    st["index"] = sorted([[h, s] for h, s in buckets.items() if h >= cutoff - 3600])
    st["parsed"] = {k: v for k, v in st["parsed"].items() if v[2] >= time.time() - 7 * 86400}
    return len(new)


def _index_set(st):
    out = set()
    for _, s in st["index"]:
        out.update(s[i:i + PREFIX] for i in range(0, len(s), PREFIX))
    return out


def _parse(rpc, sig, mint):
    tx = rpc.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    if not tx:
        return None
    keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys") or []
    signers = {k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")} - {FOMO_SIGNER}
    meta = tx.get("meta") or {}
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    delta = {}
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") != mint:
            continue
        p = pre.get(b["accountIndex"]) or {}
        d = float(b["uiTokenAmount"].get("uiAmount") or 0) - float((p.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        delta[b.get("owner")] = delta.get(b.get("owner"), 0) + d
    for b in meta.get("preTokenBalances") or []:  # token account closed in this tx (sold everything)
        if b.get("mint") == mint and b["accountIndex"] not in {x["accountIndex"] for x in meta.get("postTokenBalances") or []}:
            delta[b.get("owner")] = delta.get(b.get("owner"), 0) - float(b["uiTokenAmount"].get("uiAmount") or 0)
    users = [o for o in delta if o in signers and delta[o]]
    if not users:
        return None
    wallet = users[0]
    return [wallet, "buy" if delta[wallet] > 0 else "sell", tx.get("blockTime") or time.time(), mint]


def check_coin(state, rpc, key, mint, index):
    """Which FOMO wallets traded this coin recently, and which still hold it."""
    st = _st(state)
    sigs = rpc.call("getSignaturesForAddress", [mint, {"limit": 1000}])
    if sigs is None:
        return None
    matches = [s for s in sigs if not s.get("err") and s["signature"][:PREFIX] in index]
    trades, opened = [], 0
    for s in matches:
        pref = s["signature"][:PREFIX]
        p = st["parsed"].get(pref)
        if p is None and opened < MAX_PARSE_PER_COIN and rpc.budget_left() > 0:
            opened += 1
            p = _parse(rpc, s["signature"], mint) or ["", "none", time.time(), mint]  # remember non-trades too
            st["parsed"][pref] = p
        if p and p[1] in ("buy", "sell") and p[3] == mint:
            trades.append(p)
    last = {}
    for wallet, side, ts, _ in sorted(trades, key=lambda x: x[2]):
        last[wallet] = side
    span_h = (sigs[0]["blockTime"] - sigs[-1]["blockTime"]) / 3600 if len(sigs) > 1 and sigs[-1].get("blockTime") else 0
    res = {"ts": time.time(), "holders": sorted(w for w, side in last.items() if side == "buy"),
           "buyers": len({t[0] for t in trades if t[1] == "buy"}), "sellers": len({t[0] for t in trades if t[1] == "sell"}),
           "trades": len(matches), "span_h": round(min(span_h, INDEX_HOURS), 1)}
    st["coins"][key] = res
    return res


def due(state, key):
    c = _st(state)["coins"].get(key)
    return not c or time.time() - c["ts"] >= COIN_RECHECK


def run_cycle(state, rpc, candidates, log):
    """candidates: [(key, mint)] in priority order. Index update first, then as many coins as budget allows."""
    rpc.calls = 0
    n = update_index(state, rpc)
    index = _index_set(_st(state))
    checked = 0
    for key, mint in candidates:
        if rpc.budget_left() < 2:
            break
        if due(state, key) and check_coin(state, rpc, key, mint, index) is not None:
            checked += 1
    st = _st(state)
    st["coins"] = {k: v for k, v in st["coins"].items() if time.time() - v["ts"] < 2 * 86400}
    log(f"FOMO on-chain: +{n} FOMO transactions indexed, {checked} coins checked, {rpc.calls} RPC calls")


def summary(state, key):
    return (state.get("fomo_chain") or {}).get("coins", {}).get(key)
