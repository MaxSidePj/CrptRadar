"""Offline tests for GemRadar: python -m unittest test_radar.py"""
import json
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
        fs = {"buyers_1h": 1, "buyers_2h": 2, "buyers_6h": 3, "buyers_24h": 4, "sellers_6h": 1, "buy_usd_6h": 9000,
              "theses_24h": 0, "top_buyers": ["a", "b", "c"], "rank": None, "first_seen_h": 5,
              "buyers_7d": 4, "holders": ["a", "b", "c"], "holders_n": 3, "good_holders": [], "graduated": False,
              "sellers_24h": 1, "named_holders": 3, "good_recent": [], "fresh_buyers": 4, "fresh_sellers": 1}
        s, parts, why, gates = run_score(t, SAFE, self.state, {**SOCIAL, "fomo": fs})
        self.assertGreaterEqual(s, CFG["alert_min_score"] + 3, parts)
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


class FakeHttp:
    def __init__(self, pairs):
        self.pairs = pairs

    def get_json(self, url, **kw):
        return self.pairs


class LimitTests(unittest.TestCase):
    def test_crashed_alert_is_removed_but_never_realerted(self):
        state = radar.fresh_state({})
        for key, mc in (("dead", 500_000), ("alive", 500_000)):
            state["alerts"].append({"key": f"solana:{key}", "chain": "solana", "address": key, "symbol": key,
                                    "mcap": mc})
            state["alerted"][f"solana:{key}"] = {"ts": time.time()}
        pairs = [{"baseToken": {"address": "dead"}, "marketCap": 20_000, "liquidity": {"usd": 3000}},
                 {"baseToken": {"address": "alive"}, "marketCap": 2_000_000, "liquidity": {"usd": 90000}}]
        radar.refresh_alerts(FakeHttp(pairs), state)
        self.assertEqual([a["symbol"] for a in state["alerts"]], ["alive"])
        self.assertEqual(state["alerts"][0]["peak_mcap"], 2_000_000)
        self.assertIn("solana:dead", state["alerted"])

    def test_ntfy_ceiling_cannot_be_exceeded(self):
        state = radar.fresh_state({})
        cfg = {**CFG, "max_alerts_per_day": 1000, "max_alerts_per_cycle": 1000}
        t = token(age_h=20)
        sent = 0
        for i in range(radar.NTFY_DAILY_CEILING + 10):
            e = {**t, "key": f"k{i}", "address": f"k{i}", "score": 99, "why": [], "warnings": [], "rejects": [],
                 "verified": True, "safety": SAFE, "links": radar.links_for(t), "gates": OK_GATES,
                 "fast_gates": None, "fomo": None, "buzz": 0, "parts": {"staying": 20}}
            sent += radar.maybe_alert(e, state, cfg, 0)
        self.assertEqual(sent, radar.NTFY_DAILY_CEILING)


class AlertRescoreTests(unittest.TestCase):
    def test_old_alert_gets_current_scores_even_outside_market_filter(self):
        state = radar.fresh_state({})
        t = token(key="solana:old", address="old", age_h=40, liq=8000)  # fails the market filter now
        self.assertIsNotNone(radar.prefilter(t, CFG))
        state["alerts"].append({"key": t["key"], "chain": "solana", "address": "old", "symbol": "OLD", "score": 70})
        state["safety_cache"][t["key"]] = {"ts": time.time(), "data": SAFE}
        budget = {"deep": 0, "alerts": 0}
        radar.rescore_alerts(FakeHttp(None), state, CFG, {t["key"]: t}, [], "", set(), budget)
        now = state["alerts"][0]["now"]
        self.assertIn("staying", now["parts"])
        self.assertIsNotNone(now["buzz"])
        self.assertTrue(now["gates"])


