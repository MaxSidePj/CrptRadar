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


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.state = radar.fresh_state({})

    def test_strong_token_scores_high_and_unverified_is_capped(self):
        v = radar.judge_safety(SAFE, token(), CFG)
        social = {"x": {"followers": 8000, "tweets": 50, "age_days": 2}, "cg_trending": False, "biz": 1}
        s, parts, why = radar.score_token(token(), SAFE, v, social, self.state, CFG)
        self.assertGreaterEqual(s, CFG["alert_min_score"])
        uv = radar.judge_safety(None, token(), CFG)
        s2, _, _ = radar.score_token(token(), SAFE, uv, social, self.state, CFG)
        self.assertLessEqual(s2, 60)

    def test_weak_token_scores_low(self):
        t = token(liq=13_000, vol={"m5": 0, "h1": 5_500, "h6": 9_000, "h24": 9_000}, buys_h1=40, sells_h1=40,
                  pc={"m5": -30, "h1": -20, "h6": -40, "h24": -40}, socials={}, trending=False, profile=False,
                  buyers_h1=10, age_h=100)
        v = radar.judge_safety(SAFE, t, CFG)
        s, _, _ = radar.score_token(t, {**SAFE, "holders": 120}, v, {}, self.state, CFG)
        self.assertLess(s, 40)

    def test_prefilter(self):
        self.assertIsNone(radar.prefilter(token(), CFG))
        self.assertEqual(radar.prefilter(token(liq=500), CFG), "low liquidity")
        self.assertEqual(radar.prefilter(token(age_h=500), CFG), "too old")
        self.assertEqual(radar.prefilter(token(symbol="USDC"), CFG), "major/stable")


class AlertTests(unittest.TestCase):
    def entry(self, key, score=80):
        t = token(key=key, address=key)
        return {**t, "score": score, "why": ["x"], "warnings": [], "rejects": [], "verified": True,
                "safety": SAFE, "links": radar.links_for(t)}

    def test_daily_cap_and_dedupe(self):
        state = radar.fresh_state({})
        cfg = {**CFG, "max_alerts_per_day": 2, "max_alerts_per_cycle": 5}
        sent = sum(radar.maybe_alert(self.entry(f"k{i}"), state, cfg, 0) for i in range(5))
        self.assertEqual(sent, 2)
        self.assertFalse(radar.maybe_alert(self.entry("k0"), state, cfg, 0))  # never twice
        self.assertTrue(radar.maybe_alert(self.entry("big", 95), state, cfg, 0))  # exceptional breaks cap

    def test_rejected_never_alerts(self):
        state = radar.fresh_state({})
        e = self.entry("bad", 99)
        e["rejects"] = ["Honeypot"]
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
