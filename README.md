# GemRadar

Free early-token radar for every chain. Runs on GitHub Actions, pushes alerts to the ntfy app on
your iPhone, and publishes a dashboard on GitHub Pages.

## What it does

- **ResellCalendar buys:** one separate ntfy notification per coin (chain and contract) bought by ResellCalendar
  observed in the FOMO trade feed, with coin, chain, buy amount, UTC trade time, contract and chart.
  These activity notifications bypass score, age, scam checks, quiet hours and normal coin alert
  caps. They are not safety endorsements. Replayed trades are deduplicated across runs; failed
  pushes are retried. Further buys of the same coin do not trigger another notification, even
  across scanner runs. Different contracts remain separate even with the same symbol. Normal scored
  alerts for that coin remain eligible. Coverage depends on the feed: it reports large trades,
  and GitHub Actions schedule gaps can miss events beyond its roughly 15-minute replay window.
- Scans new and trending pools on all chains indexed by GeckoTerminal, plus DexScreener's new
  profiles, boosts and community takeovers, about every 90 seconds while a run is active.
- Remembers each launch (the watch pool) and re-checks it: dead coins are dropped, active ones are
  followed for up to 30 days, so it alerts
  when a coin starts gaining traction instead of at its first second.
- Scam checks: honeypot, buy/sell tax, mint/freeze authority, dev wallet, top-10 wallets, single
  whale, linked insider wallets (bubblemap-style clusters from RugCheck), bundles (trench.bot when
  online), same-size wallet clusters, liquidity lock, serial-rugger developer.
- Hype: X account followers (via fxtwitter), website/Telegram, DexScreener boosts and takeovers,
  GeckoTerminal and CoinGecko trending, 4chan /biz/ mentions.
- Staying power: never alerts on coins younger than 6 hours. A coin must have been watched for
  3+ hours, be no more than 45% below its peak, still trade actively, and not lose liquidity,
  market cap or holders over 3 hours. Holders growing faster than real buys (bot/airdrop wallets)
  blocks the alert.
- FOMO meter (0-100): volume speeding up, buyers outnumbering sellers, repeated trending, steady
  new holders, CoinGecko trending, X followers.
- FOMO app (fomo.family) via the unofficial fomoapi.io: live feed of FOMO traders' large buys and
  sells (free, no key), FOMO trending board (free key, `FOMO_API_KEY` secret, checked hourly).
  FOMO buys add up to 15 points. FOMO fast lane: a young coin bought by 3+ FOMO traders within 2
  hours (or top 20 on FOMO trending) alerts early as 🔥 if all scam checks pass, FOMO traders
  aren't selling, it hasn't crashed and its holders aren't bots.
- 🐋 Smart money: alerts before the move on low caps (max $1M) when at least one trader from FOMO's
  top-150 leaderboard holds the coin, FOMO traders are buying more than selling, and it hasn't risen
  more than 2.5x since FOMO traders were first seen in it. Every scam
  check still applies. FOMO trades are remembered for 7 days; with a free `FOMO_API_KEY` GemRadar also
  reads the leaderboard (daily), FOMO's trending and graduated coins (every 4h) and "which FOMO
  traders hold this coin" (15 lookups a day), capped at 900 of the 1,000 free calls a month.
- 🧩 Early project: real projects (website, own X account made before the coin, GeckoTerminal
  score, CoinGecko listing, tech theme or new chain) with at least one FOMO holder, under $1.5M,
  forming a base (+15% above the 24h low, not dumping) but not spiking yet. Based on what PAID,
  FLOAT, Elysion Pad, FOBO, TORCH and CASHCAT looked like before their 11x-61x runs.
- 🚀 Early launch: 1-6h old coins that are clearly real projects (score 6+/14) with clean wallets,
  deep liquidity, organic demand and holding their gains, under $5M. In a price replay it would have
  caught CLAUDIA at $203k (57x to peak), PAID at $1.3M (40x), FLOAT at $1.1M (13x) and DOPE at $3.5M
  (9x), and skipped HODL and AMD (both went to ~$2k).
- 💎/🏆 don't alert on a spike (max +100% in 1h, +200% in 6h).
- FOMO trades of any size on Solana, read for free from the public blockchain: every FOMO-app trade
  is signed by one FOMO wallet (identified by Bitquery, not confirmed by FOMO). GemRadar keeps 12
  hours of that wallet's transaction IDs and matches them against a coin's own recent transactions,
  so it sees which FOMO wallets bought and still hold, even small and quiet buys. Max 16 paced calls
  per cycle to the free Solana RPC.
- Sends an alert only if the coin passes **every** scam and staying-power check and scores at least
  `alert_min_score` (default 65/100). Max `max_alerts_per_day` (default 40, never above 60).
- Alerts for coins that crash below 10% of their alert market cap are removed automatically
  (they are never alerted again).

## Setup on iPhone (one time, ~10 minutes)

1. **ntfy:** in the ntfy app tap **+**, subscribe to a new private topic (for example the one
   suggested in the chat). Use a different topic from Stcks.
2. **Create the repository:** on github.com tap **+ → New repository**, name it `GemRadar`,
   choose **Public** (required for free Pages and unlimited Actions minutes), tick
   **Add a README**, then **Create repository**.