class SmartMoneyTests(unittest.TestCase):
    def quiet_coin(self, holders=("a", "b", "c"), h6=5, sell_after=(), days_ago=5 / 24, age_h=80, mcap=320_000,
                   leaders=("a",), spotted_mcap=None):
        """Pirate-Shop-like: 3 days old, quiet trading, FOMO traders (one of them a top trader) bought a few
        hours ago and still hold."""
        state = radar.fresh_state({})
        state["fomo_rest"] = {"leaders": list(leaders), "holders": {}, "trending": {}, "graduated": {}}
        if spotted_mcap:
            state["fomo_spotted"] = {"solana:abc": {"ts": time.time() - 86400, "mcap": spotted_mcap}}
        ev = [fomo_event(i, "buy", h, minutes_ago=days_ago * 1440) for i, h in enumerate(holders)]
        ev += [fomo_event(100 + i, "sell", h, minutes_ago=60) for i, h in enumerate(sell_after)]
        radar.fomo.absorb(state, ev)
        t = token(age_h=age_h, mcap=mcap, liq=60_000, buys_h1=25, sells_h1=15, buyers_h1=20, trending=False,
                  vol={"m5": 100, "h1": 4_000, "h6": 40_000, "h24": 150_000},
                  pc={"m5": 0, "h1": 2, "h6": h6, "h24": 8})
        with_history(state, t, hours=6, mcap_start=300_000, holders_start=1400, holders_now=1450)
        fs = radar.fomo.stats(state, t["key"])
        spotted = (state.get("fomo_spotted") or {}).get(t["key"])
        if spotted:
            fs["spotted_mcap"] = spotted["mcap"]
        social = {**SOCIAL, "fomo": fs}
        safety = {**SAFE, "holders": 1450}
        v = radar.judge_safety(safety, t, CFG)
        s, parts, why = radar.score_token(t, safety, v, social, state, CFG)
        last = radar.score_token.last
        e = {**t, "score": s, "parts": parts, "why": why, "warnings": [], "rejects": [], "verified": True,
             "safety": safety, "links": radar.links_for(t), "gates": last["gates"], "fast_gates": last["fast_gates"],
             "smart_gates": last["smart_gates"], "buzz": last["buzz"], "fomo": fs}
        return state, t, e

    def test_quiet_coin_held_by_fomo_traders_passes_market_filter(self):
        state, t, _ = self.quiet_coin()
        self.assertEqual(radar.prefilter(t, CFG), "few trades")      # used to be skipped here
        fs = radar.fomo.stats(state, t["key"])
        self.assertEqual(fs["holders"], ["a", "b", "c"])
        self.assertIsNone(radar.prefilter(t, CFG, quiet_ok=radar.fomo.has_signal(fs)))

    def test_smart_money_alert_before_the_run(self):
        state, t, e = self.quiet_coin()
        self.assertTrue(radar.maybe_alert(e, state, CFG, 0), (e["score"], e["smart_gates"]))
        self.assertEqual(state["alerts"][0]["kind"], "smart")

    def test_no_smart_alert_after_it_already_ran(self):
        state, t, e = self.quiet_coin(h6=150)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_traders_who_sold_do_not_count(self):
        state, t, e = self.quiet_coin(sell_after=("a", "b"))
        self.assertEqual(e["fomo"]["holders"], ["c"])
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_two_top_traders_are_enough(self):
        state = radar.fresh_state({})
        state["fomo_rest"] = {"leaders": ["whale1", "whale2"], "holders": {}, "trending": {}, "graduated": {}}
        radar.fomo.absorb(state, [fomo_event(1, "buy", "whale1", minutes_ago=1200),
                                  fomo_event(2, "buy", "whale2", minutes_ago=3000)])
        fs = radar.fomo.stats(state, "solana:abc")
        self.assertEqual(fs["good_holders"], ["whale1", "whale2"])
        gates = radar.smart_lane_gates(token(age_h=80, pc={"m5": 0, "h1": 1, "h6": 3, "h24": 5}), None, 0, fs, [], CFG)
        self.assertTrue(all(g["ok"] for g in gates), gates)

    def test_no_top_trader_no_smart_alert(self):
        state, t, e = self.quiet_coin(leaders=())
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_above_one_million_is_too_late(self):
        state, t, e = self.quiet_coin(mcap=1_500_000)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_risen_3x_since_fomo_spotted_it_is_too_late(self):
        state, t, e = self.quiet_coin(mcap=900_000, spotted_mcap=300_000)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))
        state, t, e = self.quiet_coin(mcap=600_000, spotted_mcap=300_000)   # 2x: still fine
        self.assertTrue(radar.maybe_alert(e, state, CFG, 0), e["smart_gates"])

    def test_old_big_coin_like_dacity_is_blocked(self):
        state, t, e = self.quiet_coin(age_h=676, mcap=7_500_000)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_holders_without_fresh_buying_are_not_enough(self):
        state, t, e = self.quiet_coin(days_ago=3)      # bought 3 days ago, nothing since
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_as_many_sellers_as_buyers_like_wealth_is_blocked(self):
        state, t, e = self.quiet_coin(holders=("a", "b", "c"), sell_after=("x", "y", "z"))
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_popular_on_fomo_boards_alone_is_not_enough(self):
        state = radar.fresh_state({})
        state["fomo_rest"] = {"leaders": [], "holders": {}, "graduated": {},
                              "trending": {"solana:abc": {"rank": 5, "holders": 50}}}
        fs = radar.fomo.stats(state, "solana:abc")
        self.assertEqual(fs["holders_n"], 50)
        self.assertEqual(fs["named_holders"], 0)
        gates = radar.smart_lane_gates(token(age_h=80, pc={"m5": 0, "h1": 1, "h6": 3, "h24": 5}), None, 0, fs, [], CFG)
        self.assertFalse(all(g["ok"] for g in gates))

    def test_max_one_smart_alert_per_scan_and_five_per_day(self):
        sent = 0
        state = None
        for i in range(8):
            st, t, e = self.quiet_coin()
            if state is None:
                state = st
            e = {**e, "key": f"solana:c{i}", "address": f"c{i}"}
            if i % 2 == 0:
                state["stats"]["cycles"] = i   # a new scan every 2 coins
            sent += radar.maybe_alert(e, state, CFG, 0)
        self.assertEqual(sent, 4)                       # 1 per scan, 4 scans
        for i in range(8, 12):
            st, t, e = self.quiet_coin()
            state["stats"]["cycles"] = 100 + i
            sent += radar.maybe_alert({**e, "key": f"solana:c{i}", "address": f"c{i}"}, state, CFG, 0)
        self.assertEqual(sent, CFG["smart_max_per_day"])

    def test_fomo_api_budget_never_exceeded(self):
        state = radar.fresh_state({})
        os.environ["FOMO_API_KEY"] = "test"
        try:
            calls = 0
            orig = radar.fomo._get
            radar.fomo._get = lambda st, path, log, holder=False: (radar.fomo._budget(st).__setitem__(
                "calls", radar.fomo._budget(st)["calls"] + 1), radar.fomo._budget(st).__setitem__(
                "day_calls", radar.fomo._budget(st)["day_calls"] + 1), radar.fomo._budget(st).__setitem__(
                "holder_calls", radar.fomo._budget(st)["holder_calls"] + (1 if holder else 0)), {"holders": []})[-1]
            for i in range(100):
                radar.fomo.lookup_holders(state, f"solana:t{i}", "solana", f"t{i}", print)
            b = radar.fomo._budget(state)
            self.assertEqual(b["holder_calls"], radar.fomo.HOLDER_LOOKUPS_PER_DAY)
            b["day_calls"] = 0; b["holder_calls"] = 0; b["calls"] = radar.fomo.MONTHLY_CAP
            self.assertFalse(radar.fomo._can_call(state))
        finally:
            radar.fomo._get = orig
            del os.environ["FOMO_API_KEY"]


