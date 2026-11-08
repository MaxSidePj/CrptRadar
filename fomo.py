"""FOMO app (fomo.family) signals via the unofficial fomoapi.io service.

- Live feed of FOMO users' large trades: free, no key needed (60s delay; 15s with a free key).
  Each connection also replays roughly the last 15 minutes, so runs every 15 minutes see it all.
- FOMO trending board: needs a free key (FOMO_API_KEY); polled at most once per hour to stay
  inside the free 1,000 calls/month.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque

WS_URL = "wss://api.fomoapi.io/ws/alerts"
BOARD_URL = "https://api.fomoapi.io/v2/leaderboard/tokens/trending?limit=50"
CHAIN_MAP = {"eth": "ethereum", "ethereum": "ethereum", "bnb": "bsc", "bsc": "bsc", "solana": "solana",
             "base": "base", "robinhood": "robinhood", "arbitrum": "arbitrum", "polygon": "polygon",
             "abstract": "abstract", "monad": "monad", "avalanche": "avalanche", "tron": "tron"}
KEEP_SECONDS = 24 * 3600


def norm_chain(c):
    return CHAIN_MAP.get((c or "").lower())


class FomoStream:
    """Background WebSocket reader. Events are drained into state by `absorb`."""

    def __init__(self, log):
        self.log = log
        self.queue = deque(maxlen=20000)
        self.stop = threading.Event()
        self.connected = False
        self.thread = None
        try:
            import websocket  # noqa: F401  (websocket-client)
            self.available = True
        except ImportError:
            self.available = False
            log("FOMO stream off: websocket-client not installed")

    def start(self):
        if not self.available:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        import websocket
        key = os.environ.get("FOMO_API_KEY", "").strip()
        url = WS_URL + (f"?key={key}" if key else "")
        backoff = 5
        while not self.stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=30, header=["User-Agent: GemRadar"])
                self.connected = True
                backoff = 5
                while not self.stop.is_set():
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    try:
                        m = json.loads(msg)
                    except ValueError:
                        continue
                    if m.get("type") == "alert":
                        self.queue.append(m)
            except Exception as e:  # network hiccup: reconnect
                self.log(f"  FOMO stream: {type(e).__name__}, reconnecting")
            finally:
                self.connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    def close(self):
        self.stop.set()

    def drain(self):
        out = []
        while self.queue:
            out.append(self.queue.popleft())
        return out


def token_key(chain, addr):
    addr = (addr or "").strip()
    return f"{chain}:{addr.lower() if addr.startswith('0x') else addr}"


def absorb(state, events):
    """Store FOMO buys/sells/theses per token (24h window). Returns number of new events."""
    fomo = state.setdefault("fomo", {})
    seen = state.setdefault("fomo_seen", [])
    seen_set = set(seen)
    new = 0
    for e in events:
        eid = e.get("id") or e.get("eventId")
        if not eid or eid in seen_set:
            continue
        typ = e.get("alertType")
        chain = norm_chain(e.get("chain"))
        addr = e.get("tokenAddress")
        if typ not in ("buy", "sell", "thesis") or not chain or not addr:
            continue
        seen.append(eid)
        seen_set.add(eid)
        new += 1
        ts = (e.get("ts") or e.get("execTs") or time.time() * 1000) / 1000
        k = token_key(chain, addr)
        rec = fomo.setdefault(k, {"chain": chain, "address": addr, "symbol": e.get("token"),
                                  "first": ts, "buys": [], "sells": [], "theses": []})
        trader = e.get("trader") or "?"
        if typ == "buy":
            rec["buys"].append([ts, trader, round(float(e.get("tradeUsd") or e.get("usdValue") or 0))])
        elif typ == "sell":
            rec["sells"].append([ts, trader, round(float(e.get("tradeUsd") or 0))])
        else:
            rec["theses"].append([ts, trader])
        rec["first"] = min(rec["first"], ts)
    del seen[:-5000]
    cutoff = time.time() - KEEP_SECONDS
    for k in list(fomo):
        r = fomo[k]
        for f in ("buys", "sells", "theses"):
            r[f] = [x for x in r[f] if x[0] >= cutoff]
        if not (r["buys"] or r["sells"] or r["theses"]):
            del fomo[k]
    return new


def poll_board(state, log, http_timeout=20):
    """FOMO trending board (needs FOMO_API_KEY). At most once per hour; ETag makes repeats free."""
    key = os.environ.get("FOMO_API_KEY", "").strip()
    board = state.setdefault("fomo_board", {"ts": 0, "etag": None, "ranks": {}})
    # every 90 min = ~480 calls/month, well inside the free 1,000 (unchanged boards cost nothing)
    if not key or time.time() - board["ts"] < 5400 or time.time() < board.get("paused_until", 0):
        return board["ranks"]
    board["ts"] = time.time()
    headers = {"authorization": f"Bearer {key}", "User-Agent": "GemRadar", "Accept": "application/json"}
    if board.get("etag"):
        headers["If-None-Match"] = board["etag"]
    try:
        req = urllib.request.Request(BOARD_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            board["etag"] = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code in (402, 429):  # free credits used up / rate limited: stop until tomorrow
            board["paused_until"] = time.time() + 86400
        if e.code != 304:
            log(f"  FOMO board http {e.code}")
        return board["ranks"]
    except Exception as e:
        log(f"  FOMO board error {type(e).__name__}")
        return board["ranks"]
    ranks = {}
    for i, t in enumerate(data.get("tokens") or []):
        tok = t.get("token") or {}
        chain = norm_chain(t.get("network") or tok.get("network") or t.get("chain"))
        addr = tok.get("address") or t.get("address")
        if chain and addr:
            ranks[token_key(chain, addr)] = int(t.get("rank") or i + 1)
    board["ranks"] = ranks
    log(f"  FOMO trending board: {len(ranks)} tokens")
    return ranks


def stats(state, key, now=None):
    """FOMO activity summary for one token."""
    now = now or time.time()
    r = (state.get("fomo") or {}).get(key)
    rank = ((state.get("fomo_board") or {}).get("ranks") or {}).get(key)
    if not r and not rank:
        return None
    r = r or {"buys": [], "sells": [], "theses": [], "first": now}

    def traders(rows, hours):
        return {x[1] for x in rows if now - x[0] <= hours * 3600}

    b1, b2, b6, b24 = (traders(r["buys"], h) for h in (1, 2, 6, 24))
    s6 = traders(r["sells"], 6)
    return {
        "buyers_1h": len(b1), "buyers_2h": len(b2), "buyers_6h": len(b6), "buyers_24h": len(b24),
        "sellers_6h": len(s6),
        "buy_usd_6h": sum(x[2] for x in r["buys"] if now - x[0] <= 6 * 3600),
        "theses_24h": len(r["theses"]),
        "top_buyers": sorted(b6)[:5],
        "rank": rank,
        "first_seen_h": round((now - r["first"]) / 3600, 1),
    }


def points(fs):
    """0-15 points for FOMO app activity."""
    if not fs:
        return 0.0, []
    import math
    why = []
    n = fs["buyers_6h"]
    p = 8 * min(1.0, math.log10(1 + n) / math.log10(11))  # 10 traders -> full
    if n >= 2:
        why.append(f"{n} FOMO traders bought ({', '.join(fs['top_buyers'][:3])})")
    if fs["rank"]:
        p += 5 if fs["rank"] <= 10 else 3 if fs["rank"] <= 30 else 1.5
        why.append(f"#{fs['rank']} on FOMO trending")
    if fs["theses_24h"]:
        p += 2
        why.append(f"{fs['theses_24h']} FOMO thesis posts")
    if fs["sellers_6h"] > max(2, 2 * n):
        p -= 4
        why.append("FOMO traders are selling")
    return max(0.0, min(15.0, p)), why
