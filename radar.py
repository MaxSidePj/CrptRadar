#!/usr/bin/env python3
"""GemRadar - early crypto token scanner with scam checks and ntfy alerts.

Runs on GitHub Actions. Each run scans repeatedly for RUN_SECONDS, then writes
state/state.json and the dashboard in site/.

Free data sources (no API keys):
  GeckoTerminal   new pools + trending pools on every chain it indexes
  DexScreener     market data, socials, paid profiles, boosts, community takeovers
  GoPlus          contract safety + holders for EVM chains and Tron
  RugCheck        Solana safety, holders, insider networks (bubblemap-style clusters)
  trench.bot      Solana bundle check (best effort; often unavailable)
  fxtwitter       X account followers / age (X's own API is not free)
  fomoapi.io      FOMO app: public trades of FOMO users + FOMO trending board (unofficial)
  4chan /biz/     mention count
  CoinGecko       trending list

Environment variables:
  NTFY_TOPIC     your secret ntfy topic (no push if empty)
  DASHBOARD_URL  link to your dashboard
  RUN_SECONDS    how long one run keeps scanning (default 660)
  CYCLE_SECONDS  pause between scans (default 90)
  DRY_RUN=1      score and print, never push
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import fomo

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
ALERTS_FILE = ROOT / "state" / "alerts.json"   # committed to git (small, changes only on alerts)
CACHE_FILE = ROOT / "cache" / "radar.json"     # kept with actions/cache (watch pool, board, caches)
ALERT_KEYS = ("alerted", "alerts", "welcomed", "heartbeat")

# Free-tier ceilings (never exceeded, whatever config.json says)
NTFY_DAILY_CEILING = 60      # ntfy.sh free/anonymous allows 250 messages a day
MAX_DEEP_CHECKS = 15         # GoPlus / RugCheck calls per cycle
WEB_FILES = ("index.html", "app.js", "style.css", "manifest.webmanifest",
             "icon-180.png", "icon-192.png", "icon-512.png")
SITE_DIR = ROOT / "site"

DEFAULT_CONFIG = {
    "alert_min_score": 65,
    "exceptional_score": 85,
    "max_alerts_per_day": 40,
    "max_alerts_per_cycle": 2,
    "gem_max_age_hours": 72,
    "runner_max_age_hours": 720,
    "min_alert_age_hours": 6,
    "min_observed_hours": 3,
    "max_drawdown_pct": 45,
    "fomo_fast_lane": True,
    "fomo_fast_min_buyers": 3,
    "fomo_fast_min_score": 55,
    "min_liquidity_usd": 12000,
    "min_mcap_usd": 30000,
    "max_mcap_usd": 50000000,
    "min_txns_1h": 60,
    "min_volume_1h_usd": 5000,
    "max_top10_pct": 35,
    "max_single_holder_pct": 12,
    "max_creator_pct": 8,
    "max_insider_pct": 20,
    "max_bundle_pct": 20,
    "max_tax_pct": 10,
    "deep_checks_per_cycle": 12,
    "gt_new_pool_pages": 4,
    "gt_trending_networks": ["solana", "base", "bsc", "robinhood", "eth"],
    "watch_rechecks_per_cycle": 900,
    "chains_only": [],
    "chains_excluded": [],
    "quiet_hours_utc": [],
}

# DexScreener chain id -> GoPlus chain id (EVM + Tron). Solana uses RugCheck.
GOPLUS_CHAIN = {
    "ethereum": "1", "bsc": "56", "arbitrum": "42161", "polygon": "137", "base": "8453",
    "optimism": "10", "avalanche": "43114", "zksync": "324", "linea": "59144",
    "mantle": "5000", "unichain": "130", "scroll": "534352", "cronos": "25",
    "gnosischain": "100", "tron": "tron", "robinhood": "4663", "monad": "143",
    "sonic": "146", "abstract": "2741", "berachain": "80094", "worldchain": "480",
    "soneium": "1868", "blast": "81457", "xlayer": "196", "opbnb": "204",
    "story": "1514", "plasma": "9745", "merlin": "4200", "manta": "169",
    "hashkey": "177", "morph": "2818", "stable": "988",
}
# GeckoTerminal network id -> DexScreener chain id (unknown ids pass through).
GT_TO_DS = {
    "eth": "ethereum", "polygon_pos": "polygon", "avax": "avalanche", "sui-network": "sui",
    "world-chain": "worldchain", "zksync": "zksync", "xdai": "gnosischain",
    "arbitrum": "arbitrum", "optimism": "optimism", "cro": "cronos", "ftm": "fantom",
    "hyperevm": "hyperevm", "x-layer": "xlayer", "opbnb": "opbnb", "manta-pacific": "manta",
    "berachain": "berachain", "sonic": "sonic", "abstract": "abstract",
}
BUBBLEMAPS_CHAIN = {"ethereum": "eth", "solana": "solana", "bsc": "bsc", "base": "base",
                    "polygon": "polygon", "arbitrum": "arbitrum", "avalanche": "avalanche",
                    "sonic": "sonic", "ton": "ton", "tron": "tron", "optimism": "optimism"}
DEAD_ADDRESSES = {"0x0000000000000000000000000000000000000000",
                  "0x000000000000000000000000000000000000dead"}
STABLE_OR_MAJOR = {"USDC", "USDT", "WETH", "WBNB", "SOL", "WSOL", "ETH", "BNB", "DAI", "USD1",
                   "USDE", "FDUSD", "WBTC", "BTCB", "CBBTC", "USDG", "PYUSD", "TRX", "TON"}

UA = "Mozilla/5.0 (GemRadar; +https://github.com)"
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    SSL_CTX = ssl.create_default_context()

LOG_PREFIX = ""


def log(*a):
    print(LOG_PREFIX, *a, flush=True)


def now() -> float:
    return time.time()


def fnum(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def logscale(value, lo, hi):
    """0 at <=lo, 1 at >=hi, logarithmic in between."""
    if value <= lo:
        return 0.0
    return clamp(math.log10(value / lo) / math.log10(hi / lo))


# --------------------------------------------------------------------------- http

class Http:
    """Tiny HTTP client with per-host pacing and call counting."""

    def __init__(self):
        self.last = {}
        self.calls = {}
        self.min_gap = {"api.geckoterminal.com": 4.0, "api.gopluslabs.io": 2.1,
                        "api.rugcheck.xyz": 0.8, "api.fxtwitter.com": 0.6,
                        "api.dexscreener.com": 0.25, "trench.bot": 1.0}
        self.dead_until = {}

    def get_json(self, url, timeout=20, retries=1):
        host = urllib.parse.urlparse(url).netloc
        if self.dead_until.get(host, 0) > now():
            return None
        gap = self.min_gap.get(host, 0.3)
        wait = self.last.get(host, 0) + gap - now()
        if wait > 0:
            time.sleep(wait)
        for attempt in range(retries + 1):
            self.last[host] = now()
            self.calls[host] = self.calls.get(host, 0) + 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries:
                    time.sleep(15)
                    continue
                if e.code in (404, 400):
                    return None
                if e.code >= 500 or e.code in (403, 429):
                    # back off from this host for a while
                    self.dead_until[host] = now() + (60 if e.code == 429 else 900 if host == "trench.bot" else 300)
                log(f"  http {e.code} {host}")
                return None
            except Exception as e:
                if attempt < retries:
                    time.sleep(2)
                    continue
                log(f"  http error {host}: {type(e).__name__}")
                self.dead_until[host] = now() + (900 if host == "trench.bot" else 30)
                return None
        return None


# --------------------------------------------------------------------------- config / state

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except Exception as e:
            log(f"config.json unreadable ({e}); using defaults")
    return cfg


def load_state():
    state = {}
    for f in (CACHE_FILE, ALERTS_FILE):
        if f.exists():
            try:
                state.update(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                log(f"{f.name} unreadable ({e})")
    return state


def fresh_state(state):
    state.setdefault("alerted", {})      # key -> alert record
    state.setdefault("alerts", [])       # newest first
    state.pop("snapshots", None)
    state.setdefault("history", {})      # key -> {"first", "peak", "peak_ts", "trend_hits", "snaps": [[ts, mcap, liq, vol_h1, buys_h1, holders]]}
    state.setdefault("safety_cache", {}) # key -> {"ts", "data"}
    state.setdefault("x_cache", {})      # handle -> {"ts", "data"}
    state.setdefault("board", [])
    state.setdefault("rejected", [])
    state.setdefault("stats", {})
    state.setdefault("watch", {})        # key -> [chain, address, symbol, first_seen, last_checked, misses]
    return state


def prune_state(state, cfg):
    t = now()
    keep_s = cfg["runner_max_age_hours"] * 3600 + 86400
    state["history"] = {k: h for k, h in state["history"].items() if h["snaps"] and t - h["snaps"][-1][0] < 24 * 3600}
    state["safety_cache"] = {k: v for k, v in state["safety_cache"].items() if t - v["ts"] < 6 * 3600}
    state["x_cache"] = {k: v for k, v in state["x_cache"].items() if t - v["ts"] < 24 * 3600}
    state["alerted"] = {k: v for k, v in state["alerted"].items() if t - v["ts"] < max(keep_s, 30 * 86400)}
    state["alerts"] = state["alerts"][:150]
    w = state["watch"]
    if len(w) > 5000:
        for k in sorted(w, key=lambda k: w[k][3])[:len(w) - 5000]:
            del w[k]


def _write_json(path, obj, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=indent, separators=None if indent else (",", ":"), ensure_ascii=False)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_state(state):
    _write_json(ALERTS_FILE, {k: state[k] for k in ALERT_KEYS if k in state}, indent=1)
    _write_json(CACHE_FILE, {k: v for k, v in state.items() if k not in ALERT_KEYS})


# --------------------------------------------------------------------------- helpers

def norm_addr(chain, addr):
    addr = (addr or "").strip()
    return addr.lower() if addr.startswith("0x") else addr


def token_key(chain, addr):
    return f"{chain}:{norm_addr(chain, addr)}"


def ds_chain_from_gt(network):
    return GT_TO_DS.get(network, network)


def chain_allowed(chain, cfg):
    if cfg.get("chains_only") and chain not in cfg["chains_only"]:
        return False
    return chain not in (cfg.get("chains_excluded") or [])


def parse_x_handle(url):
    """Handle of the coin's own X profile; None for tweets/communities (those are narrative links)."""
    if not url or "/status/" in url or "/communities/" in url:
        return None
    m = re.match(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", url.strip())
    if not m:
        return None
    h = m.group(1)
    if h.lower() in {"i", "home", "search", "intent", "share", "hashtag", "explore"}:
        return None
    return h


def hours_since_ms(ms):
    if not ms:
        return None
    return max(0.0, (now() * 1000 - ms) / 3_600_000)


# --------------------------------------------------------------------------- discovery

def gt_pools(http, url, tag):
    data = http.get_json(url)
    out = []
    if not data:
        return out
    inc = {i["id"]: i.get("attributes", {}) for i in data.get("included", []) if "id" in i}
    for p in data.get("data", []):
        a = p.get("attributes", {})
        rel = p.get("relationships", {})
        net = (rel.get("network", {}).get("data") or {}).get("id") or p.get("id", "").split("_")[0]
        bt_id = (rel.get("base_token", {}).get("data") or {}).get("id", "")
        if "_" not in bt_id:
            continue
        addr = bt_id.split("_", 1)[1]
        tinfo = inc.get(bt_id, {})
        sym = tinfo.get("symbol") or a.get("name", "?").split(" / ")[0]
        if (sym or "").upper() in STABLE_OR_MAJOR:
            continue
        tx = a.get("transactions", {}) or {}
        created = a.get("pool_created_at")
        created_ms = None
        if created:
            try:
                created_ms = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000
            except ValueError:
                pass
        out.append({
            "chain": ds_chain_from_gt(net), "address": addr, "symbol": sym,
            "name": tinfo.get("name") or sym, "source": tag,
            "gt": {
                "liq": fnum(a.get("reserve_in_usd")),
                "mcap": fnum(a.get("market_cap_usd")) or fnum(a.get("fdv_usd")),
                "vol": {k: fnum(v) for k, v in (a.get("volume_usd") or {}).items()},
                "pc": {k: fnum(v) for k, v in (a.get("price_change_percentage") or {}).items()},
                "buyers_h1": int(fnum((tx.get("h1") or {}).get("buyers"))),
                "sellers_h1": int(fnum((tx.get("h1") or {}).get("sellers"))),
                "buys_h1": int(fnum((tx.get("h1") or {}).get("buys"))),
                "sells_h1": int(fnum((tx.get("h1") or {}).get("sells"))),
                "created_ms": created_ms,
                "pool": p.get("id"),
            },
        })
    return out


def discover(http, cfg, ctx, state):
    """Collect candidate tokens from all free feeds. Returns dict key -> seed."""
    seeds = {}

    def add(chain, addr, source, **extra):
        if not addr or not chain or not chain_allowed(chain, cfg):
            return
        k = token_key(chain, addr)
        s = seeds.setdefault(k, {"key": k, "chain": chain, "address": addr, "sources": set()})
        s["sources"].add(source)
        for ek, ev in extra.items():
            if ev is not None and ek not in s:
                s[ek] = ev

    base = "https://api.geckoterminal.com/api/v2/networks"
    watch = state["watch"]
    for page in range(1, int(cfg["gt_new_pool_pages"]) + 1):
        for r in gt_pools(http, f"{base}/new_pools?page={page}&include=base_token", "new"):
            add(r["chain"], r["address"], "new", symbol=r["symbol"], name=r["name"], gt=r["gt"])
            k = token_key(r["chain"], r["address"])
            if k not in watch and chain_allowed(r["chain"], cfg):
                watch[k] = [r["chain"], r["address"], r["symbol"], now(), 0, 0]
    trending_urls = [f"{base}/trending_pools?page=1&duration={d}&include=base_token" for d in ("5m", "1h")]
    trending_urls += [f"{base}/{n}/trending_pools?page=1&duration=1h&include=base_token"
                      for n in cfg.get("gt_trending_networks") or []]
    for url in trending_urls:
        for r in gt_pools(http, url, "trending"):
            add(r["chain"], r["address"], "trending", symbol=r["symbol"], name=r["name"], gt=r["gt"])
            ctx["gt_trending"].add(token_key(r["chain"], r["address"]))

    ds = "https://api.dexscreener.com"
    for path, tag in (("/token-profiles/latest/v1", "profile"), ("/token-boosts/latest/v1", "boost"),
                      ("/token-boosts/top/v1", "boost"), ("/community-takeovers/latest/v1", "cto")):
        items = http.get_json(ds + path) or []
        if isinstance(items, dict):
            items = items.get("data") or []
        for it in items:
            add(it.get("chainId"), it.get("tokenAddress"), tag)
            k = token_key(it.get("chainId", ""), it.get("tokenAddress", ""))
            if tag == "cto":
                ctx["cto"].add(k)
            elif tag == "profile":
                ctx["profile"].add(k)
            elif tag == "boost":
                ctx["boost"][k] = max(ctx["boost"].get(k, 0), int(fnum(it.get("totalAmount") or it.get("amount"))))
            if k not in watch and it.get("chainId") and chain_allowed(it["chainId"], cfg):
                watch[k] = [it["chainId"], it.get("tokenAddress"), None, now(), 0, 0]
    # Coins in the alerts list: keep scoring them for as long as they stay there.
    for a in state.get("alerts") or []:
        add(a["chain"], a["address"], "alert", symbol=a.get("symbol"))
    # Coins FOMO app traders bought in the last 6 hours, or on FOMO's trending board.
    t_now = now()
    for k, r in (state.get("fomo") or {}).items():
        if any(t_now - b[0] <= 6 * 3600 for b in r["buys"]):
            add(r["chain"], r["address"], "fomo", symbol=r.get("symbol"))
            if k not in watch and chain_allowed(r["chain"], cfg):
                watch[k] = [r["chain"], r["address"], r.get("symbol"), t_now, 0, 0]
    for k in ((state.get("fomo_board") or {}).get("ranks") or {}):
        chain, addr = k.split(":", 1)
        add(chain, addr, "fomo")
    # Re-check launches we saw earlier: this is where early gems show up, hours after launch.
    budget = int(cfg.get("watch_rechecks_per_cycle", 900))
    for k in sorted(watch, key=lambda k: watch[k][4])[:budget]:
        if k not in seeds:
            add(watch[k][0], watch[k][1], "watch", symbol=watch[k][2])
    return seeds


def update_watch(state, seeds, pairs, tokens_by_key, cfg):
    """Track re-check results; forget launches that died or got too old."""
    watch, t = state["watch"], now()
    max_age = cfg["runner_max_age_hours"]
    for k, tok in tokens_by_key.items():  # follow every active coin so we can judge its staying power
        if k not in watch and tok["liq"] >= cfg["min_liquidity_usd"] and (tok.get("age_h") or 0) < max_age:
            watch[k] = [tok["chain"], tok["address"], tok["symbol"], t, t, 0]
    for k, w in list(watch.items()):
        if k not in seeds:
            continue
        w[4] = t
        tok = tokens_by_key.get(k)
        if not pairs.get(k) and not tok:
            w[5] += 1
        age_h = (t - w[3]) / 3600 if not tok or tok.get("age_h") is None else tok["age_h"]
        liq = tok["liq"] if tok else 0
        if w[5] >= 4 or age_h > max_age or (age_h > 6 and liq < 3000) or (age_h > 24 and liq < 10000):
            del watch[k]


# --------------------------------------------------------------------------- market data

def ds_market(http, seeds):
    """Fetch DexScreener pairs for all seeds, grouped per chain (30 per call)."""
    by_chain = {}
    for s in seeds.values():
        by_chain.setdefault(s["chain"], []).append(s["address"])
    pairs_by_key = {}
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            data = http.get_json(f"https://api.dexscreener.com/tokens/v1/{chain}/{','.join(chunk)}")
            if not isinstance(data, list):
                continue
            wanted = {norm_addr(chain, a) for a in chunk}
            for p in data:
                b = norm_addr(chain, (p.get("baseToken") or {}).get("address"))
                if b in wanted:
                    pairs_by_key.setdefault(token_key(chain, b), []).append(p)
    return pairs_by_key


def build_token(seed, pairs, ctx):
    """Merge DexScreener pairs (preferred) or GeckoTerminal pool data into one record."""
    t = {"key": seed["key"], "chain": seed["chain"], "address": seed["address"],
         "symbol": seed.get("symbol") or "?", "name": seed.get("name") or "",
         "sources": sorted(seed["sources"]), "socials": {}, "image": None}
    if pairs:
        pairs = sorted(pairs, key=lambda p: fnum((p.get("liquidity") or {}).get("usd")), reverse=True)
        main = pairs[0]
        bt = main.get("baseToken") or {}
        t["symbol"] = bt.get("symbol") or t["symbol"]
        t["name"] = bt.get("name") or t["name"]
        t["address"] = bt.get("address") or t["address"]
        t["url"] = main.get("url")
        t["dex"] = main.get("dexId")
        t["price"] = fnum(main.get("priceUsd"))
        t["liq"] = sum(fnum((p.get("liquidity") or {}).get("usd")) for p in pairs)
        t["mcap"] = fnum(main.get("marketCap")) or fnum(main.get("fdv"))
        t["vol"] = {k: sum(fnum((p.get("volume") or {}).get(k)) for p in pairs) for k in ("m5", "h1", "h6", "h24")}
        t["buys_h1"] = sum(int(fnum(((p.get("txns") or {}).get("h1") or {}).get("buys"))) for p in pairs)
        t["sells_h1"] = sum(int(fnum(((p.get("txns") or {}).get("h1") or {}).get("sells"))) for p in pairs)
        t["buys_m5"] = sum(int(fnum(((p.get("txns") or {}).get("m5") or {}).get("buys"))) for p in pairs)
        t["sells_m5"] = sum(int(fnum(((p.get("txns") or {}).get("m5") or {}).get("sells"))) for p in pairs)
        t["pc"] = {k: fnum((main.get("priceChange") or {}).get(k)) for k in ("m5", "h1", "h6", "h24")}
        created = [p.get("pairCreatedAt") for p in pairs if p.get("pairCreatedAt")]
        t["created_ms"] = min(created) if created else None
        t["boost_active"] = max(int(fnum((p.get("boosts") or {}).get("active"))) for p in pairs)
        info = main.get("info") or {}
        t["image"] = info.get("imageUrl")
        for s in info.get("socials") or []:
            typ = (s.get("type") or "").lower()
            if typ in ("twitter", "x"):
                t["socials"].setdefault("x", s.get("url"))
            elif typ == "telegram":
                t["socials"].setdefault("telegram", s.get("url"))
            elif typ and s.get("url"):
                t["socials"].setdefault(typ, s.get("url"))
        for w in info.get("websites") or []:
            if w.get("url"):
                t["socials"].setdefault("website", w["url"])
                break
    elif seed.get("gt"):
        g = seed["gt"]
        t.update({"price": None, "liq": g["liq"], "mcap": g["mcap"], "vol": g["vol"],
                  "buys_h1": g["buys_h1"], "sells_h1": g["sells_h1"], "buys_m5": 0, "sells_m5": 0,
                  "pc": g["pc"], "created_ms": g["created_ms"], "boost_active": 0, "dex": None,
                  "url": f"https://www.geckoterminal.com/{seed['chain']}/pools/{g['pool'].split('_', 1)[-1]}"})
    else:
        return None
    if seed.get("gt"):
        t["buyers_h1"] = seed["gt"].get("buyers_h1")
    t["age_h"] = hours_since_ms(t.get("created_ms"))
    t["trending"] = t["key"] in ctx["gt_trending"]
    t["cto"] = t["key"] in ctx["cto"]
    t["profile"] = t["key"] in ctx["profile"] or bool(t["socials"])
    t["boosts"] = max(ctx["boost"].get(t["key"], 0), t.get("boost_active", 0))
    return t


def prefilter(t, cfg):
    """Cheap market filters before spending API calls on safety. Returns reason or None."""
    if (t["symbol"] or "").upper() in STABLE_OR_MAJOR:
        return "major/stable"
    if t["age_h"] is None:
        return "unknown age"
    if t["age_h"] > cfg["runner_max_age_hours"]:
        return "too old"
    if t["liq"] < cfg["min_liquidity_usd"]:
        return "low liquidity"
    if not (cfg["min_mcap_usd"] <= t["mcap"] <= cfg["max_mcap_usd"]):
        return "market cap out of range"
    if t["buys_h1"] + t["sells_h1"] < cfg["min_txns_1h"]:
        return "few trades"
    if t["vol"].get("h1", 0) < cfg["min_volume_1h_usd"]:
        return "low volume"
    if t["pc"].get("h1", 0) < -50 or t["pc"].get("h6", 0) < -75:
        return "dumping"
    if t["liq"] > 0 and t["mcap"] / t["liq"] > 400:
        return "liquidity too thin for market cap"
    return None


# --------------------------------------------------------------------------- safety

def same_size_cluster_pct(pcts, tolerance=0.03, min_group=3):
    """Bundle footprint: >=3 wallets holding (almost) the same amount. Returns their total %."""
    vals = sorted([p for p in pcts if p >= 0.2], reverse=True)
    total, i = 0.0, 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and abs(vals[j + 1] - vals[i]) <= vals[i] * tolerance:
            j += 1
        if j - i + 1 >= min_group:
            total += sum(vals[i:j + 1])
        i = j + 1
    return round(total, 2)


def safety_solana(http, t, cfg):
    rc = http.get_json(f"https://api.rugcheck.xyz/v1/tokens/{t['address']}/report", timeout=25)
    if not rc or "topHolders" not in rc:
        return None
    ka = rc.get("knownAccounts") or {}
    pool_accounts = set()
    lp_locked = []
    for m in rc.get("markets") or []:
        for f in ("liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount"):
            v = m.get(f)
            if isinstance(v, dict):
                v = v.get("pubkey")
            if isinstance(v, str) and v:
                pool_accounts.add(v)
        lp = m.get("lp") or {}
        if lp.get("lpLockedPct") is not None:
            lp_locked.append((fnum(lp.get("lpLockedUSD")), fnum(lp.get("lpLockedPct"))))

    def is_pool(h):
        for a in (h.get("address"), h.get("owner")):
            if a in pool_accounts or (ka.get(a) or {}).get("type") in ("AMM", "LOCKER"):
                return True
        return False

    holders = [h for h in rc.get("topHolders") or [] if not is_pool(h)]
    pcts = [fnum(h.get("pct")) for h in holders]
    supply = fnum((rc.get("token") or {}).get("supply"))
    creator_pct = fnum(rc.get("creatorBalance")) / supply * 100 if supply else 0.0
    insider_pct = sum(fnum(h.get("pct")) for h in holders if h.get("insider"))
    networks = rc.get("insiderNetworks") or []
    net_pct = 0.0
    for n in networks:
        net_pct = max(net_pct, fnum(n.get("tokenAmount")) / supply * 100 if supply else 0.0)
    risks = rc.get("risks") or []
    m = {
        "top10_pct": round(sum(sorted(pcts, reverse=True)[:10]), 2),
        "max_holder_pct": round(max(pcts) if pcts else 0, 2),
        "creator_pct": round(creator_pct, 2),
        "insider_pct": round(max(insider_pct, net_pct), 2),
        "insider_networks": int(fnum(rc.get("graphInsidersDetected"))) or len(networks),
        "cluster_pct": same_size_cluster_pct(pcts),
        "holders": int(fnum(rc.get("totalHolders"))),
        "mint_authority": bool(rc.get("mintAuthority")),
        "freeze_authority": bool(rc.get("freezeAuthority")),
        "transfer_fee_pct": fnum((rc.get("transferFee") or {}).get("pct")),
        "lp_locked_pct": round(max(lp_locked, default=(0, -1))[1], 1) if lp_locked else None,
        "rugged": bool(rc.get("rugged")),
        "rugcheck_score": int(fnum(rc.get("score_normalised"))),
        "risks": [{"name": r.get("name"), "level": r.get("level")} for r in risks][:12],
        "launchpad": ((rc.get("launchpad") or {}).get("name")),
        "source": "RugCheck",
    }
    # Bundle check (best effort - service is often down)
    tb = http.get_json(f"https://trench.bot/api/bundle/bundle_advanced/{t['address']}", timeout=15, retries=0)
    if isinstance(tb, dict):
        held = fnum(tb.get("total_holding_percentage"), None) if tb.get("total_holding_percentage") is not None else None
        if held is not None:
            m["bundle_pct"] = round(held, 2)
            m["bundles"] = int(fnum(tb.get("total_bundles")))
    return m


def safety_goplus(http, t, cfg):
    cid = GOPLUS_CHAIN.get(t["chain"])
    if not cid:
        return None
    addr = norm_addr(t["chain"], t["address"])
    data = http.get_json(f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={addr}", timeout=25)
    res = (data or {}).get("result") or {}
    r = res.get(addr) or (next(iter(res.values())) if res else None)
    if not r:
        return None
    pools = set()
    for d in r.get("dex") or []:
        for f in ("pair", "pool_manager"):
            if d.get(f):
                pools.add(norm_addr(t["chain"], d[f]))
    holders, contract_pct = [], 0.0
    for h in r.get("holders") or []:
        a = norm_addr(t["chain"], h.get("address"))
        if a in DEAD_ADDRESSES or a in pools or str(h.get("is_locked")) == "1":
            continue
        if str(h.get("is_contract")) == "1":  # pools, lockers, vesting, launchpad curves
            contract_pct += fnum(h.get("percent")) * 100
            continue
        tag = (h.get("tag") or "").lower()
        if any(w in tag for w in ("lock", "pool", "pair", "burn", "dead")):
            continue
        holders.append(fnum(h.get("percent")) * 100)
    # GoPlus sometimes lists one wallet with ~100% next to other holders (impossible) -> bad data
    suspect = sum(holders) > 100.5 and any(p >= 95 for p in holders)
    if suspect:
        holders = [p for p in holders if p < 95]
    lp_safe = 0.0
    for h in r.get("lp_holders") or []:
        a = norm_addr(t["chain"], h.get("address"))
        if a in DEAD_ADDRESSES or str(h.get("is_locked")) == "1" or "lock" in (h.get("tag") or "").lower():
            lp_safe += fnum(h.get("percent")) * 100
    dex_types = {(d.get("liquidity_type") or "").lower() for d in r.get("dex") or []}
    launchpad = (r.get("launchpad_token") or {})
    owner = norm_addr(t["chain"], r.get("owner_address") or "")
    renounced = owner in ("", *DEAD_ADDRESSES)
    flag = lambda k: str(r.get(k, "0")) == "1"
    return {
        "top10_pct": round(sum(sorted(holders, reverse=True)[:10]), 2),
        "max_holder_pct": round(max(holders) if holders else 0, 2),
        "creator_pct": round(fnum(r.get("creator_percent")) * 100, 2),
        "owner_pct": round(fnum(r.get("owner_percent")) * 100, 2),
        "cluster_pct": same_size_cluster_pct(holders),
        "insider_pct": 0.0,
        "contract_pct": round(contract_pct, 1),
        "holder_data_suspect": suspect,
        "holders": int(fnum(r.get("holder_count"))),
        "honeypot": flag("is_honeypot") or flag("cannot_buy") or flag("cannot_sell_all"),
        "buy_tax_pct": round(fnum(r.get("buy_tax")) * 100, 1),
        "sell_tax_pct": round(fnum(r.get("sell_tax")) * 100, 1),
        "mintable": flag("is_mintable") and not renounced,
        "owner_can_change_balance": flag("owner_change_balance"),
        "hidden_owner": flag("hidden_owner"),
        "take_back_ownership": flag("can_take_back_ownership"),
        "selfdestruct": flag("selfdestruct"),
        "pausable": flag("transfer_pausable") and not renounced,
        "blacklist": flag("is_blacklisted") and not renounced,
        "tax_modifiable": flag("slippage_modifiable") and not renounced,
        "proxy": flag("is_proxy"),
        "open_source": flag("is_open_source"),
        "honeypot_same_creator": flag("honeypot_with_same_creator"),
        "renounced": renounced,
        "lp_locked_pct": round(lp_safe, 1),
        "lp_v2_only": bool(dex_types) and all("v2" in d or d == "" for d in dex_types),
        "launchpad": launchpad.get("launchpad_name") if str(launchpad.get("is_launchpad_token")) == "1" else None,
        "source": "GoPlus",
    }


def judge_safety(m, t, cfg):
    """Turn safety metrics into hard rejects, warnings and a score penalty."""
    rejects, warns, penalty = [], [], 0
    if m is None:
        return {"verified": False, "rejects": [], "warnings": ["Safety not checked (chain or API unavailable)"], "penalty": 0}
    if m.get("honeypot"):
        rejects.append("Honeypot: you may not be able to sell")
    if m.get("rugged"):
        rejects.append("RugCheck marks it as rugged")
    tax = max(m.get("buy_tax_pct", 0), m.get("sell_tax_pct", 0), m.get("transfer_fee_pct", 0))
    if tax > cfg["max_tax_pct"]:
        rejects.append(f"Tax {tax:.0f}%")
    elif tax > 3:
        warns.append(f"Tax {tax:.0f}%"); penalty += 4
    if m.get("mint_authority"):
        rejects.append("Mint authority active (dev can print tokens)")
    if m.get("freeze_authority"):
        rejects.append("Freeze authority active (dev can freeze wallets)")
    for k, txt in (("mintable", "Owner can mint"), ("owner_can_change_balance", "Owner can change balances"),
                   ("hidden_owner", "Hidden owner"), ("take_back_ownership", "Ownership can be reclaimed"),
                   ("selfdestruct", "Self-destruct in contract"), ("honeypot_same_creator", "Creator made honeypots before")):
        if m.get(k):
            rejects.append(txt)
    for k, txt, p in (("pausable", "Trading can be paused", 8), ("blacklist", "Blacklist function", 5),
                      ("tax_modifiable", "Tax can be changed", 8), ("proxy", "Upgradeable proxy contract", 6)):
        if m.get(k):
            warns.append(txt); penalty += p
    if m.get("source") == "GoPlus" and not m.get("open_source", True):
        rejects.append("Contract source not verified")
    if m.get("lp_v2_only") and not m.get("launchpad") and m.get("lp_locked_pct", 100) < 50:
        rejects.append(f"Liquidity not locked/burned ({m.get('lp_locked_pct', 0):.0f}% safe)")
    if m.get("source") == "RugCheck" and m.get("lp_locked_pct") is not None and 0 <= m["lp_locked_pct"] < 50 \
            and not m.get("launchpad"):
        warns.append(f"Only {m['lp_locked_pct']:.0f}% of liquidity locked/burned"); penalty += 10

    if m.get("holder_data_suspect"):
        warns.append("Holder data inconsistent - check bubblemap yourself"); penalty += 10
    top10, single = m.get("top10_pct", 0), m.get("max_holder_pct", 0)
    if top10 > cfg["max_top10_pct"]:
        rejects.append(f"Top 10 wallets hold {top10:.0f}%")
    elif top10 > 25:
        warns.append(f"Top 10 wallets hold {top10:.0f}%"); penalty += 6
    if single > cfg["max_single_holder_pct"]:
        rejects.append(f"One wallet holds {single:.0f}%")
    creator = max(m.get("creator_pct", 0), m.get("owner_pct", 0))
    if creator > cfg["max_creator_pct"]:
        rejects.append(f"Dev holds {creator:.0f}%")
    elif creator > 3:
        warns.append(f"Dev holds {creator:.1f}%"); penalty += 5
    ins = m.get("insider_pct", 0)
    if ins > cfg["max_insider_pct"]:
        rejects.append(f"Insider cluster holds {ins:.0f}% (bubblemap)")
    elif ins > 8 or m.get("insider_networks", 0) >= 2:
        warns.append(f"Linked insider wallets ({ins:.0f}%)"); penalty += 8
    bundle = m.get("bundle_pct")
    if bundle is not None:
        if bundle > cfg["max_bundle_pct"]:
            rejects.append(f"Bundled wallets still hold {bundle:.0f}%")
        elif bundle > 8:
            warns.append(f"Bundles hold {bundle:.0f}%"); penalty += 8
    cl = m.get("cluster_pct", 0)
    if cl > cfg["max_bundle_pct"]:
        rejects.append(f"Same-size wallet cluster holds {cl:.0f}% (likely bundle)")
    elif cl > 8:
        warns.append(f"Same-size wallets hold {cl:.0f}% (possible bundle)"); penalty += 6
    for r in m.get("risks") or []:
        name = r.get("name") or ""
        low = name.lower()
        if "lp unlocked" in low:
            if m.get("launchpad") or (m.get("lp_locked_pct") or 0) >= 50:
                warns.append(f"RugCheck: {name}"); penalty += 6
            else:
                rejects.append(f"RugCheck: {name}")
        elif r.get("level") == "danger" and not any(x in low for x in ("low liquidity", "top 10", "single holder", "mint", "freeze")):
            rejects.append(f"RugCheck: {name}")
        elif r.get("level") == "warn" and "copycat" in name.lower():
            warns.append("Copycat ticker"); penalty += 6
    return {"verified": True, "rejects": rejects, "warnings": warns, "penalty": penalty}


# --------------------------------------------------------------------------- social

def x_profile(http, state, handle):
    if not handle:
        return None
    hk = handle.lower()
    c = state["x_cache"].get(hk)
    if c and now() - c["ts"] < 6 * 3600:
        return c["data"]
    d = http.get_json(f"https://api.fxtwitter.com/{handle}", timeout=15, retries=0)
    u = (d or {}).get("user")
    data = None
    if u:
        age_days = None
        try:
            joined = datetime.strptime(u.get("joined", ""), "%a %b %d %H:%M:%S %z %Y")
            age_days = (datetime.now(timezone.utc) - joined).days
        except ValueError:
            pass
        data = {"followers": int(fnum(u.get("followers"))), "tweets": int(fnum(u.get("tweets"))),
                "age_days": age_days}
    state["x_cache"][hk] = {"ts": now(), "data": data}
    return data


def biz_corpus(http, ctx):
    if now() - ctx.get("biz_ts", 0) < 300:
        return ctx.get("biz", "")
    pages = http.get_json("https://a.4cdn.org/biz/catalog.json", timeout=20, retries=0) or []
    parts = []
    for page in pages:
        for th in page.get("threads", []):
            parts.append(th.get("sub") or "")
            parts.append(th.get("com") or "")
            for r in th.get("last_replies") or []:
                parts.append(r.get("com") or "")
    ctx["biz"] = re.sub(r"<[^>]+>", " ", " ".join(parts)).lower()
    ctx["biz_ts"] = now()
    return ctx["biz"]


COMMON_WORDS = {"the", "cat", "dog", "ai", "sol", "eth", "moon", "pump", "coin", "token", "meme", "real",
                "frog", "pepe", "trump", "usa", "one", "god", "cto", "buy", "sell", "dev", "gm", "lol"}


def biz_mentions(corpus, t):
    if not corpus:
        return 0
    n = corpus.count(t["address"].lower()) * 3
    sym = (t["symbol"] or "").lower()
    if len(sym) >= 3:
        n += len(re.findall(r"\$" + re.escape(sym) + r"\b", corpus))
        if sym not in COMMON_WORDS and len(sym) >= 4:
            n += len(re.findall(r"\b" + re.escape(sym) + r"\b", corpus)) // 2
    return n


def coingecko_trending(http, ctx):
    if now() - ctx.get("cg_ts", 0) < 900:
        return ctx.get("cg", set())
    d = http.get_json("https://api.coingecko.com/api/v3/search/trending", timeout=20, retries=0) or {}
    ctx["cg"] = {(c.get("item") or {}).get("symbol", "").upper() for c in d.get("coins", [])}
    ctx["cg_ts"] = now()
    return ctx["cg"]


# --------------------------------------------------------------------------- scoring

def record_history(state, t, holders):
    """One market snapshot per ~10 minutes for every active coin (keeps 24h)."""
    h = state["history"].setdefault(t["key"], {"first": now(), "peak": 0, "peak_ts": now(),
                                               "trend_hits": 0, "snaps": []})
    if t["mcap"] > h["peak"]:
        h["peak"], h["peak_ts"] = t["mcap"], now()
    snaps = h["snaps"]
    if snaps and now() - snaps[-1][0] < 600:
        if holders and not snaps[-1][5]:
            snaps[-1][5] = holders
        return h
    if t.get("trending"):
        h["trend_hits"] += 1
    snaps.append([now(), round(t["mcap"]), round(t["liq"]), round(t["vol"].get("h1", 0)), t["buys_h1"], holders or 0])
    del snaps[:-144]
    return h


def snap_ago(hist, hours):
    """Oldest snapshot that is at most `hours` old (None if history is shorter than half of that)."""
    if not hist or not hist["snaps"]:
        return None
    target = now() - hours * 3600
    best = None
    for s in hist["snaps"]:
        if s[0] >= target:
            best = s
            break
    if best is None or now() - best[0] < hours * 3600 * 0.5:
        return None
    return best


def holder_growth_per_hour(hist, holders):
    if not hist or not holders:
        return None
    for s in hist["snaps"]:
        dt = (now() - s[0]) / 3600
        if s[5] and dt >= 0.25:
            return (holders - s[5]) / dt
    return None


def staying_power(t, hist, holders, cfg):
    """Has this coin survived and is it still alive? Returns points (0-25), alert gates, reasons."""
    gates, why, pts = [], [], 0.0
    age = t["age_h"] or 0
    observed = (now() - hist["first"]) / 3600 if hist else 0
    peak = max(hist["peak"] if hist else 0, t["mcap"])
    drawdown = (1 - t["mcap"] / peak) * 100 if peak else 0
    vol = t["vol"]
    pace_h1 = vol.get("h1", 0) / max(vol.get("h6", 0) / 6, 1)
    s3 = snap_ago(hist, 3)
    s6 = snap_ago(hist, 6)

    def gate(ok, text):
        gates.append({"ok": bool(ok), "text": text})

    gate(age >= cfg["min_alert_age_hours"], f"Survived {fmt_age(age)} (needs {cfg['min_alert_age_hours']}h+)")
    gate(observed >= cfg["min_observed_hours"] and hist and len(hist["snaps"]) >= 6,
         f"Watched by GemRadar for {observed:.1f}h (needs {cfg['min_observed_hours']}h+)")
    gate(drawdown <= cfg["max_drawdown_pct"], f"{drawdown:.0f}% below its peak (max {cfg['max_drawdown_pct']}%)")
    gate(pace_h1 >= 0.5, f"Trading still active: last hour = {pace_h1:.1f}x the 6h average")
    if s3:
        liq_ratio = t["liq"] / max(s3[2], 1)
        gate(liq_ratio >= 0.75, f"Liquidity {(liq_ratio - 1) * 100:+.0f}% in 3h")
        mc_ratio = t["mcap"] / max(s3[1], 1)
        gate(mc_ratio >= 0.7, f"Market cap {(mc_ratio - 1) * 100:+.0f}% in 3h")
        if s3[5] and holders:
            hg = (holders - s3[5]) / s3[5] * 100
            gate(hg >= -1, f"Holders {hg:+.0f}% in 3h")
    growth = holder_growth_per_hour(hist, holders)
    if growth is not None and t["buys_h1"] and growth > max(t["buys_h1"], 50) * 1.2:
        gate(False, f"Holders grow faster than buys (+{growth:.0f}/h vs {t['buys_h1']} buys): bot/airdrop wallets")

    # points
    pts += 6 * logscale(max(age, 1), 6, 168)
    pts += 5 * clamp(1 - drawdown / 50)
    if s6:
        r = t["mcap"] / max(s6[1], 1)
        pts += 4 * clamp((r - 0.7) / 0.6)
        if r >= 1.2:
            why.append(f"market cap {(r - 1) * 100:+.0f}% in 6h")
    if s3 and s3[5] and holders:
        hg = (holders - s3[5]) / s3[5]
        pts += 5 * clamp(hg / 0.15)
        if hg >= 0.05:
            why.append(f"holders {hg * 100:+.0f}% in 3h")
    if s3:
        pts += 3 * clamp((t["liq"] / max(s3[2], 1) - 0.8) / 0.4)
    h6, h24 = vol.get("h6", 0), vol.get("h24", 0)
    if h24 > 0 and 0.6 <= h6 / max(h24 / 4, 1) <= 3:
        pts += 2  # steady trading, not a single spike
    if age >= 24 and drawdown <= 30:
        why.append(f"holding up after {fmt_age(age)}")
    return round(clamp(pts, 0, 25), 1), gates, why


def buzz_meter(t, hist, social, holders):
    """0-100: how much lasting buying interest there is right now (on-chain + social)."""
    f = 0.0
    vol = t["vol"]
    pace = vol.get("h1", 0) / max(vol.get("h6", 0) / 6, 1)
    f += 20 * clamp((pace - 0.8) / 1.7)                                    # volume accelerating
    ratio = t["buys_h1"] / max(t["sells_h1"], 1)
    f += 15 * clamp((ratio - 1) / 1.0) if ratio <= 4 else 5                # more buyers than sellers
    f += 15 * logscale(max(hist["trend_hits"], 1), 1, 30) if hist else 0  # keeps showing up in trending
    growth = holder_growth_per_hour(hist, holders)
    if growth is not None:
        f += 15 * logscale(max(growth, 1), 5, 300)                         # new holders keep coming
    if social.get("cg_trending"):
        f += 10
    f += 5 * min(social.get("biz") or 0, 3) / 3
    if t.get("boosts"):
        f += 5
    if t.get("cto"):
        f += 5
    xp = social.get("x")
    if xp:
        f += 10 * logscale(xp["followers"], 500, 50_000)
    return round(clamp(f, 0, 100))


def fast_lane_gates(t, hist, holders, fs, gates, cfg):
    """Young coin with real FOMO-app hype: skip the 6h wait, keep every safety check."""
    if not cfg.get("fomo_fast_lane") or not fs:
        return None
    out = []
    hot = fs["buyers_2h"] >= cfg["fomo_fast_min_buyers"] or ((fs["rank"] or 999) <= 20 and fs["buyers_6h"] >= 2)
    out.append({"ok": hot, "text": f"{fs['buyers_2h']} FOMO traders bought in 2h (needs {cfg['fomo_fast_min_buyers']}+)"
                + (f", #{fs['rank']} on FOMO trending" if fs["rank"] else "")})
    out.append({"ok": (t["age_h"] or 0) >= 0.33, "text": f"Age {fmt_age(t['age_h'])} (needs 20m+)"})
    out.append({"ok": fs["sellers_6h"] <= fs["buyers_6h"],
                "text": f"FOMO buyers {fs['buyers_6h']} vs sellers {fs['sellers_6h']}"})
    for g in gates:  # keep the crash and bot-holder checks from staying power
        if "below its peak" in g["text"] or "faster than buys" in g["text"]:
            out.append(g)
    return out


def score_token(t, safety_m, verdict, social, state, cfg):
    """Score 0-100: staying 25, traction 20, FOMO app 15, holders 13, hype 12, momentum 10, upside 5."""
    parts, why = {}, []
    hist = state["history"].get(t["key"])
    liq, mcap = t["liq"], max(t["mcap"], 1)
    vol_h1 = t["vol"].get("h1", 0)
    buys, sells = t["buys_h1"], t["sells_h1"]

    # Traction (22)
    tr = 8 * logscale(liq, 10_000, 250_000)
    tr += 8 * clamp((vol_h1 / mcap) / 0.5)
    tr += 5 * logscale(vol_h1, 5_000, 200_000)
    tr += 6 * logscale(buys + sells, 50, 2_000)
    ratio = buys / max(sells, 1)
    if 1.1 <= ratio <= 3:
        tr += 5; why.append(f"buyers dominate ({ratio:.1f}x more buys)")
    elif 0.9 <= ratio < 1.1 or 3 < ratio <= 5:
        tr += 3
    if t.get("buyers_h1"):
        tr += 3 * logscale(t["buyers_h1"], 30, 900)
    parts["traction"] = round(tr * 20 / 35, 1)

    # Momentum (8)
    pc = t["pc"]
    mo = 6 * clamp(pc.get("h1", 0) / 30) + 5 * clamp(pc.get("h6", 0) / 80)
    if pc.get("m5", 0) > -10:
        mo += 4
    elif pc.get("m5", 0) < -25:
        mo -= 5
    if pc.get("h1", 0) > 1000 or pc.get("h6", 0) > 3000:
        mo -= 6; why.append("already pumped hard")
    momentum_raw = clamp(mo, -10, 15)

    # Holders (15)
    holders = (safety_m or {}).get("holders", 0)
    ho = 8 * logscale(holders, 100, 3_000)
    growth = holder_growth_per_hour(hist, holders)
    if growth is not None:
        ho += 4 * logscale(max(growth, 1), 5, 300)
    top10 = (safety_m or {}).get("top10_pct", 100)
    ho += 3 if top10 < 15 else 1.5 if top10 < 25 else 0
    parts["holders"] = round(ho * 13 / 15, 1)

    # Social / hype (25)
    so = 0.0
    if t["socials"].get("x"):
        so += 3
    if t["socials"].get("website"):
        so += 2
    if t["socials"].get("telegram"):
        so += 2
    xp = social.get("x")
    if xp:
        so += 6 * logscale(xp["followers"], 200, 20_000)
        if xp["followers"] >= 2000:
            why.append(f"X {xp['followers']:,} followers")
    if t["profile"]:
        so += 1
    if t["boosts"]:
        so += 2
    if t["cto"]:
        so += 3; why.append("community takeover")
    if t["trending"]:
        so += 3; why.append("trending on GeckoTerminal")
    if social.get("cg_trending"):
        so += 4; why.append("trending on CoinGecko")
    if social.get("biz"):
        so += min(3, social["biz"]); why.append(f"{social['biz']} mentions on 4chan /biz/")
    parts["social"] = round(min(so, 25) * 12 / 25, 1)

    buzz = buzz_meter(t, hist, social, holders)
    parts["momentum"] = round(momentum_raw * 6 / 15 + buzz / 25, 1)  # price trend 6 + buzz meter 4

    fs = social.get("fomo")
    fp, fwhy = fomo.points(fs)
    parts["fomo"] = round(fp, 1)
    why = fwhy + why

    stay, gates, stay_why = staying_power(t, hist, holders, cfg)
    parts["staying"] = stay
    why = stay_why + why

    # Upside (5): room to grow
    parts["upside"] = 5 if mcap <= 1_000_000 else 3.5 if mcap <= 5_000_000 else 2 if mcap <= 20_000_000 else 0.5

    total = sum(parts.values()) - verdict["penalty"]
    if not verdict["verified"]:
        total = min(total, 60)
    if verdict["rejects"]:
        total = min(total, 40)
    if liq >= 50_000:
        why.insert(0, f"${liq/1000:.0f}k liquidity")
    if holders >= 500:
        why.append(f"{holders:,} holders")
    fast = fast_lane_gates(t, hist, holders, fs, gates, cfg)
    score_token.last = {"gates": gates, "buzz": buzz, "fast_gates": fast}
    return round(clamp(total, 0, 100), 1), parts, why[:6]


# --------------------------------------------------------------------------- alerts

def fmt_usd(v):
    if v is None:
        return "?"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}k"
    return f"${v:.0f}"


def fmt_age(h):
    if h is None:
        return "?"
    if h < 1:
        return f"{h*60:.0f}m"
    if h < 48:
        return f"{h:.1f}h"
    return f"{h/24:.0f}d"


def links_for(t):
    chain, addr = t["chain"], t["address"]
    links = {"chart": t.get("url") or f"https://dexscreener.com/{chain}/{addr}",
             "x_search": f"https://x.com/search?q={urllib.parse.quote('$' + (t['symbol'] or '') + ' OR ' + addr)}&f=live"}
    bm = BUBBLEMAPS_CHAIN.get(chain)
    if bm:
        links["bubblemap"] = f"https://v2.bubblemaps.io/map?address={addr}&chain={bm}"
    if chain == "solana":
        links["rugcheck"] = f"https://rugcheck.xyz/tokens/{addr}"
    elif chain in GOPLUS_CHAIN:
        links["goplus"] = f"https://gopluslabs.io/token-security/{GOPLUS_CHAIN[chain]}/{addr}"
    for k in ("x", "telegram", "website"):
        if t["socials"].get(k):
            links[k] = t["socials"][k]
    return links


def push(title, message, click=None, actions=None, tags=None, priority=4):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or os.environ.get("DRY_RUN") == "1":
        log(f"  [push skipped] {title}")
        return False
    payload = {"topic": topic, "title": title, "message": message, "tags": tags or [], "priority": priority}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions[:3]
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20, context=SSL_CTX).read()
        return True
    except Exception as e:
        log(f"  push failed: {e}")
        return False


