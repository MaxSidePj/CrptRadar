"""Offline wallet-monitor tests, including a reduced real Fiona transaction."""
import copy
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fomo
import wallet_watch as watch
import radar
from test_radar import token, SAFE

WALLET = "FfztZq1outrYfyYbKw5P4LGo4xUc4XYEdf7o91UdS7qu"
MINT = "test-mint"
PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CFG = {**radar.DEFAULT_CONFIG}


def encode(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = watch.BASE58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def balance(index, mint, value, owner=WALLET):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(value), "decimals": 6}}


def transaction(side="buy", ts=None, native=False, instruction="buy"):
    quote = watch.WSOL if native else sorted(watch.STABLES)[0]
    before_coin, after_coin = (0, 1_000_000) if side == "buy" else (1_000_000, 0)
    before_quote, after_quote = (2_000_000, 1_000_000) if side == "buy" else (1_000_000, 2_000_000)
    tx = {"blockTime": ts or int(time.time()), "version": 1,
          "transaction": {"message": {"accountKeys": [{"pubkey": WALLET, "signer": True},
              {"pubkey": "ata", "signer": False}, {"pubkey": "quote-ata", "signer": False}],
              "instructions": [{"programId": PROGRAM, "data": encode(hashlib.sha256(("global:" + instruction).encode()).digest()[:8])}]}},
          "meta": {"err": None, "fee": 5000,
              "preBalances": [3_000_000_000, 2039280, 2039280],
              "postBalances": [2_999_995_000, 2039280, 2039280],
              "preTokenBalances": [balance(1, MINT, before_coin)],
              "postTokenBalances": [balance(1, MINT, after_coin)]}}
    if native:
        tx["meta"]["postBalances"][0] += -1_000_000 if side == "buy" else 1_000_000
    else:
        tx["meta"]["preTokenBalances"].append(balance(2, quote, before_quote))
        tx["meta"]["postTokenBalances"].append(balance(2, quote, after_quote))
    return tx


class ParserTests(unittest.TestCase):
    def test_actual_fiona_buy_under_feed_threshold(self):
        tx = json.loads(Path(__file__).with_name("test_wallet_fiona.json").read_text(encoding="utf-8"))
        result = watch.parse_swap(tx, WALLET, "0xsneakky", tx["transaction"]["signatures"][0])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["alertType"], "buy")
        self.assertEqual(result[0]["tokenAddress"], "B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump")
        self.assertEqual(result[0]["ts"], 1795122152000)
        self.assertAlmostEqual(float(result[0]["tokenAmount"]), 16712915.577418)
        self.assertNotIn("tradeUsd", result[0])  # unknown USD amounts never become invented fill sizes

    def test_stable_and_native_buys_and_full_sells(self):
        for native in (False, True):
            for side in ("buy", "sell"):
                with self.subTest(native=native, side=side):
                    tx = transaction(side, native=native)
                    if side == "sell":
                        tx["meta"]["postTokenBalances"] = [b for b in tx["meta"]["postTokenBalances"] if b["mint"] != MINT]
                    self.assertEqual(watch.parse_swap(tx, WALLET, "0xsneakky", "sig")[0]["alertType"], side)

    def test_transfers_airdrops_and_fees_are_not_buys(self):
        tx = transaction(native=True)
        tx["transaction"]["message"]["instructions"] = []
        self.assertEqual(watch.parse_swap(tx, WALLET, "0xsneakky", "sig"), [])
        tx = transaction(native=True)
        tx["meta"]["postBalances"][0] = tx["meta"]["preBalances"][0] - tx["meta"]["fee"]
        self.assertEqual(watch.parse_swap(tx, WALLET, "0xsneakky", "sig"), [])

    def test_owned_ata_creation_rent_does_not_fake_sol_spend(self):
        tx = transaction(native=True)
        tx["meta"]["preTokenBalances"] = []
        tx["meta"]["preBalances"][1] = 0
        tx["meta"]["postBalances"][0] = tx["meta"]["preBalances"][0] - 5000 - 2039280
        self.assertEqual(watch.parse_swap(tx, WALLET, "0xsneakky", "sig"), [])

    def test_wrapping_sol_is_not_a_buy(self):
        tx = transaction(native=True)
        tx["meta"]["preTokenBalances"] = [balance(1, watch.WSOL, 0)]
        tx["meta"]["postTokenBalances"] = [balance(1, watch.WSOL, 1_000_000)]
        self.assertEqual(watch.parse_swap(tx, WALLET, "0xsneakky", "sig"), [])

    def test_failed_unrecognized_deposit_or_unsigned_not_classified(self):
        for change in ("failed", "deposit", "unknown_program", "unsigned", "missing_time"):
            tx = transaction(instruction="deposit" if change == "deposit" else "buy")
            if change == "failed": tx["meta"]["err"] = {"InstructionError": [0, "failed"]}
            if change == "unknown_program": tx["transaction"]["message"]["instructions"][0]["programId"] = "unknown"
            if change == "unsigned": tx["transaction"]["message"]["accountKeys"][0]["signer"] = False
            if change == "missing_time": tx["blockTime"] = None
            self.assertEqual(watch.parse_swap(tx, WALLET, "0xsneakky", "sig"), [], change)

    def test_token_to_token_swap_emits_both_sides(self):
        tx = transaction()
        for b in tx["meta"]["preTokenBalances"] + tx["meta"]["postTokenBalances"]:
            if b["accountIndex"] == 2: b["mint"] = "other-mint"
        events = watch.parse_swap(tx, WALLET, "0xsneakky", "sig")
        self.assertEqual({(e["tokenAddress"], e["alertType"]) for e in events}, {(MINT, "buy"), ("other-mint", "sell")})


