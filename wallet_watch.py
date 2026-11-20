"""Read-only Solana wallet monitoring, independent of FOMO's large-position feed.

Recognized swap instructions plus the owner's actual token/quote balance changes are required.
No wallet keys, trading permissions or FOMO API calls are used. Cursors and unfinished work live
in the scanner cache. RPC calls share fomo_chain.Rpc's paced per-cycle budget.
"""
from __future__ import annotations

import hashlib
import time
from decimal import Decimal, InvalidOperation

VERSION = 1
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
QUOTES = STABLES | {WSOL}
# Official Pump and Raydium program documentation; routed swaps are recognized in inner calls.
ANCHOR_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM SDK
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2 SDK
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
}
RAYDIUM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
SWAP_NAMES = ("buy", "sell", "buy_v2", "sell_v2", "buy_exact_sol_in", "buy_exact_quote_in", "swap", "swap_v2",
              "swap2", "swap_exact_out", "swap_exact_out2", "two_hop_swap", "two_hop_swap_v2",
              "swap_base_input", "swap_base_output", "swap_router_base_in")
DISCRIMINATORS = {hashlib.sha256(("global:" + name).encode()).digest()[:8] for name in SWAP_NAMES}
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _decode(value):
    if not isinstance(value, str) or len(value) > 20000:
        return b""
    n = 0
    try:
        for char in value:
            n = n * 58 + BASE58.index(char)
    except ValueError:
        return b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")


