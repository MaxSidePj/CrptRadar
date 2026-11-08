# GemRadar

Free early-token radar for every chain. Runs on GitHub Actions, pushes alerts to the ntfy app on
your iPhone, and publishes a dashboard on GitHub Pages.

## What it does

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
| fomoapi.io | live feed free; 1,000 REST calls/month | ~480/month; stops for a day if it ever answers 402/429 |

No service has a payment card attached, so nothing can ever be billed. state/alerts.json is
updated at least weekly so GitHub never pauses the schedule for inactivity.
