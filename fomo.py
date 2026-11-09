"""FOMO app (fomo.family) signals via the unofficial fomoapi.io service.

Free, no key: live feed of FOMO users' large trades (60s delay; 15s with a free key). Each connection
replays roughly the last 15 minutes, so runs every 15 minutes see almost everything. Trades are kept
for 7 days, which shows which FOMO traders still hold a coin.

With a free key (FOMO_API_KEY): the top-trader leaderboard (daily), FOMO's trending and graduated coin
boards (every 4h) and "which FOMO traders hold this coin" lookups (max 15 a day). A monthly counter
keeps this at or below 900 of the 1,000 free calls. The free plan has no card, so it can never bill.
"""
from __future__ import annotations

VERSION = 4  # radar.py checks this, so an outdated upload is reported instead of failing silently

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque

WS_URL = "wss://api.fomoapi.io/ws/alerts"
CHAIN_MAP = {"eth": "ethereum", "ethereum": "ethereum", "bnb": "bsc", "bsc": "bsc", "solana": "solana",
             "base": "base", "robinhood": "robinhood", "arbitrum": "arbitrum", "polygon": "polygon",
             "abstract": "abstract", "monad": "monad", "avalanche": "avalanche", "tron": "tron"}


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


KEEP_DAYS = 7


def absorb(state, events):
    """Store FOMO buys/sells/theses per token (7-day window). Returns number of new events."""
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
            rec["buys"].append([ts, trader, round(_num(e.get("tradeUsd") or e.get("usdValue")))])
        elif typ == "sell":
            rec["sells"].append([ts, trader, round(_num(e.get("tradeUsd")))])
        else:
            rec["theses"].append([ts, trader])
        rec["first"] = min(rec["first"], ts)
    del seen[:-8000]
    cutoff = time.time() - KEEP_DAYS * 86400
    for k in list(fomo):
        r = fomo[k]
        for f in ("buys", "sells", "theses"):
            r[f] = [x for x in r[f] if x[0] >= cutoff]
        if not (r["buys"] or r["sells"] or r["theses"]):
            del fomo[k]
    return new


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def stream_holders(rec):
    """Traders whose latest large trade on this coin (last 7 days) was a buy: they still hold."""
    last = {}
    for ts, trader, _ in rec.get("buys", []):
        last[trader] = max(last.get(trader, ("", 0)), ("buy", ts), key=lambda x: x[1])
    for ts, trader, _ in rec.get("sells", []):
        if ts >= last.get(trader, ("", 0))[1]:
            last[trader] = ("sell", ts)
    return sorted(t for t, (side, _) in last.items() if side == "buy")


# --------------------------------------------------------------------------- REST (needs a free key)
# Free plan: 1,000 calls/month. Budget: leaderboard 1/day + 2 coin boards every 4h + max 15 holder
# lookups/day = at most 28/day (~870/month). A hard monthly counter stops everything at 900.

API = "https://api.fomoapi.io"
MONTHLY_CAP = 900
DAILY_CAP = 29
HOLDER_LOOKUPS_PER_DAY = 15


def _budget(state):
    b = state.setdefault("fomo_api", {})
    month, day = time.strftime("%Y-%m", time.gmtime()), time.strftime("%Y-%m-%d", time.gmtime())
    if b.get("month") != month:
        b.update(month=month, calls=0)
    if b.get("day") != day:
        b.update(day=day, day_calls=0, holder_calls=0)
    return b


def _can_call(state, holder=False):
    b = _budget(state)
    if not os.environ.get("FOMO_API_KEY", "").strip() or time.time() < b.get("paused_until", 0):
        return False
    if b["calls"] >= MONTHLY_CAP or b["day_calls"] >= DAILY_CAP:
        return False
    return not holder or b["holder_calls"] < HOLDER_LOOKUPS_PER_DAY


