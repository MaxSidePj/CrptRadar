"""Offline tests for GemRadar: python -m unittest test_radar.py"""
import os
import time
import unittest

os.environ["DRY_RUN"] = "1"
import radar  # noqa: E402

CFG = dict(radar.DEFAULT_CONFIG)


def token(**kw):
    t = {"key": "solana:abc", "chain": "solana", "address": "abc", "symbol": "GEM", "name": "Gem",
         "liq": 80_000, "mcap": 600_000, "vol": {"m5": 3000, "h1": 60_000, "h6": 200_000, "h24": 300_000},
         "buys_h1": 900, "sells_h1": 600, "pc": {"m5": 2, "h1": 40, "h6": 120, "h24": 120},
         "age_h": 3.0, "socials": {"x": "https://x.com/gemcoin", "website": "https://gem.xyz"},
         "trending": True, "cto": False, "profile": True, "boosts": 0, "buyers_h1": 400, "url": None,
         "price": 0.001, "image": None, "dex": "pumpswap", "sources": ["new"]}
    t.update(kw)
    return t


SAFE = {"top10_pct": 14, "max_holder_pct": 3, "creator_pct": 0, "insider_pct": 0, "cluster_pct": 0,
        "holders": 2500, "source": "RugCheck", "lp_locked_pct": 100, "risks": []}


class SafetyTests(unittest.TestCase):
    def test_clean_token_passes(self):
        v = radar.judge_safety(SAFE, token(), CFG)
        self.assertTrue(v["verified"])
        self.assertEqual(v["rejects"], [])

    def test_hard_rejects(self):
        cases = [{"honeypot": True}, {"mint_authority": True}, {"freeze_authority": True},
                 {"top10_pct": 50}, {"max_holder_pct": 20}, {"creator_pct": 15}, {"insider_pct": 30},
                 {"bundle_pct": 35}, {"cluster_pct": 25}, {"sell_tax_pct": 25},
                 {"risks": [{"name": "Creator history of rugged tokens", "level": "danger"}]}]
        for extra in cases:
            v = radar.judge_safety({**SAFE, **extra}, token(), CFG)
            self.assertTrue(v["rejects"], f"expected reject for {extra}")

    def test_unverified_is_flagged(self):
        v = radar.judge_safety(None, token(chain="ton"), CFG)
        self.assertFalse(v["verified"])

    def test_evm_unlocked_v2_liquidity_rejected(self):
        m = {**SAFE, "source": "GoPlus", "open_source": True, "lp_v2_only": True, "lp_locked_pct": 10}
        self.assertTrue(radar.judge_safety(m, token(chain="bsc"), CFG)["rejects"])
        m["launchpad"] = "Four.meme"
        self.assertFalse(radar.judge_safety(m, token(chain="bsc"), CFG)["rejects"])

    def test_same_size_cluster(self):
        self.assertGreater(radar.same_size_cluster_pct([5.0, 5.05, 4.98, 1.2, 0.8]), 14)
        self.assertEqual(radar.same_size_cluster_pct([5.0, 3.0, 1.0, 0.5]), 0)


def with_history(state, t, hours=6, mcap_start=None, liq_start=None, holders_start=1500, holders_now=2500,
                 trend_hits=8, peak=None):
    """Fake GemRadar observations of `t` over the past `hours`."""
    n = int(hours * 6) + 1
    mc0 = mcap_start if mcap_start is not None else t["mcap"] * 0.8
    lq0 = liq_start if liq_start is not None else t["liq"] * 0.9
    snaps = []
    for i in range(n):
        f = i / (n - 1)
        snaps.append([time.time() - hours * 3600 * (1 - f), mc0 + (t["mcap"] - mc0) * f, lq0 + (t["liq"] - lq0) * f,
                      t["vol"]["h1"], t["buys_h1"], int(holders_start + (holders_now - holders_start) * f)])
    state["history"][t["key"]] = {"first": snaps[0][0], "peak": peak or max(mc0, t["mcap"]), "peak_ts": time.time(),
                                  "trend_hits": trend_hits, "snaps": snaps}


def run_score(t, safety, state, social=None):
    v = radar.judge_safety(safety, t, CFG)
    s, parts, why = radar.score_token(t, safety, v, social or {}, state, CFG)
    return s, parts, why, radar.score_token.last["gates"]