def alerts_today(state):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for a in state["alerts"] if a.get("day") == day)


def in_quiet_hours(cfg):
    q = cfg.get("quiet_hours_utc") or []
    return datetime.now(timezone.utc).hour in q


def maybe_alert(entry, state, cfg, cycle_alerts):
    t = entry
    if entry["key"] in state["alerted"]:
        return False
    if entry["rejects"] or not entry["verified"]:
        return False
    normal = entry["score"] >= cfg["alert_min_score"] and all(g["ok"] for g in entry.get("gates") or [{"ok": False}])
    fast = (not normal and entry.get("fast_gates") and entry["score"] >= cfg["fomo_fast_min_score"]
            and all(g["ok"] for g in entry["fast_gates"]))
    if not (normal or fast):
        return False
    if cycle_alerts >= cfg["max_alerts_per_cycle"]:
        return False
    if alerts_today(state) >= NTFY_DAILY_CEILING:
        return False  # hard free-tier ceiling, even for exceptional scores
    if (alerts_today(state) >= cfg["max_alerts_per_day"] or in_quiet_hours(cfg)) and entry["score"] < cfg["exceptional_score"]:
        return False
    kind = "fomo" if fast else "gem" if (t["age_h"] or 0) <= cfg["gem_max_age_hours"] else "survivor"
    icon, label = {"fomo": ("🔥", "FOMO hype"), "gem": ("💎", "Gem"), "survivor": ("🏆", "Survivor")}[kind]
    title = f"{icon} {label}: ${t['symbol']} ({t['chain']}) score {entry['score']:.0f}"
    lines = [
        f"MC {fmt_usd(t['mcap'])} · Liq {fmt_usd(t['liq'])} · Age {fmt_age(t['age_h'])}",
        f"1h: {t['pc'].get('h1', 0):+.0f}% · vol {fmt_usd(t['vol'].get('h1'))} · {t['buys_h1']} buys / {t['sells_h1']} sells",
        f"Buzz {entry.get('buzz', 0)}/100 · staying power {entry['parts'].get('staying', 0):.0f}/25",
    ]
    fs = entry.get("fomo")
    if fs:
        line = f"FOMO app: {fs['buyers_6h']} traders bought (6h)"
        if fs["top_buyers"]:
            line += ": " + ", ".join(fs["top_buyers"][:3])
        if fs["rank"]:
            line += f" · #{fs['rank']} trending"
        lines.append(line)
    if fast:
        lines.append("Young coin - early FOMO alert, higher risk")
    m = entry.get("safety") or {}
    safety_line = f"Safe ✓ top10 {m.get('top10_pct', 0):.0f}% · dev {max(m.get('creator_pct', 0), m.get('owner_pct', 0)):.1f}%"
    if m.get("holders"):
        safety_line += f" · {m['holders']:,} holders"
    lines.append(safety_line)
    if entry["why"]:
        lines.append("Why: " + ", ".join(entry["why"][:4]))
    if entry["warnings"]:
        lines.append("⚠ " + "; ".join(entry["warnings"][:2]))
    lines.append("Not financial advice - memecoins can go to zero.")
    links = entry["links"]
    actions = [{"action": "view", "label": "Chart", "url": links["chart"]}]
    if links.get("bubblemap"):
        actions.append({"action": "view", "label": "Bubblemap", "url": links["bubblemap"]})
    actions.append({"action": "view", "label": "X posts", "url": links["x_search"]})
    sent = push(title, "\n".join(lines), click=links["chart"], actions=actions,
                tags=[{"fomo": "fire", "gem": "gem", "survivor": "trophy"}[kind]], priority=5 if entry["score"] >= cfg["exceptional_score"] else 4)
    rec = {"key": t["key"], "ts": now(), "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
           "kind": kind, "symbol": t["symbol"], "name": t["name"], "chain": t["chain"], "address": t["address"],
           "score": entry["score"], "mcap": t["mcap"], "price": t.get("price"), "liq": t["liq"],
           "age_h": t["age_h"], "why": entry["why"], "warnings": entry["warnings"], "links": links,
           "image": t.get("image"), "sent": sent,
           "vol": t.get("vol"), "pc": t.get("pc"), "buys_h1": t.get("buys_h1"), "sells_h1": t.get("sells_h1"),
           "safety": entry.get("safety"), "parts": entry.get("parts"), "x": entry.get("x"),
           "socials": t.get("socials"), "dex": t.get("dex"), "url": t.get("url"), "biz": entry.get("biz"),
           "trending": t.get("trending"), "cto": t.get("cto"), "boosts": t.get("boosts"),
           "cg_trending": entry.get("cg_trending"), "fast_gates": entry.get("fast_gates"), "fomo": entry.get("fomo"), "buzz": entry.get("buzz"),
           "gates": entry.get("fast_gates") if fast else entry.get("gates")}
    state["alerted"][t["key"]] = {"ts": rec["ts"], "score": entry["score"], "mcap": t["mcap"]}
    state["alerts"].insert(0, rec)
    log(f"  ALERT {title}")
    return True