class FakeRpc:
    def __init__(self, pages, transactions=None):
        self.pages, self.transactions = list(pages), transactions or {}
        self.calls, self.requests = 0, []

    def budget_left(self): return max(0, 16 - self.calls)

    def call(self, method, params):
        self.calls += 1
        self.requests.append((method, params))
        if method == "getSignaturesForAddress": return self.pages.pop(0)
        tx = self.transactions.get(params[0])
        if tx and isinstance(tx.get("version"), int) and tx["version"] > params[1].get("maxSupportedTransactionVersion", 0):
            return None
        return tx


class PollTests(unittest.TestCase):
    def row(self, signature, ts=None, slot=1):
        return {"signature": signature, "blockTime": ts or int(time.time()), "slot": slot, "err": None}

    def test_small_buy_enters_normal_scoring_path_and_replay_is_deduped(self):
        state = radar.fresh_state({})
        rpc = FakeRpc([[self.row("sig")]], {"sig": transaction()})
        events = watch.poll(state, rpc, CFG, lambda *a: None)
        self.assertEqual(len(events), 1)
        fomo.absorb(state, events)
        self.assertIsNotNone(radar.tracked_candidate(state, "solana:" + MINT, CFG))
        self.assertTrue(watch.coverage(state, "0xsneakky", CFG))
        state = json.loads(json.dumps(state))
        again = FakeRpc([[self.row("sig")]], {"sig": transaction()})
        self.assertEqual(watch.poll(state, again, CFG, lambda *a: None), [])
        self.assertEqual(again.calls, 1)

    def test_actual_fiona_transaction_passes_version_one_rpc_and_signal_ingestion(self):
        tx = json.loads(Path(__file__).with_name("test_wallet_fiona.json").read_text(encoding="utf-8"))
        sig = tx["transaction"]["signatures"][0]
        state = radar.fresh_state({})
        with patch.object(watch.time, "time", return_value=tx["blockTime"] + 10):
            events = watch.poll(state, FakeRpc([[self.row(sig, ts=tx["blockTime"])]], {sig: tx}), CFG, lambda *a: None)
            self.assertEqual(len(events), 1)
            fomo.absorb(state, events)
            signal = radar.tracked_candidate(state, "solana:" + events[0]["tokenAddress"], CFG)
            self.assertEqual(signal["trader"], "0xsneakky")
            self.assertTrue(watch.coverage(state, "0xsneakky", CFG))

    def test_null_transaction_retries_after_restart(self):
        state = {}
        watch.poll(state, FakeRpc([[self.row("sig")]]), CFG, lambda *a: None)
        self.assertFalse(watch.coverage(state, "0xsneakky", CFG))
        self.assertEqual(state["wallet_monitor"][WALLET]["cursor"], "sig")
        state = json.loads(json.dumps(state))
        events = watch.poll(state, FakeRpc([[]], {"sig": transaction()}), CFG, lambda *a: None)
        self.assertEqual(len(events), 1)
        self.assertTrue(watch.coverage(state, "0xsneakky", CFG))

    def test_direct_buy_can_alert_only_after_existing_quality_checks(self):
        state = radar.fresh_state({})
        events = watch.poll(state, FakeRpc([[self.row("sig")]], {"sig": transaction()}), CFG, lambda *a: None)
        fomo.absorb(state, events)
        t = token(key="solana:" + MINT, address=MINT, age_h=.5, mcap=100_000, liq=30_000)
        state["safety_cache"][t["key"]] = {"ts": time.time(), "data": SAFE}
        with patch.object(radar, "gt_info", return_value={}), patch.object(radar, "x_profile", return_value=None):
            entry = radar.evaluate(None, state, CFG, t, "", set(), {"deep": 0}, "deep")
        with patch.object(radar, "push", return_value=True) as push:
            self.assertTrue(radar.maybe_alert(entry, state, CFG, 0), entry)
            self.assertEqual(state["alerts"][0]["kind"], "tracked")
            self.assertEqual(push.call_count, 1)
        state["alerted"] = {}
        with patch.object(radar, "push") as push:
            self.assertFalse(radar.maybe_alert({**entry, "rejects": ["Honeypot"]}, state, CFG, 0))
            push.assert_not_called()

    def test_stale_wallet_poll_blocks_coverage(self):
        state = {"wallet_monitor": {WALLET: {"ready": True, "checked": time.time() - 301}}}
        self.assertFalse(watch.coverage(state, "0xsneakky", CFG))

    def test_pagination_does_not_advance_cursor_until_finished(self):
        state = {"wallet_monitor": {WALLET: {"cursor": "old", "pending": [], "seen": {}}}}
        page = [self.row(f"s{i}", slot=i) for i in range(100)]
        cfg = {**CFG, "wallet_rpc_calls_per_cycle": 2}
        watch.poll(state, FakeRpc([page]), cfg, lambda *a: None)
        st = state["wallet_monitor"][WALLET]
        self.assertEqual(st["cursor"], "old")
        self.assertEqual(st["paging"]["before"], "s99")
        self.assertEqual(len(st["pending"]), 100)
        rpc = FakeRpc([[]])
        watch.poll(state, rpc, cfg, lambda *a: None)
        self.assertEqual(rpc.requests[0][1][1]["before"], "s99")
        self.assertEqual(st["cursor"], "s0")
        self.assertFalse(st["ready"])

    def test_burst_or_rpc_failure_blocks_filtered_lane_until_caught_up(self):
        state = radar.fresh_state({})
        rows = [self.row(f"s{i}", slot=i) for i in range(10)]
        rpc = FakeRpc([rows], {f"s{i}": transaction() for i in range(10)})
        events = watch.poll(state, rpc, CFG, lambda *a: None)
        fomo.absorb(state, events)
        gates, _ = radar.tracked_lane_gates(token(key="solana:" + MINT), SAFE, [], state, CFG)
        self.assertFalse(gates[0]["ok"])
        self.assertLessEqual(rpc.calls, 8)
        watch.poll(state, FakeRpc([None]), CFG, lambda *a: None)
        self.assertFalse(watch.coverage(state, "0xsneakky", CFG))

    def test_shared_rpc_budget_is_respected(self):
        rpc = FakeRpc([[self.row("sig")]], {"sig": transaction()})
        rpc.calls = 15
        self.assertEqual(watch.poll({}, rpc, CFG, lambda *a: None), [])
        self.assertEqual(rpc.calls, 15)

    def test_disabled_watch_makes_no_requests_and_old_events_are_not_signals(self):
        rpc = FakeRpc([])
        self.assertEqual(watch.poll({}, rpc, {**CFG, "wallet_watch": False}, print), [])
        self.assertEqual(rpc.calls, 0)
        old = self.row("old", ts=int(time.time()) - 3 * 3600)
        rpc = FakeRpc([[old]])
        self.assertEqual(watch.poll({}, rpc, CFG, lambda *a: None), [])
        self.assertEqual(rpc.calls, 1)

    def test_sell_cancels_prior_buy_without_sending_any_push(self):
        state = radar.fresh_state({})
        buy = transaction(ts=int(time.time()) - 60)
        sell = transaction("sell")
        events = watch.poll(state, FakeRpc([[self.row("sell", slot=2), self.row("buy", slot=1)]],
                            {"buy": buy, "sell": sell}), CFG, lambda *a: None)
        with patch.object(radar, "push") as push:
            fomo.absorb(state, events)
            self.assertIsNone(radar.tracked_candidate(state, "solana:" + MINT, CFG))
            push.assert_not_called()


if __name__ == "__main__": unittest.main()