SOCIAL = {"x": {"followers": 8000, "tweets": 50, "age_days": 5}, "cg_trending": False, "biz": 1}


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.state = radar.fresh_state({})

    def test_survivor_scores_high_and_passes_all_gates(self):
        t = token(age_h=20)
        with_history(self.state, t, hours=6)
        s, parts, why, gates = run_score(t, SAFE, self.state, SOCIAL)
        self.assertGreaterEqual(s, CFG["alert_min_score"], parts)
        self.assertTrue(all(g["ok"] for g in gates), gates)

    def test_unverified_is_capped(self):
        t = token(age_h=20)
        with_history(self.state, t, hours=6)
        v = radar.judge_safety(None, t, CFG)
        s, _, _ = radar.score_token(t, SAFE, v, SOCIAL, self.state, CFG)
        self.assertLessEqual(s, 60)

    def test_young_pump_like_your_alerts_is_blocked(self):
        # IMAM: 1.4h old, +3000 holders/hour
        t = token(age_h=1.4, mcap=480_000, liq=114_000)
        with_history(self.state, t, hours=1, holders_start=1500, holders_now=4500)
        _, _, _, gates = run_score(t, {**SAFE, "holders": 4500}, self.state, SOCIAL)
        self.assertFalse(all(g["ok"] for g in gates))

    def test_fake_holder_growth_blocked(self):
        t = token(age_h=20, buys_h1=300)
        with_history(self.state, t, hours=6, holders_start=500, holders_now=9000)
        _, _, _, gates = run_score(t, {**SAFE, "holders": 9000}, self.state, SOCIAL)
        self.assertTrue(any("faster than buys" in g["text"] and not g["ok"] for g in gates))

    def test_crashed_from_peak_blocked(self):
        t = token(age_h=20, mcap=300_000)
        with_history(self.state, t, hours=6, mcap_start=900_000, peak=1_200_000)
        s, _, _, gates = run_score(t, SAFE, self.state, SOCIAL)
        self.assertFalse(all(g["ok"] for g in gates))

    def test_dying_activity_blocked(self):
        t = token(age_h=20, vol={"m5": 10, "h1": 2_000, "h6": 120_000, "h24": 300_000})
        with_history(self.state, t, hours=6)
        _, _, _, gates = run_score(t, SAFE, self.state, SOCIAL)
        self.assertFalse(all(g["ok"] for g in gates))

    def test_weak_token_scores_low(self):
        t = token(liq=13_000, vol={"m5": 0, "h1": 5_500, "h6": 9_000, "h24": 9_000}, buys_h1=40, sells_h1=40,
                  pc={"m5": -30, "h1": -20, "h6": -40, "h24": -40}, socials={}, trending=False, profile=False,
                  buyers_h1=10, age_h=100)
        s, _, _, _ = run_score(t, {**SAFE, "holders": 120}, self.state)
        self.assertLess(s, 40)

    def test_prefilter(self):
        self.assertIsNone(radar.prefilter(token(), CFG))
        self.assertEqual(radar.prefilter(token(liq=500), CFG), "low liquidity")
        self.assertEqual(radar.prefilter(token(age_h=900), CFG), "too old")
        self.assertEqual(radar.prefilter(token(symbol="USDC"), CFG), "major/stable")


OK_GATES = [{"ok": True, "text": "fine"}]


class AlertTests(unittest.TestCase):
    def entry(self, key, score=80, gates=OK_GATES):
        t = token(key=key, address=key, age_h=20)
        return {**t, "score": score, "why": ["x"], "warnings": [], "rejects": [], "verified": True,
                "safety": SAFE, "links": radar.links_for(t), "gates": gates, "fomo": None, "buzz": 50,
                "fast_gates": None, "parts": {"staying": 20}}

    def test_daily_cap_and_dedupe(self):
        state = radar.fresh_state({})
        cfg = {**CFG, "max_alerts_per_day": 2, "max_alerts_per_cycle": 5}
        sent = sum(radar.maybe_alert(self.entry(f"k{i}"), state, cfg, 0) for i in range(5))
        self.assertEqual(sent, 2)
        self.assertFalse(radar.maybe_alert(self.entry("k0"), state, cfg, 0))  # never twice
        self.assertTrue(radar.maybe_alert(self.entry("big", 95), state, cfg, 0))  # exceptional breaks cap

    def test_rejected_or_failed_gate_never_alerts(self):
        state = radar.fresh_state({})
        e = self.entry("bad", 99)
        e["rejects"] = ["Honeypot"]
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))
        e2 = self.entry("young", 99, gates=[{"ok": False, "text": "Survived 1h"}])
        self.assertFalse(radar.maybe_alert(e2, state, CFG, 0))