# --------------------------------------------------------------------------- cycle

def scan_cycle(http, state, cfg, ctx):
    t0 = now()
    ctx["gt_trending"], ctx["cto"], ctx["profile"], ctx["boost"] = set(), set(), set(), {}
    seeds = discover(http, cfg, ctx, state)
    pairs = ds_market(http, seeds)
    tokens, skipped, built = [], {}, {}
    for k, seed in seeds.items():
        t = build_token(seed, pairs.get(k), ctx)
        if not t:
            skipped["no market data"] = skipped.get("no market data", 0) + 1
            continue
        built[k] = t
        reason = prefilter(t, cfg)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        tokens.append(t)

    # Rank by a quick pre-score so deep checks go to the most promising tokens first.
    def quick(t):
        dummy = {"verified": True, "rejects": [], "warnings": [], "penalty": 0}
        return score_token(t, {}, dummy, {}, state, cfg)[0]
    tokens.sort(key=lambda t: (bool(fomo.stats(state, t["key"])), quick(t)), reverse=True)

    update_watch(state, seeds, pairs, {k: t for k, t in built.items() if k in {x["key"] for x in tokens}}, cfg)
    for t in tokens:
        cached = state["safety_cache"].get(t["key"])
        record_history(state, t, ((cached or {}).get("data") or {}).get("holders"))
    corpus = biz_corpus(http, ctx)
    cg = coingecko_trending(http, ctx)
    budget = {"deep": min(int(cfg["deep_checks_per_cycle"]), MAX_DEEP_CHECKS), "alerts": 4}
    results = []
    for t in tokens:
        entry = evaluate(http, state, cfg, t, corpus, cg, budget, "deep")
        if entry:
            results.append(entry)
    rescore_alerts(http, state, cfg, built, results, corpus, cg, budget)

    results.sort(key=lambda e: e["score"], reverse=True)
    sent = 0
    for e in results:
        if maybe_alert(e, state, cfg, sent):
            sent += 1
    merge_board(state, results)
    return finish_cycle(state, t0, seeds, tokens, results, skipped, sent)