class FakeRpc:
    """Fake Solana RPC: FOMO signer signed s1..s4; the coin MINT has s1, s2, s3, x9 in its history."""
    def __init__(self):
        self.calls = 0

    def budget_left(self):
        return 99

    def call(self, method, params):
        self.calls += 1
        now = time.time()
        if method == "getSignaturesForAddress" and params[0] == radar.fomo_chain.FOMO_SIGNER:
            return [{"signature": f"s{i}" + "x" * 20, "blockTime": now - i * 60} for i in range(1, 5)]
        if method == "getSignaturesForAddress":
            return [{"signature": sig + "x" * 20, "blockTime": now - n * 600} for n, sig in enumerate(("s1", "s2", "s3", "x9"))]
        if method == "getTransaction":
            sig = params[0][:2]
            wallet, amount = {"s1": ("walletA", 100), "s2": ("walletB", 50), "s3": ("walletA", -100)}[sig]
            pre = [{"accountIndex": 1, "mint": "MINT", "owner": wallet, "uiTokenAmount": {"uiAmount": 100 if amount < 0 else 0}}]
            post = [{"accountIndex": 1, "mint": "MINT", "owner": wallet, "uiTokenAmount": {"uiAmount": 0 if amount < 0 else amount}}]
            return {"blockTime": now - (4 - int(sig[1])) * 600,  # s1 oldest, s3 newest
                    "transaction": {"message": {"accountKeys": [{"pubkey": radar.fomo_chain.FOMO_SIGNER, "signer": True},
                                                                {"pubkey": wallet, "signer": True}]}},
                    "meta": {"preTokenBalances": pre, "postTokenBalances": post}}


