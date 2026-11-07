# GemRadar

Free early-token radar for every chain. Runs on GitHub Actions, pushes alerts to the ntfy app on
your iPhone, and publishes a dashboard on GitHub Pages.

## What it does

- Scans new and trending pools on all chains indexed by GeckoTerminal, plus DexScreener's new
  profiles, boosts and community takeovers, about every 90 seconds while a run is active.
- Remembers each new launch for up to 48 hours (the watch pool) and re-checks it, so it alerts
  when a coin starts gaining traction instead of at its first second.
- Scam checks: honeypot, buy/sell tax, mint/freeze authority, dev wallet, top-10 wallets, single
  whale, linked insider wallets (bubblemap-style clusters from RugCheck), bundles (trench.bot when
  online), same-size wallet clusters, liquidity lock, serial-rugger developer.
- Hype: X account followers (via fxtwitter), website/Telegram, DexScreener boosts and takeovers,
  GeckoTerminal and CoinGecko trending, 4chan /biz/ mentions.
- Sends an alert only if the coin passes **every** scam check and scores at least
  `alert_min_score` (default 70/100). Max `max_alerts_per_day` (default 6). A score of 85+ may
  break that limit.

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
| `alert_min_score` | 70 | Higher = fewer, stronger alerts |
| `max_alerts_per_day` | 6 | Daily cap (85+ scores can break it) |
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