3. **Upload the files:** **Add file → Upload files**, select every file from this folder except
   `radar-workflow.txt` and `README.md` (optional), then **Commit changes**.
4. **Workflow:** **Add file → Create new file**. Name it exactly `.github/workflows/radar.yml`.
   Paste everything from `radar-workflow.txt`, then **Commit changes**.
5. **Secret:** **Settings → Secrets and variables → Actions → New repository secret**.
   Name `NTFY_TOPIC`, value = your topic name.
6. **Pages:** **Settings → Pages → Source: GitHub Actions**.
7. **Start:** **Actions → GemRadar scan → Run workflow**. After about 13 minutes you get a
   "GemRadar is live" notification. Open `https://<your-username>.github.io/GemRadar/` in Safari,
   tap **Share → Add to Home Screen**.

After that it runs by itself every 15 minutes.

## Tuning

Edit `config.json` on GitHub and commit. Useful settings:

| Setting | Default | Meaning |
|---|---|---|
| `alert_min_score` | 65 | Higher = fewer, stronger alerts |
| `max_alerts_per_day` | 40 | Daily cap; the code never sends more than 60 (ntfy free limit is 250) |
| `min_alert_age_hours` | 6 | Never alert on coins younger than this |
| `max_drawdown_pct` | 45 | Max % below its peak |
| `fomo_fast_lane` | true | Early 🔥 alerts for young coins with FOMO-app hype |
| `fomo_fast_min_buyers` | 3 | FOMO traders needed within 2h for the fast lane |
| `smart_money_lane` | true | 🐋 alerts when FOMO traders already hold a coin before it runs |
| `smart_min_top_traders` | 1 | Top-150 FOMO traders that must hold it (needs the free `FOMO_API_KEY`) |
| `smart_min_holders` | 2 | Named FOMO traders/wallets holding in total (or 2+ top traders) |
| `smart_max_runup_6h_pct` | 100 | No 🐋 alert once it already rose this much in 6h |
| `smart_min_fresh_buyers` | 2 | FOMO traders that must have bought in the last 24h (and more buyers than sellers) |
| `smart_max_age_hours` | 336 | 🐋 only for coins up to 14 days old |
| `smart_max_mcap_usd` | 1000000 | 🐋 only below this market cap |
| `smart_max_rise_since_spotted` | 2.5 | No 🐋 once it rose more than this since FOMO traders were first seen in it |
| `smart_max_per_day` | 5 | Max 🐋 alerts per day (and 1 per scan) |
| `project_lane` | true | 🧩 alerts for real projects forming a base |
| `project_min_quality` | 5 | Project quality needed (0-14) |
| `project_max_mcap_usd` | 1500000 | 🧩 only below this market cap |
| `project_min_recovery_pct` | 15 | Must be this much above its 24h low |
| `project_max_per_day` | 3 | Max 🧩 alerts per day |
| `new_chains` / `tech_keywords` | lists | Chains and words that count as new-chain / tech theme |
| `max_runup_1h_pct` / `max_runup_6h_pct` | 100 / 200 | No 💎/🏆 alert on a spike |
| `early_lane` | true | 🚀 alerts for 1-6h old real projects |
| `early_min_quality` | 6 | Project score needed for 🚀 |
| `early_max_mcap_usd` | 5000000 | 🚀 only below this market cap |
| `early_max_per_day` | 3 | Max 🚀 alerts per day |
| `min_liquidity_usd` | 12000 | Ignore coins with less liquidity |
| `max_mcap_usd` | 50000000 | Ignore coins above this market cap |
| `chains_only` | `[]` | e.g. `["solana", "base"]` to restrict |
| `quiet_hours_utc` | `[]` | e.g. `[0,1,2,3,4,5]`: only 85+ alerts at night |

## Limits

- X's own API isn't free. Hype from X = the coin's account followers and age, plus a button that
  opens live X posts about the ticker.
- Safety can be checked on Solana, Tron and about 40 EVM chains (incl. Ethereum, Base, BSC,
  Robinhood). Coins on other chains (TON, Sui, …) show on the dashboard but are never alerted.
- trench.bot (Solana bundles) is a free unofficial service that is often offline; the
  same-size-wallet bundle check always runs.
- GitHub can start scheduled runs late, so alerts arrive within minutes, not seconds.
- Not financial advice. Passing the checks lowers scam risk; it does not remove it.

## Always free

| Service | Free limit | GemRadar uses |
|---|---|---|
| GitHub Actions (public repo) | unlimited minutes | ~1 run per 15 min |
| GitHub Pages | 100 GB/month, ~10 deploys/hour | 4 deploys/hour, tiny page |
| GitHub cache | 10 GB, old entries auto-deleted | ~1 MB per run |
| ntfy.sh | 250 messages/day | max 60/day (hard-coded) |
| GeckoTerminal | 30 calls/min | max 15/min |
| DexScreener | 60-300 calls/min | well below |
| GoPlus | free tier | max 15 checks per 90 s |
| Solana public RPC | free, rate-limited | max 16 calls per ~90 s, pauses 2 min on 429 |
| fomoapi.io | live feed free; 1,000 REST calls/month | max 29/day and 900/month (hard-coded); stops for a day on 401/402/429 |

No service has a payment card attached, so nothing can ever be billed. state/alerts.json is
updated at least weekly so GitHub never pauses the schedule for inactivity.
