# MemeRadar 📡

**A paper-trading bot that hunts Solana memecoin pumps, flags pump-and-dumps,
tracks whale / insider wallets, and sells *before* the dump — all on a live,
professional dashboard. Starts with a $10,000 paper bankroll. No real money.**

![dashboard](docs/dashboard.png)

> ⚠️ **Paper trading & educational only.** This places **no real orders** and is
> **not financial advice.** Memecoins are extremely high-risk. Nothing here can
> guarantee profit — it is a risk-management and signal-detection toolkit.

---

## What it does

| Goal you asked for | How MemeRadar does it |
|---|---|
| **Find coins about to pump** | Scans trending Solana memecoins and scores each with a **pump score** (price acceleration + volume surge + buy/sell imbalance + youth). |
| **Flag pump & dumps** | A **dump-risk** score detects distribution: rollovers after a run, rising sell pressure, volume fading at the highs. Tokens are labelled by **market phase** (accumulation → markup → distribution → dump). |
| **Insider / whale / smart-money wallets** | A wallet-intelligence layer classifies wallets as **whale / insider / smart-money / retail**, tracks their net flow, and surfaces a live watchlist. |
| **Sell before losing everything** | A layered exit ladder: **stop-loss**, **trailing stop** (locks in profit, bails before the crash), **take-profit**, **dump-risk spike**, and **momentum reversal**. |
| **"Sell when multiple wallets sell — but only when sure"** | A **confirmed coordinated-sell** signal fires *only* when ≥ N distinct tracked smart wallets dump the same token inside a time window **and** the combined size is material. When it fires, open positions are exited immediately. |
| **Start with $10,000 paper** | The portfolio starts at exactly `$10,000` and simulates fills with slippage + fees. |
| **Professional dashboard** | A dark, real-time web dashboard: equity curve, KPIs, open positions, opportunity scanner, signals feed, whale watchlist, and trade history. |

---

## Quick start

```bash
# 1. install dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. run it
python run.py

# 3. open the dashboard
#    http://127.0.0.1:8000
```