def _instructions(tx):
    yield from ((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        yield from group.get("instructions") or []


def _is_swap(tx, keys):
    for ins in _instructions(tx):
        program = ins.get("programId")
        if not program and isinstance(ins.get("programIdIndex"), int) and ins["programIdIndex"] < len(keys):
            program = keys[ins["programIdIndex"]]
        data = _decode(ins.get("data"))
        if program in ANCHOR_PROGRAMS and data[:8] in DISCRIMINATORS:
            return True
        if program == RAYDIUM_V4 and data[:1] in (b"\x09", b"\x0b"):
            return True
    return False


def _amount(balance):
    info = balance.get("uiTokenAmount") or {}
    return Decimal(info["amount"]) / (Decimal(10) ** int(info["decimals"]))


def parse_swap(tx, wallet, trader, signature):
    """Return zero or more observed buys/sells. Transfers and LP deposits are not swaps.

    USD fill size stays unknown for SOL/token swaps. Stablecoin spend is a quote amount, not
    a historical price oracle. Native SOL is adjusted for fees and owned token-account rent.
    """
    if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
        return []
    meta = tx["meta"]
    if meta.get("err") is not None or not isinstance(tx.get("blockTime"), (int, float)):
        return []
    message = ((tx.get("transaction") or {}).get("message") or {})
    accounts = message.get("accountKeys") or []
    keys = [a.get("pubkey") if isinstance(a, dict) else a for a in accounts]
    signers = {a.get("pubkey") for a in accounts if isinstance(a, dict) and a.get("signer")}
    if wallet not in keys or wallet not in signers or not _is_swap(tx, keys):
        return []
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}
    delta, owned = {}, []
    try:
        for index in set(pre) | set(post):
            before, after = pre.get(index), post.get(index)
            owner = (after or {}).get("owner") or (before or {}).get("owner")
            if owner != wallet:
                continue
            if before and before.get("owner", wallet) != wallet:
                return []  # authority changes are not a purchase
            if before and after and before.get("mint") != after.get("mint"):
                return []
            mint = (after or before)["mint"]
            before_amount, after_amount = _amount(before) if before else Decimal(0), _amount(after) if after else Decimal(0)
            delta[mint] = delta.get(mint, Decimal(0)) + after_amount - before_amount
            owned.append((index, mint, before_amount, after_amount))
        index = keys.index(wallet)
        before_lamports, after_lamports = meta.get("preBalances") or [], meta.get("postBalances") or []
        if index >= len(before_lamports) or index >= len(after_lamports):
            return []
        native = Decimal(after_lamports[index] - before_lamports[index])
        if index == 0:
            native += meta.get("fee", 0)
        for index, mint, before_amount, after_amount in owned:
            if index >= len(before_lamports) or index >= len(after_lamports):
                return []
            rent_change = Decimal(after_lamports[index] - before_lamports[index])
            if mint == WSOL:
                rent_change -= (after_amount - before_amount) * 1_000_000_000
            native += rent_change
        delta[WSOL] = delta.get(WSOL, Decimal(0)) + native / 1_000_000_000
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return []
    bought = sorted(m for m, amount in delta.items() if amount > 0 and m not in QUOTES)
    sold = sorted(m for m, amount in delta.items() if amount < 0 and m not in QUOTES)
    quote_out = any(delta.get(m, 0) < 0 for m in QUOTES)
    quote_in = any(delta.get(m, 0) > 0 for m in QUOTES)
    events = []
    for side, mints, confirmed in (("buy", bought, quote_out or bool(sold)), ("sell", sold, quote_in or bool(bought))):
        if not confirmed:
            continue
        for mint in mints:
            events.append({"id": f"wallet:{signature}:{mint}:{side}", "type": "alert", "alertType": side,
                "source": "wallet", "chain": "solana", "tokenAddress": mint, "trader": trader,
                "token": "?", "txHash": signature, "wallet": wallet, "ts": tx["blockTime"] * 1000,
                "tokenAmount": str(abs(delta[mint]))})
    return events


def poll(state, rpc, cfg, log):
    """Queue new signatures durably before advancing cursors, then parse within a fixed budget.

    A null transaction remains pending. An unfinished pagination scan retains its old cursor.
    The filtered lane must wait until both history pagination and transaction parsing catch up.
    """
    if not cfg.get("wallet_watch") or not cfg.get("tracked_lane"):
        return []
    watched = {str(h).strip().lstrip("@").casefold() for h in cfg.get("tracked_traders", [])}
    events, start_calls = [], rpc.calls
    limit = max(2, min(int(cfg.get("wallet_rpc_calls_per_cycle", 8)), 10))
    cutoff = time.time() - cfg.get("tracked_max_signal_hours", 2) * 3600
    monitors = state.setdefault("wallet_monitor", {})
    for trader, wallet in (cfg.get("tracked_solana_wallets") or {}).items():
        if trader.casefold() not in watched:
            continue
        st = monitors.setdefault(wallet, {"cursor": None, "pending": [], "seen": {}})
        st["ready"] = False
        st["trader"] = trader
        st["seen"] = {s: ts for s, ts in st["seen"].items() if ts >= time.time() - 7 * 86400}
        st["pending"] = [r for r in st["pending"] if (r.get("blockTime") or r.get("queued_at") or time.time()) >= cutoff]
        pages = 0
        succeeded = False
        while pages < 2 and rpc.calls - start_calls < limit - 1 and rpc.budget_left() > 1:
            paging = st.get("paging") or {}
            params = {"limit": 100, "commitment": "confirmed"}
            target = paging.get("target") or st.get("cursor")
            if target:
                params["until"] = target
            if paging.get("before"):
                params["before"] = paging["before"]
            page = rpc.call("getSignaturesForAddress", [wallet, params])
            if not isinstance(page, list):
                break
            pages += 1
            succeeded = True
            st["checked"] = time.time()
            head = paging.get("head") or (page[0]["signature"] if page else st.get("cursor"))
            pending_ids = {r["signature"] for r in st["pending"]}
            for row in page:
                sig = row.get("signature")
                if not sig or row.get("err") or sig in st["seen"] or sig in pending_ids:
                    continue
                if row.get("blockTime") and row["blockTime"] < cutoff:
                    continue
                st["pending"].append({"signature": sig, "blockTime": row.get("blockTime"),
                                      "slot": row.get("slot", 0), "queued_at": time.time()})
                pending_ids.add(sig)
            finished = len(page) < 100 or (page and page[-1].get("blockTime") and page[-1]["blockTime"] < cutoff)
            if finished:
                st["cursor"] = head
                st.pop("paging", None)
                break
            st["paging"] = {"head": head, "target": target, "before": page[-1]["signature"]}
        # Process oldest first. Do not let one temporarily null response lose newer signatures.
        for row in sorted(list(st["pending"]), key=lambda r: r.get("slot", 0)):
            if rpc.calls - start_calls >= limit or rpc.budget_left() <= 0:
                break
            sig = row["signature"]
            tx = rpc.call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                    "maxSupportedTransactionVersion": 1}])
            if not isinstance(tx, dict) or not isinstance(tx.get("meta"), dict):
                continue
            if tx.get("blockTime") is None:
                continue
            if tx["blockTime"] >= cutoff:
                events.extend(parse_swap(tx, wallet, trader, sig))
            st["seen"][sig] = tx["blockTime"]
            st["pending"].remove(row)
        st["ready"] = succeeded and not st.get("paging") and not st["pending"]
        state.setdefault("stats", {}).setdefault("wallet_watch", {})[trader] = {
            "checked": st.get("checked"), "ready": st["ready"], "pending": len(st["pending"]),
            "paging": bool(st.get("paging"))}
        log(f"Wallet watch {trader}: {len(st['pending'])} pending, "
            f"{'caught up' if st['ready'] else 'waiting for RPC/history'}")
    return events


def coverage(state, trader, cfg, now=None):
    """None when direct monitoring is disabled; otherwise require a recent, complete poll."""
    if not cfg.get("wallet_watch"):
        return None
    now = time.time() if now is None else now
    mapping = cfg.get("tracked_solana_wallets") or {}
    matches = [wallet for name, wallet in mapping.items() if name.casefold() == trader.casefold()]
    if not matches:
        return None
    return all((state.get("wallet_monitor", {}).get(wallet) or {}).get("ready")
               and 0 <= now - state["wallet_monitor"][wallet].get("checked", 0) <= 300 for wallet in matches)