class FomoChainTests(unittest.TestCase):
    def test_small_fomo_trades_found_on_chain(self):
        state = radar.fresh_state({})
        rpc = FakeRpc()
        radar.fomo_chain.run_cycle(state, rpc, [("solana:MINT", "MINT")], lambda *a: None)
        c = radar.fomo_chain.summary(state, "solana:MINT")
        self.assertEqual(c["trades"], 3)                 # x9 is not a FOMO trade
        self.assertEqual(c["holders"], ["walletB"])       # walletA bought, then sold
        fs = radar.fomo.stats(state, "solana:MINT")
        self.assertEqual(fs["wallets_holding"], 1)
        radar.fomo_chain.run_cycle(state, rpc, [("solana:MINT", "MINT")], lambda *a: None)
        self.assertEqual(rpc.calls, 1)                   # coin cached for 3h: only the index is refreshed

    def test_two_wallets_holding_make_a_quiet_coin_eligible(self):
        state = radar.fresh_state({})
        state["fomo_chain"] = {"coins": {"solana:abc": {"ts": time.time(), "holders": ["w1", "w2"], "buyers": 2,
                                                         "sellers": 0, "trades": 2, "span_h": 8}}}
        self.assertTrue(radar.fomo.has_signal(radar.fomo.stats(state, "solana:abc")))