def evaluate(http, state, cfg, t, corpus, cg, budget, pool):
    """Safety + social + score for one token. None if its safety check has to wait for a later cycle."""
    cached = state["safety_cache"].get(t["key"])
    ttl = 1200 if (t["age_h"] or 0) < 24 else 3600
    if cached and now() - cached["ts"] < ttl:
        m = cached["data"]
    elif budget[pool] > 0:
        budget[pool] -= 1
        m = safety_solana(http, t, cfg) if t["chain"] == "solana" else safety_goplus(http, t, cfg)
        state["safety_cache"][t["key"]] = {"ts": now(), "data": m}
    elif cached:
        m = cached["data"]  # a slightly older check is fine for the dashboard
    else:
        return None
    verdict = judge_safety(m, t, cfg)
    social = {"cg_trending": (t["symbol"] or "").upper() in cg, "biz": biz_mentions(corpus, t),
              "fomo": fomo.stats(state, t["key"])}
    if not verdict["rejects"]:
        social["x"] = x_profile(http, state, parse_x_handle(t["socials"].get("x")))
    record_history(state, t, (m or {}).get("holders"))
    score, parts, why = score_token(t, m, verdict, social, state, cfg)
    extra = score_token.last
    return {**{k: t[k] for k in ("key", "chain", "address", "symbol", "name", "url", "price", "liq", "mcap",
                                 "vol", "pc", "buys_h1", "sells_h1", "age_h", "socials", "trending", "cto",
                                 "boosts", "image", "dex", "sources")},
            "score": score, "parts": parts, "why": why, "safety": m, "verified": verdict["verified"],
            "rejects": verdict["rejects"], "warnings": verdict["warnings"], "links": links_for(t),
            "x": social.get("x"), "biz": social.get("biz"), "cg_trending": social.get("cg_trending"),
            "gates": extra["gates"], "fast_gates": extra["fast_gates"], "buzz": extra["buzz"],
            "fomo": social.get("fomo"), "checked": now()}