def fomo_event(i, typ, trader, addr="abc", minutes_ago=10, chain="solana"):
    return {"type": "alert", "id": f"e{i}", "alertType": typ, "trader": trader, "token": "GEM",
            "tokenAddress": addr, "chain": chain, "tradeUsd": 3000, "ts": (time.time() - minutes_ago * 60) * 1000}


class FomoTests(unittest.TestCase):
    def test_absorb_dedupes_and_counts_distinct_traders(self):
        state = radar.fresh_state({})
        ev = [fomo_event(1, "buy", "a"), fomo_event(2, "buy", "b"), fomo_event(3, "buy", "a"),
              fomo_event(4, "sell", "c"), fomo_event(5, "thesis", "b"),
              {"type": "alert", "id": "p", "alertType": "perp", "trader": "x", "chain": "hyperliquid"}]
        self.assertEqual(radar.fomo.absorb(state, ev), 5)
        self.assertEqual(radar.fomo.absorb(state, ev), 0)  # replayed events are not counted twice
        fs = radar.fomo.stats(state, "solana:abc")
        self.assertEqual((fs["buyers_6h"], fs["sellers_6h"], fs["theses_24h"]), (2, 1, 1))

    def young_hyped(self, buyers=4, sellers=0, holders_now=1200):
        state = radar.fresh_state({})
        ev = [fomo_event(i, "buy", f"t{i}") for i in range(buyers)]
        ev += [fomo_event(100 + i, "sell", f"s{i}") for i in range(sellers)]
        radar.fomo.absorb(state, ev)
        t = token(age_h=1.0)
        with_history(state, t, hours=0.8, holders_start=600, holders_now=holders_now)
        social = {**SOCIAL, "fomo": radar.fomo.stats(state, t["key"])}
        v = radar.judge_safety(SAFE, t, CFG)
        s, parts, why = radar.score_token(t, {**SAFE, "holders": holders_now}, v, social, state, CFG)
        last = radar.score_token.last
        entry = {**t, "score": s, "parts": parts, "why": why, "warnings": [], "rejects": [], "verified": True,
                 "safety": SAFE, "links": radar.links_for(t), "gates": last["gates"],
                 "fast_gates": last["fast_gates"], "buzz": last["buzz"], "fomo": social["fomo"]}
        return state, entry

    def test_young_coin_with_fomo_hype_alerts(self):
        state, e = self.young_hyped()
        self.assertFalse(all(g["ok"] for g in e["gates"]))  # normal rules would wait 6h
        self.assertTrue(radar.maybe_alert(e, state, CFG, 0), (e["score"], e["fast_gates"]))
        self.assertEqual(state["alerts"][0]["kind"], "fomo")

    def test_fomo_hype_but_traders_dumping_is_blocked(self):
        state, e = self.young_hyped(buyers=3, sellers=6)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_fomo_hype_with_bot_holders_is_blocked(self):
        state, e = self.young_hyped(holders_now=8000)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_single_fomo_buyer_is_not_enough(self):
        state, e = self.young_hyped(buyers=1)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))


class ParsingTests(unittest.TestCase):
    def test_x_handle(self):
        self.assertEqual(radar.parse_x_handle("https://x.com/gemcoin"), "gemcoin")
        self.assertEqual(radar.parse_x_handle("https://twitter.com/gemcoin?s=21"), "gemcoin")
        self.assertIsNone(radar.parse_x_handle("https://x.com/elonmusk/status/123"))
        self.assertIsNone(radar.parse_x_handle("https://x.com/i/communities/123"))

    def test_build_token_from_dexscreener(self):
        seed = {"key": "solana:abc", "chain": "solana", "address": "abc", "sources": {"new"}}
        pair = {"baseToken": {"address": "abc", "symbol": "GEM", "name": "Gem"}, "url": "u", "dexId": "pumpswap",
                "priceUsd": "0.01", "liquidity": {"usd": 50000}, "marketCap": 400000,
                "volume": {"h1": 20000, "h24": 90000}, "txns": {"h1": {"buys": 300, "sells": 200}},
                "priceChange": {"h1": 12}, "pairCreatedAt": int((time.time() - 7200) * 1000),
                "info": {"socials": [{"type": "twitter", "url": "https://x.com/gem"}], "websites": [{"url": "https://g"}]}}
        ctx = {"gt_trending": set(), "cto": set(), "profile": set(), "boost": {}}
        t = radar.build_token(seed, [pair], ctx)
        self.assertEqual(t["liq"], 50000)
        self.assertAlmostEqual(t["age_h"], 2.0, places=1)
        self.assertEqual(t["socials"]["x"], "https://x.com/gem")


if __name__ == "__main__":
    unittest.main()