class DuplicateTests(unittest.TestCase):
    def test_old_repository_copy_cannot_cause_a_second_alert(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            orig = (radar.ALERTS_FILE, radar.CACHE_FILE)
            radar.ALERTS_FILE = pathlib.Path(d) / "state" / "alerts.json"
            radar.CACHE_FILE = pathlib.Path(d) / "cache" / "radar.json"
            try:
                state = radar.fresh_state(radar.load_state())
                t = token(key="solana:stank", address="stank", age_h=20)
                e = {**t, "score": 80, "why": [], "warnings": [], "rejects": [], "verified": True, "safety": SAFE,
                     "links": radar.links_for(t), "gates": OK_GATES, "fast_gates": None, "fomo": None, "buzz": 0,
                     "parts": {"staying": 20}}
                self.assertTrue(radar.maybe_alert(e, state, CFG, 0))
                radar.save_state(state)
                radar.ALERTS_FILE.write_text('{"alerted": {}, "alerts": []}')  # next run got an old copy
                state2 = radar.fresh_state(radar.load_state())
                self.assertFalse(radar.maybe_alert(dict(e), state2, CFG, 0))
                self.assertEqual([a["key"] for a in state2["alerts"]], ["solana:stank"])
            finally:
                radar.ALERTS_FILE, radar.CACHE_FILE = orig

    def test_crashed_alert_stays_removed_after_restore(self):
        state = radar.fresh_state({})
        state["alerted"]["solana:dead"] = {"ts": 1}
        state["alerts"] = []
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            orig = (radar.ALERTS_FILE, radar.CACHE_FILE)
            radar.ALERTS_FILE = pathlib.Path(d) / "a.json"
            radar.CACHE_FILE = pathlib.Path(d) / "c.json"
            try:
                radar.save_state(state)
                cache = json.loads(radar.CACHE_FILE.read_text())
                cache["alerts_backup"] = [{"key": "solana:dead", "ts": 1}]
                radar.CACHE_FILE.write_text(json.dumps(cache))
                self.assertEqual(radar.load_state()["alerts"], [])
            finally:
                radar.ALERTS_FILE, radar.CACHE_FILE = orig


GT_ELYSION = {"websites": ["https://tryelysion.fun"], "twitter": "tryelysion", "telegram": None, "coingecko": "elysion",
              "gt_score": 58, "verified": True, "categories": [], "description": "", "launchpad": True}


class ProjectLaneTests(unittest.TestCase):
    def setup_coin(self, name="Elysion Pad", chain="solana", gt=GT_ELYSION, fomo_holders=("trader1",),
                   pc=None, low=70_000, mcap=100_000, x=None, sellers=()):
        state = radar.fresh_state({})
        state["fomo_rest"] = {"leaders": [], "holders": {}, "trending": {}, "graduated": {}}
        ev = [fomo_event(i, "buy", h, minutes_ago=300) for i, h in enumerate(fomo_holders)]
        ev += [fomo_event(50 + i, "sell", h, minutes_ago=60) for i, h in enumerate(sellers)]
        radar.fomo.absorb(state, ev)
        t = token(name=name, symbol="ELY", chain=chain, age_h=24, mcap=mcap, liq=30_000, buys_h1=40, sells_h1=30,
                  buyers_h1=25, trending=False, socials={"x": "https://x.com/tryelysion", "website": "https://tryelysion.fun"},
                  vol={"m5": 100, "h1": 3_000, "h6": 20_000, "h24": 90_000},
                  pc=pc or {"m5": 1, "h1": 5, "h6": 20, "h24": 30})
        # 24h after an early pump to $230k and a crash: base at ~$70k, now recovering to $100k
        with_history(state, t, hours=8, mcap_start=low, holders_start=900, holders_now=966, peak=230_000)
        fs = radar.fomo.stats(state, t["key"])
        social = {"x": x or {"followers": 356, "tweets": 56, "age_days": 1}, "cg_trending": False, "biz": 0,
                  "fomo": fs, "gt": gt}
        safety = {**SAFE, "holders": 966}
        v = radar.judge_safety(safety, t, CFG)
        sc, parts, why = radar.score_token(t, safety, v, social, state, CFG)
        last = radar.score_token.last
        e = {**t, "score": sc, "parts": parts, "why": why, "warnings": [], "rejects": [], "verified": True,
             "safety": safety, "links": radar.links_for(t), "fomo": fs, "buzz": last["buzz"], "gates": last["gates"],
             "fast_gates": last["fast_gates"], "smart_gates": last["smart_gates"],
             "project_gates": last["project_gates"], "quality": last["quality"], "quality_why": last["quality_why"]}
        return state, e

    def test_elysion_before_the_spike_alerts_as_early_project(self):
        state, e = self.setup_coin()
        self.assertFalse(all(g["ok"] for g in e["gates"]))          # far below its launch peak: no Gem
        self.assertTrue(radar.maybe_alert(e, state, CFG, 0), (e["score"], e["project_gates"]))
        self.assertEqual(state["alerts"][0]["kind"], "project")

    def test_quiet_new_chain_coin_like_fobo(self):
        gt = {**GT_ELYSION, "gt_score": 86, "websites": ["https://fobo.fun"], "coingecko": None}
        state, e = self.setup_coin(name="FOBO", chain="robinhood", gt=gt,
                                   x={"followers": 2018, "tweets": 300, "age_days": 22})
        self.assertTrue(radar.maybe_alert(e, state, CFG, 0), (e["score"], e["project_gates"]))

    def test_no_alert_on_the_spike_itself(self):
        state, e = self.setup_coin(pc={"m5": 20, "h1": 279, "h6": 400, "h24": 300})
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))
        self.assertTrue(any("Not spiking" in g["text"] and not g["ok"] for g in e["gates"]))

    def test_no_fomo_holder_no_project_alert(self):
        state, e = self.setup_coin(fomo_holders=())
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_fomo_traders_selling_blocks(self):
        state, e = self.setup_coin(fomo_holders=("a",), sellers=("b", "c"))
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_no_real_project_no_alert(self):
        gt = {"websites": [], "twitter": None, "telegram": None, "coingecko": None, "gt_score": 20,
              "verified": False, "categories": [], "description": "", "launchpad": False}
        state, e = self.setup_coin(name="Random Dog", gt=gt, x={"followers": 50, "tweets": 3, "age_days": 0})
        e["socials"] = {}
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_still_dumping_no_alert(self):
        state, e = self.setup_coin(low=100_000, mcap=100_000)       # no recovery from the low yet
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))


GT_PAID = {"websites": ["https://usepaid.com"], "twitter": "UsePaid", "telegram": None, "coingecko": "paid",
           "gt_score": 83, "verified": True, "categories": ["Pump Fun"], "description": "", "launchpad": True}
GT_JUNK = {"websites": ["https://x.fun"], "twitter": None, "telegram": None, "coingecko": None,
           "gt_score": 47, "verified": False, "categories": [], "description": "", "launchpad": True}