NOW_FIELDS = ("score", "parts", "why", "safety", "verified", "rejects", "warnings", "gates", "fast_gates", "buzz",
              "fomo", "x", "biz", "cg_trending", "trending", "cto", "boosts", "socials", "checked")


def rescore_alerts(http, state, cfg, built, results, corpus, cg, budget):
    """Keep score bars, FOMO, buzz and checks current for every coin still in the alerts list."""
    scored = {e["key"]: e for e in results}
    for a in state.get("alerts") or []:
        e = scored.get(a["key"])
        if e is None and a["key"] in built:
            e = evaluate(http, state, cfg, built[a["key"]], corpus, cg, budget, "alerts")
        if e:
            a["now"] = {k: e.get(k) for k in NOW_FIELDS}


def finish_cycle(state, t0, seeds, tokens, results, skipped, sent):
    st = state["stats"]
    st["last_cycle"] = {"ts": now(), "seconds": round(now() - t0, 1), "seeds": len(seeds),
                        "passed_market_filter": len(tokens), "deep_checked": len(results), "alerts": sent,
                        "skipped": skipped, "watching": len(state["watch"]),
                        "chains": sorted({s["chain"] for s in seeds.values()})}
    st["cycles"] = st.get("cycles", 0) + 1
    log(f"cycle: {len(seeds)} seen ({len(state['watch'])} in watch pool), {len(tokens)} pass market filter, {len(results)} scored, {sent} alerts "
        f"({now() - t0:.0f}s)")
    return results