def _get(state, path, log, holder=False):
    b = _budget(state)
    b["calls"] += 1
    b["day_calls"] += 1
    if holder:
        b["holder_calls"] += 1
    key = os.environ.get("FOMO_API_KEY", "").strip()
    req = urllib.request.Request(API + path, headers={"authorization": f"Bearer {key}", "User-Agent": "GemRadar",
                                                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 402, 429):  # bad key / free credits used up / rate limited: stop for a day
            b["paused_until"] = time.time() + 86400
        log(f"  FOMO API {path.split('?')[0]}: http {e.code}")
    except Exception as e:
        log(f"  FOMO API {path.split('?')[0]}: {type(e).__name__}")
    return None


def _rows(data, *names):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for n in names:
            if isinstance(data.get(n), list):
                return data[n]
    return []


def _handle(row):
    if not isinstance(row, dict):
        return None
    for k in ("handle", "username", "userName", "name", "trader"):
        if isinstance(row.get(k), str) and row[k]:
            return row[k]
    user = row.get("user") or row.get("trader")
    return _handle(user) if isinstance(user, dict) else None


def _board_tokens(data):
    out = {}
    for i, t in enumerate(_rows(data, "tokens", "board", "data")):
        tok = t.get("token") or {}
        chain = norm_chain(t.get("network") or tok.get("network") or t.get("chain") or tok.get("chain"))
        addr = tok.get("address") or t.get("address")
        if chain and addr:
            out[token_key(chain, addr)] = {"rank": int(_num(t.get("rank")) or i + 1),
                                           "holders": int(_num(t.get("holders")))}
    return out


def refresh(state, log):
    """Periodic FOMO REST data: good traders (daily) and trending/graduated coin boards (every 4h)."""
    st = state.setdefault("fomo_rest", {"leaders": [], "leaders_ts": 0, "trending": {}, "trending_ts": 0,
                                        "graduated": {}, "graduated_ts": 0, "holders": {}})
    now = time.time()
    if now - st["leaders_ts"] > 86400 and _can_call(state):
        st["leaders_ts"] = now
        data = _get(state, "/v2/leaderboard/30d?limit=150", log)
        leaders = [h for h in (_handle(r) for r in _rows(data, "traders", "leaderboard", "board", "data")) if h]
        if leaders:
            st["leaders"] = leaders
        log(f"  FOMO leaderboard: {len(leaders)} top traders")
    for board, path in (("trending", "/v2/leaderboard/tokens/trending?limit=50"),
                        ("graduated", "/v2/leaderboard/tokens/graduated?limit=50")):
        if now - st[f"{board}_ts"] > 4 * 3600 and _can_call(state):
            st[f"{board}_ts"] = now
            data = _get(state, path, log)
            if data is not None:
                st[board] = _board_tokens(data)
            log(f"  FOMO {board} board: {len(st[board])} coins")
    # keep the old field the scorer reads
    state["fomo_board"] = {"ranks": {k: v["rank"] for k, v in st["trending"].items()}}
    st["holders"] = {k: v for k, v in st["holders"].items() if now - v["ts"] < 3 * 86400}
    return st


def lookup_holders(state, key, chain, address, log):
    """Which FOMO traders hold this coin right now (max 15 lookups a day, cached 12h)."""
    st = state.setdefault("fomo_rest", {}).setdefault("holders", {})
    cached = st.get(key)
    if cached and time.time() - cached["ts"] < 12 * 3600:
        return cached["handles"]
    if not _can_call(state, holder=True):
        return cached["handles"] if cached else None
    data = _get(state, f"/token/{address}/holders?limit=50", log, holder=True)
    if data is None:
        return cached["handles"] if cached else None
    handles = sorted({h for h in (_handle(r) for r in _rows(data, "holders", "traders", "data")) if h})
    st[key] = {"ts": time.time(), "handles": handles}
    log(f"  FOMO holders of {address[:6]}…: {len(handles)}")
    return handles


def wants_lookup(state, key):
    st = (state.get("fomo_rest") or {}).get("holders") or {}
    return key not in st or time.time() - st[key]["ts"] >= 12 * 3600


def stats(state, key, now=None):
    """FOMO activity summary for one token."""
    now = now or time.time()
    r = (state.get("fomo") or {}).get(key)
    rest = state.get("fomo_rest") or {}
    trend = (rest.get("trending") or {}).get(key) or {}
    grad = (rest.get("graduated") or {}).get(key)
    rank = trend.get("rank") or ((state.get("fomo_board") or {}).get("ranks") or {}).get(key)
    api_holders = ((rest.get("holders") or {}).get(key) or {}).get("handles") or []
    chain = ((state.get("fomo_chain") or {}).get("coins") or {}).get(key) or {}
    if not r and not rank and not grad and not api_holders and not chain.get("trades"):
        return None
    r = r or {"buys": [], "sells": [], "theses": [], "first": now}

    def traders(rows, hours):
        return {x[1] for x in rows if now - x[0] <= hours * 3600}

    b1, b2, b6, b24, b7d = (traders(r["buys"], h) for h in (1, 2, 6, 24, 24 * 7))
    s6, s24 = traders(r["sells"], 6), traders(r["sells"], 24)
    holders = sorted(set(stream_holders(r)) | set(api_holders))
    leaders = set(rest.get("leaders") or [])
    good = [h for h in holders if h in leaders]
    good_recent = sorted(traders(r["buys"], 48) & leaders)        # top traders who bought in the last 48h
    fresh_chain = chain if chain and now - chain.get("ts", 0) < 3 * 3600 else {}
    board_holders = max(trend.get("holders") or 0, (grad or {}).get("holders") or 0)
    return {
        "buyers_1h": len(b1), "buyers_2h": len(b2), "buyers_6h": len(b6), "buyers_24h": len(b24),
        "buyers_7d": len(b7d), "sellers_6h": len(s6), "sellers_24h": len(s24),
        # FOMO traders we can see by name or wallet (not FOMO's popularity count of all its users)
        "named_holders": max(len(holders), len(chain.get("holders") or [])),
        "good_recent": good_recent[:10],
        "fresh_buyers": max(len(b24), fresh_chain.get("buyers", 0)),
        "fresh_sellers": max(len(s24), fresh_chain.get("sellers", 0)),
        "buy_usd_6h": sum(x[2] for x in r["buys"] if now - x[0] <= 6 * 3600),
        "theses_24h": len([x for x in r["theses"] if now - x[0] <= 86400]),
        "top_buyers": sorted(b6)[:5],
        "holders": holders[:20],
        "holders_n": max(len(holders), board_holders, len(chain.get("holders") or [])),
        "wallets_holding": len(chain.get("holders") or []),      # on-chain: any trade size (Solana)
        "onchain_trades": chain.get("trades", 0), "onchain_span_h": chain.get("span_h"),
        "good_holders": good[:10], "graduated": bool(grad),
        "rank": rank,
        "first_seen_h": round((now - r["first"]) / 3600, 1),
    }


def has_signal(fs):
    """Enough FOMO interest to look at a coin even while its trading is still quiet."""
    return bool(fs) and (fs["holders_n"] >= 2 or fs["good_holders"] or fs["buyers_24h"] >= 2 or fs["rank"])


def points(fs):
    """0-15 points for FOMO app activity."""
    if not fs:
        return 0.0, []
    import math
    why = []
    n = fs["buyers_6h"]
    p = 6 * min(1.0, math.log10(1 + n) / math.log10(11))           # recent buyers (10 -> full)
    if n >= 2:
        why.append(f"{n} FOMO traders bought ({', '.join(fs['top_buyers'][:3])})")
    h = fs["holders_n"]
    p += 5 * min(1.0, math.log10(1 + h) / math.log10(11))           # FOMO traders still holding
    if h >= 2:
        why.append(f"{h} FOMO traders hold it")
    elif fs.get("onchain_trades"):
        why.append(f"{fs['onchain_trades']} FOMO trades on-chain")
    if fs["good_holders"]:
        p += min(4, 2 * len(fs["good_holders"]))                     # top-150 traders count extra
        why.append(f"top FOMO traders hold: {', '.join(fs['good_holders'][:3])}")
    if fs["rank"]:
        p += 3 if fs["rank"] <= 10 else 2 if fs["rank"] <= 30 else 1
        why.append(f"#{fs['rank']} on FOMO trending")
    if fs["graduated"]:
        p += 1
    if fs["theses_24h"]:
        p += 1
        why.append(f"{fs['theses_24h']} FOMO thesis posts")
    if fs["sellers_6h"] > max(2, 2 * n):
        p -= 4
        why.append("FOMO traders are selling")
    return max(0.0, min(15.0, p)), why