class EarlyLaunchTests(unittest.TestCase):
    def coin(self, gt=GT_PAID, safety=None, liq=150_000, mcap=2_000_000, buys=600, sells=350, buyers=300,
             watched_h=0.75, peak=None, pc1=40, holders_start=2000, holders_now=2300, name="Paid",
             x={"followers": 47_000, "tweets": 400, "age_days": 22}):
        state = radar.fresh_state({})
        t = token(name=name, symbol="PAID", age_h=2.0, mcap=mcap, liq=liq, buys_h1=buys, sells_h1=sells,
                  buyers_h1=buyers, socials={"website": "https://usepaid.com"},
                  vol={"m5": 20_000, "h1": 400_000, "h6": 900_000, "h24": 900_000},
                  pc={"m5": 2, "h1": pc1, "h6": 150, "h24": 150})
        with_history(state, t, hours=watched_h, mcap_start=mcap * 0.8, holders_start=holders_start,
                     holders_now=holders_now, peak=peak or mcap)
        safety = {**SAFE, "holders": holders_now, "top10_pct": 18, "creator_pct": 1.0, **(safety or {})}
        social = {"x": x, "cg_trending": False, "biz": 0, "fomo": None, "gt": gt}
        v = radar.judge_safety(safety, t, CFG)
        sc, parts, why = radar.score_token(t, safety, v, social, state, CFG)
        last = radar.score_token.last
        e = {**t, "score": sc, "parts": parts, "why": why, "warnings": v["warnings"], "rejects": v["rejects"],
             "verified": True, "safety": safety, "links": radar.links_for(t), "fomo": None, "buzz": last["buzz"],
             **{k: last[k] for k in ("gates", "fast_gates", "smart_gates", "project_gates", "early_gates",
                                     "quality", "quality_why")}}
        return state, e

    def test_paid_like_launch_alerts_within_hours(self):
        state, e = self.coin()
        self.assertTrue(radar.maybe_alert(e, state, CFG, 0), (e["score"], e["early_gates"]))
        self.assertEqual(state["alerts"][0]["kind"], "early")

    def test_garbage_launch_without_real_project(self):
        state, e = self.coin(gt=GT_JUNK, name="Imam", x=None)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0), e["early_gates"])
        # even with a fresh, small X account it stays below the bar
        state, e = self.coin(gt=GT_JUNK, name="Imam", x={"followers": 300, "tweets": 10, "age_days": 0})
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0), e["early_gates"])

    def test_whales_or_dev_bag_blocked(self):
        for bad in ({"top10_pct": 30}, {"creator_pct": 5}, {"cluster_pct": 12}):
            state, e = self.coin(safety=bad)
            self.assertFalse(radar.maybe_alert(e, state, CFG, 0), bad)

    def test_thin_liquidity_blocked(self):
        state, e = self.coin(liq=60_000, mcap=3_000_000)   # 2% of market cap
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_bot_like_buying_blocked(self):
        state, e = self.coin(buys=900, sells=100)          # 9x more buys than sells
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_one_candle_pump_or_dump_blocked(self):
        state, e = self.coin(pc1=300)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))
        state, e = self.coin(peak=4_000_000)               # already 50% below its peak
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))

    def test_needs_30_minutes_of_watching(self):
        state, e = self.coin(watched_h=0.2)
        self.assertFalse(radar.maybe_alert(e, state, CFG, 0))


class FailureReportTests(unittest.TestCase):
    def test_all_files_match(self):
        self.assertEqual(radar.outdated_files(), [])

    def test_outdated_fomo_file_is_detected(self):
        old = radar.fomo.VERSION
        radar.fomo.VERSION = 3
        try:
            self.assertEqual(radar.outdated_files(), ["fomo.py"])
        finally:
            radar.fomo.VERSION = old

    def test_warning_after_three_failed_scans_once_a_day(self):
        state = radar.fresh_state({})
        sent = []
        orig = radar.push
        radar.push = lambda *a, **k: sent.append(a[0]) or True
        try:
            for _ in range(6):
                radar.report_failure(state, KeyError("named_holders"))
        finally:
            radar.push = orig
        self.assertEqual(len(sent), 1)
        self.assertIn("named_holders", state["stats"]["last_error"])


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