That's it. With **no API keys** it already works end-to-end using the free,
key-less [DexScreener](https://dexscreener.com) API for live Solana market data.

### Run it 24/7 (DigitalOcean)

A scanner is most useful running around the clock. See **[DEPLOY.md](DEPLOY.md)**
for a step-by-step DigitalOcean Droplet + Docker Compose setup (with a persistent
data volume, optional HTTPS, and a dashboard login). TL;DR on a Docker host:

```bash
cp .env.example .env      # set DASHBOARD_PASSWORD
docker compose up -d --build
```

> **Dashboard login:** set `DASHBOARD_PASSWORD` in `.env` before exposing the
> dashboard publicly — it gates the controls (including **Reset**). Unset = no
> login, which is fine for localhost-only use.

### Optional: real on-chain wallet data

Token-level signals are 100% real out of the box. To make the **whale-holder
concentration** real (instead of estimated), add a free
[Helius](https://helius.dev) key:

```bash
cp .env.example .env
# then set HELIUS_API_KEY=...   (and restart)
```

See **“What's real vs simulated”** below for an honest breakdown.

---

## How the brain works

```
 ┌─────────────┐   ┌───────────┐   ┌──────────────┐   ┌───────────┐   ┌────────────┐
 │ DexScreener │ → │ Detector  │ → │ Wallet Intel │ → │ Strategy  │ → │ PaperTrader│
 │ (discovery  │   │ pump/dump │   │ whales/      │   │ entry +   │   │ $10k book, │
 │  + enrich)  │   │ /safety   │   │ multi-sell   │   │ exit ladder│  │ P&L, curve │
 └─────────────┘   └───────────┘   └──────────────┘   └───────────┘   └────────────┘
                          └──────────────┴── Scanner loop (every ~25s) ──┘
                                         ↓
                              FastAPI + WebSocket → Dashboard
```

### Entry logic
A token is bought only when **all** gates pass: strong pump score, acceptable
dump risk, structural safety (liquidity / age / churn), and net smart-money
inflow — and never while a confirmed coordinated sell is active.

### Exit logic — *“sell before the dump”*
Evaluated worst-case first, every scan, for every open position:

1. **Stop-loss** — hard floor (default −18%).
2. **Confirmed multi-wallet sell** — ≥ 3 smart wallets dumping ≥ $15k → exit now.
3. **Dump-risk spike** — detector says distribution is starting → exit.
4. **Trailing stop** — once +12% in profit, give back at most 14% from the peak.
5. **Take-profit** — absolute target (default +45%).
6. **Momentum reversal** — clear roll-over while in profit → bank it.

Every entry/exit posts an explained signal to the dashboard feed.

---

## Configuration

Everything is tunable in [`config.yaml`](config.yaml) — bankroll, scan interval,
position sizing, all entry/exit thresholds, and the wallet-intelligence
parameters (whale size, the coordinated-sell window and minimum wallet count,
etc.). Secrets come from `.env`.

A few of the most useful knobs:

```yaml
starting_balance_usd: 10000   # paper bankroll
take_profit_pct: 0.45         # lock profit at +45%
stop_loss_pct: 0.18           # hard floor at -18%
trailing_stop_pct: 0.14       # give back at most 14% off the peak
multi_sell_min_wallets: 3     # "only when sure" — N smart wallets selling
multi_sell_min_usd: 15000     # ...and a material combined size
auto_trade: true              # let the strategy open/close paper positions
```

---

## Dashboard

The dashboard is organised into four tabs:

### 📊 Overview
| Panel | Shows |
|---|---|
| **KPI strip** | Equity, total P&L, realized vs unrealized, win rate, open positions, cash deployed. |
| **Equity curve** | Live paper-portfolio value over time. |
| **Signals & alerts** | Pumps, dump warnings, whale buys, coordinated sells, and every entry/exit with its reason. |
| **Open positions** | Live P&L, peak price, and the active exit guard (e.g. “trailing · 6% off peak”). |
| **Whale / insider watchlist** | Most active tracked smart wallets and their net flow. |
| **Opportunity scanner** | Every scanned token ranked by opportunity, with pump / dump-risk / safety bars. Click any row to chart it. |
| **Trade history** | Closed paper trades with entry/exit, P&L, and exit reason. |

### 📈 Live Charts
Every scanned coin gets a **profile** (full DexScreener stats — price, all
change/volume windows, txns, liquidity/mcap/FDV, age, rug-authority status —
plus MemeRadar's pump/dump/safety/opportunity scores and reasons) with a toggle
to the **full DexScreener chart** (TradingView-powered candles, timeframes,
indicators). Each coin in the list has a 📈 button to jump straight to its
chart, or paste any pair/token address.

### 👛 Wallets
Organised into sub-tabs:
- **🚀 Loadouts** — young coins a strong team is accumulating (see below).
- **👛 Wallets** — one searchable, filterable table of every tracked wallet:
  sort by **richest→poorest**, **buy amount**, **date added**, win rate, or
  activity; filter by type; **search by address**.
- **👥 Cabals** — clusters of wallets that repeatedly trade the *same* coins.
- **📦 Bundles** — coordinated multi-wallet **buys** of the same coin, tiered by
  timing: **<1 min = synchronized** (strong), **<20 min = coordinated**,
  **same-day = minor**. Also pushed as alerts. (Most precise with real wallet
  data; the simulated feed flags frequently because its smart wallets are very
  active.)

- **🚀 Fresh loadouts** (top of the tab): young coins where a *strong team* of
  good wallets is accumulating early — the bullish, forward-looking inverse of
  the dump detector. Each shows a loadout score, the team, and the cabal behind
  it. Recency-ranked so it stays current.
- **Click any wallet** anywhere → a **profile** with holdings, PnL, win rate,
  and a modelled "career" equity chart. **Click any cabal** → a profile with
  members, combined stats, what they're *currently loading*, and their track
  record. *(PnL/holdings/career are modelled from market pressure and clearly
  labelled — real per-wallet trade history needs a paid indexer; current SPL
  holdings can be made real with a Helius key.)*

### 📣 Influencers
A curated watchlist of X/Twitter influencers / KOLs and their wallets, with what
each is currently trading. Because no API reliably maps a handle → wallet, this
list is **manual**: edit [`influencers.yaml`](influencers.yaml) to add verified
addresses. Ships with clearly-labelled demo entries.

Controls: **Pause/Resume** auto-trading, **Scan now**, and **Reset** the paper
portfolio back to $10,000.

---

## What's real vs simulated (honest breakdown)

| Component | Source |
|---|---|
| Token discovery, prices, volume, txns, liquidity, price-change windows | **Real** — DexScreener (free, key-less). |
| Pump / dump / safety scoring, phase labels | **Real** — computed from the above. |
| Paper fills, fees, slippage, P&L, equity curve | **Real** simulation against live prices. |
| Whale-holder **concentration** | **Real** with `HELIUS_API_KEY` (`getTokenLargestAccounts`); otherwise estimated from market structure. |
| Per-wallet **buy/sell events** (the named whale/insider wallets) | **Simulated** — a stable per-token roster whose actions are driven by the token's *real* buy/sell pressure and price action. Clearly labelled `simulated` in the UI. Live per-swap attribution needs a streaming indexer (a planned v2). |

The point: market signals and trade simulation are real; the individual wallet
*identities* are synthesised (and labelled as such) so the coordinated-sell
logic is fully demonstrable without paid streaming infrastructure.

---

## Tests

```bash
python -m pytest -q
```

Covers the detector scoring, the paper trader (fills/fees/persistence), and the
full entry + exit strategy ladder.

---

## Project layout

```
run.py                     # entry point: python run.py
config.yaml                # all tunables
app/
  config.py                # config loader (yaml + env)
  models.py                # domain dataclasses
  database.py              # SQLite persistence
  main.py                  # FastAPI app: REST + WebSocket + static
  data/
    dexscreener.py         # live Solana market data
    wallets.py             # Helius (real) + simulated wallet providers
  engine/
    detector.py            # pump/dump/safety scoring (pure, tested)
    wallet_intel.py        # whale/insider aggregation + coordinated-sell
    strategy.py            # entry gating + exit ladder (pure, tested)
    paper_trader.py        # $10k book, fills, P&L, equity curve
    scanner.py             # the loop that ties it together
  web/static/              # dashboard (index.html, app.js, styles.css)
tests/                     # pytest suite
```