def merge_board(state, results):
    t = now()
    board = {e["key"]: e for e in state.get("board", []) if t - e.get("checked", 0) < 6 * 3600}
    rejected = {e["key"]: e for e in state.get("rejected", []) if t - e.get("checked", 0) < 6 * 3600}
    for e in results:
        if e["rejects"]:
            rejected[e["key"]] = e
            board.pop(e["key"], None)
        else:
            board[e["key"]] = e
            rejected.pop(e["key"], None)
    state["board"] = sorted(board.values(), key=lambda e: e["score"], reverse=True)[:80]
    state["rejected"] = sorted(rejected.values(), key=lambda e: e.get("checked", 0), reverse=True)[:40]


# --------------------------------------------------------------------------- site

def refresh_alerts(http, state):
    """Every ~10 minutes: update alerted coins' market cap; drop alerts for coins that went to ~zero.
    Dropped coins stay in `alerted`, so they are never alerted again."""
    if now() - state.get("alerts_refreshed", 0) < 600 or not state["alerts"]:
        return
    state["alerts_refreshed"] = now()
    by_chain = {}
    for a in state["alerts"]:
        by_chain.setdefault(a["chain"], []).append(a)
    best = {}
    for chain, items in by_chain.items():
        addrs = [a["address"] for a in items]
        for i in range(0, len(addrs), 30):
            data = http.get_json(f"https://api.dexscreener.com/tokens/v1/{chain}/{','.join(addrs[i:i + 30])}")
            if not isinstance(data, list):
                continue
            for p in data:
                k = token_key(chain, (p.get("baseToken") or {}).get("address"))
                if k not in best or fnum((p.get("liquidity") or {}).get("usd")) > fnum((best[k].get("liquidity") or {}).get("usd")):
                    best[k] = p
    keep = []
    for a in state["alerts"]:
        p = best.get(a["key"])
        if p is None:
            keep.append(a)  # no data this time: keep, try again later
            continue
        mc = fnum(p.get("marketCap")) or fnum(p.get("fdv"))
        liq = fnum((p.get("liquidity") or {}).get("usd"))
        a["now_mcap"], a["now_liq"] = mc, liq
        tx = p.get("txns") or {}
        created = p.get("pairCreatedAt")
        a["live"] = {"ts": now(), "mcap": mc, "liq": liq, "price": fnum(p.get("priceUsd")),
                     "vol": {k: fnum((p.get("volume") or {}).get(k)) for k in ("m5", "h1", "h6", "h24")},
                     "pc": {k: fnum((p.get("priceChange") or {}).get(k)) for k in ("m5", "h1", "h6", "h24")},
                     "buys_h1": int(fnum((tx.get("h1") or {}).get("buys"))),
                     "sells_h1": int(fnum((tx.get("h1") or {}).get("sells"))),
                     "age_h": hours_since_ms(created) if created else None}
        a["peak_mcap"] = max(a.get("peak_mcap") or 0, mc, a.get("mcap") or 0)
        if (a.get("mcap") and mc < a["mcap"] * 0.10) or liq < 1000:
            log(f"  removed crashed alert ${a['symbol']} (MC {fmt_usd(mc)} vs {fmt_usd(a.get('mcap'))} at alert)")
            state.setdefault("stats", {}).setdefault("removed_crashed", 0)
            state["stats"]["removed_crashed"] += 1
            continue
        keep.append(a)
    state["alerts"] = keep


def heartbeat(state):
    """Change state/alerts.json once a week so GitHub never pauses the schedule for inactivity (60 days)."""
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if state.get("heartbeat") != week:
        state["heartbeat"] = week


def write_site(state, cfg):
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    for name in WEB_FILES:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, SITE_DIR / name)
    data = {"updated": now(), "config": {k: cfg[k] for k in ("alert_min_score", "max_alerts_per_day",
                                                             "gem_max_age_hours", "runner_max_age_hours",
                                                             "min_liquidity_usd")},
            "stats": state["stats"], "alerts": state["alerts"][:60], "board": state["board"],
            "rejected": state["rejected"], "alerts_today": alerts_today(state)}
    (SITE_DIR / "data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


# --------------------------------------------------------------------------- main

def main():
    cfg = load_config()
    state = fresh_state(load_state())
    run_s = int(os.environ.get("RUN_SECONDS", "660"))
    cycle_s = int(os.environ.get("CYCLE_SECONDS", "90"))
    http = Http()
    ctx = {}
    if not state.get("welcomed") and os.environ.get("NTFY_TOPIC"):
        if push("✅ GemRadar is live", "You'll get an alert only when a new token scores "
                f"{cfg['alert_min_score']}+ and passes the scam checks (max {cfg['max_alerts_per_day']}/day).",
                click=os.environ.get("DASHBOARD_URL") or None, tags=["satellite"], priority=3):
            state["welcomed"] = True
    stream = fomo.FomoStream(log)
    stream.start()
    time.sleep(8 if stream.available else 0)  # let the replay of the last ~15 minutes arrive
    start = now()
    while True:
        c0 = now()
        try:
            n = fomo.absorb(state, stream.drain())
            fomo.poll_board(state, log)
            log(f"FOMO: {n} new trades, {len(state.get('fomo', {}))} coins traded in 24h"
                f"{'' if stream.connected else ' (stream offline)'}")
            scan_cycle(http, state, cfg, ctx)
            refresh_alerts(http, state)
            heartbeat(state)
        except Exception as e:  # one bad cycle must not kill the run
            import traceback
            traceback.print_exc()
            log(f"cycle failed: {e}")
        prune_state(state, cfg)
        state["stats"]["api_calls"] = http.calls
        save_state(state)
        write_site(state, cfg)
        if now() - start + cycle_s + 60 > run_s:
            break
        time.sleep(max(5, cycle_s - (now() - c0)))
    stream.close()
    fomo.absorb(state, stream.drain())
    save_state(state)
    log(f"run finished after {now() - start:.0f}s; calls: {http.calls}")


if __name__ == "__main__":
    sys.exit(main())
