# Trenchr — Detection Strategy & Domain Knowledge

This is the playbook the bot is built around: how Solana memecoin pump-and-dumps
actually work, how to spot insider / whale / smart-money / cabal wallets, the
red/green flags that separate a rug from a runner, and how to *sell before the
dump*. Findings tagged **[verified]** were cross-checked by an adversarial
research pass (≥2 of 3 skeptical reviewers had to fail to refute them);
**[heuristic]** items are widely-used community rules of thumb that are sensible
but not independently proven. Sources are listed at the bottom.

> ⚠️ Memecoins are a hostile, negative-sum environment. The single most
> important edge is **not buying garbage**. The numbers below explain why the
> bot filters so aggressively.

---

## 1. The base rates (why filtering is everything)

- **98.6% of Pump.fun tokens collapse** into worthless pump-and-dumps shortly
  after launch. Of 7M+ tokens deployed (with ≥5 trades), only ~**97,000** ever
  hold liquidity above **$1,000**. **[verified]** [S1]
- In a study of **388,000 Raydium pools, ~93% (361,000)** showed **soft-rug**
  characteristics. **[verified]** [S1]
- Raydium hard rug pulls are reliably detectable as **near-complete liquidity
  withdrawals** — a **≥90% liquidity-sweep** is the practical threshold. **[verified]** [S1]

**Implication for the bot:** treat every token as guilty until proven otherwise.
A high pump score is *necessary but not sufficient* — it must clear hard safety
gates first. Survivorship is the whole game.

---

## 2. Anatomy of a Pump.fun pump-and-dump

### Lifecycle
1. **Launch on a bonding curve.** Pump.fun mints the token against a bonding
   curve (price rises as people buy). No traditional LP yet.
2. **Bonding-curve fill / graduation.** Once the curve accumulates enough SOL,
   the token "graduates": liquidity migrates to a DEX (Raydium/Pump's AMM). This
   migration is **observable on-chain in real time** by subscribing to the
   migration wrapper program (`39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg`),
   which emits a `Migrate` event with `baseMint`, `quoteMint`, decimals, and the
   deposited amounts. **[verified]** [S2]
3. **Markup.** Post-graduation hype + influencer calls drive the parabolic move.
4. **Distribution.** Insiders/snipers/bundlers quietly sell into strength;
   buy/sell ratio flips, volume fades at the highs.
5. **Dump.** Coordinated exit; price collapses, often a liquidity pull.

### Measurable signatures per phase (computable from DexScreener)
| Phase | Signature the bot can compute |
|---|---|
| Accumulation | flat-to-up price, buys ≥ sells, rising but not parabolic volume |
| Markup | strong short-window price change (m5/h1), volume surge vs 24h-avg-hour, buy-dominated txns |
| Distribution | big h6 run **but** m5/h1 rolling over, sell txns rising, volume fading at highs |
| Dump | sharp negative m5/h1 after a run, sell-dominated, liquidity dropping |

Trenchr's `detector.py` encodes exactly these into `pump_score`, `dump_risk`
and a `phase` label.

---

## 3. The actors (insiders, snipers, bundlers, whales, smart money, cabals)

Platforms that already classify Solana wallets give us the canonical taxonomy.
**GMGN** labels wallets on a token as: **smart money, KOL/VC, whales, new
wallets, snipers, large holders, developers, followed wallets, and "rat
warehouses" (insider clusters).** **[verified]** [S3]

| Actor | On-chain footprint |
|---|---|
| **Developer** | created/funded the mint; holds dev allocation; first to add LP |
| **Sniper** | buys in the very first block(s) at the curve's lowest prices |
| **Bundler** | funds many wallets from one source and buys in the same bundle/tx to fake distribution |
| **Insider cluster ("rat warehouse")** | a group of related wallets accumulating pre-pump then dumping together |
| **Whale** | very large position relative to the pool |
| **Smart money** | consistently profitable trader (see §4) |
| **Cabal** | a coordinated group that repeatedly enters and **dumps the same coins together** |

Trenchr's **Wallets tab** maps onto this directly: whales, insiders, smart
money, pump-&-dump actors, and **cabal groups** (detected by repeated co-dumping,
which is the behavioural definition of a "rat warehouse"/cabal).

---

## 4. Defining "smart money" (so the bot can rank wallets)

- **Nansen** exposes smart-money labels such as **"Smart Trader", "Fund", and
  "30D Smart Trader"**. **[verified]** [S4]
- Their API lets you **filter wallets by realized PnL**, e.g. **`pnl_usd_realised ≥ $1,000`**. **[verified]** [S4]
- A practical **win-rate cutoff for copy-worthy wallets is `> 50–60%`**
  (`win_rate > 0.5`–`0.6`). **[verified]** [S4]

**Operational definition the bot uses:** a wallet is "smart money" if its
**win rate ≥ ~55–60%** *and* it is **net profitable** (positive realized PnL).
Trenchr's `smart_money_min_winrate` config (default `0.55`) implements this;
the Wallets tab's "Smart money" category requires win-rate ≥ threshold **and**
positive net flow.

> ⚠️ Caution that **did not survive** verification (treat as unreliable): a fixed
> "whales hold $1M+, alert on $50K txns" rule, and "10+ smart wallets buying in
> 48h = conviction" were **refuted** by the skeptical pass — don't hard-code
> those exact numbers. Use relative, per-token sizing instead. [S5]

---

## 5. Rug / honeypot red flags (hard gates)

These are the checks that keep you out of the 93–98% that go to zero. Where a
threshold survived verification it's marked; otherwise it's a community standard.

| Check | Red flag | Notes |
|---|---|---|
| **Mint authority** | **not renounced** → dev can mint infinite supply | classic hard-block. Readable free from Solana RPC (`getAccountInfo`, `mintAuthority`). **[heuristic, strong]** |
| **Freeze authority** | **not renounced** → dev can freeze your wallet (honeypot) | hard-block. Readable free (`freezeAuthority`). **[heuristic, strong]** |
| **LP burned/locked** | LP **not** burned or locked → liquidity can be pulled | a ≥90% liquidity withdrawal is the rug signature. **[verified for the sweep]** [S1] |
| **Top-10 holder concentration** | very high (community line often **>50%**) | the exact 50% line did **not** survive verification — use as a *risk weight*, not a hard cutoff. **[heuristic]** [S5] |
| **Bundle %** | high % of supply bought in the launch bundle | indicates manufactured distribution / coordinated dump risk. **[heuristic]** |
| **Sniper %** | large supply % taken in block 0–1 | snipers dump first. **[heuristic]** |
| **Dev holdings** | dev still holds a large % | overhang / dev-dump risk. **[heuristic]** |
| **Liquidity floor** | below a few-$k of liquidity | thin pools = rug-prone; recall only ~97k of 7M ever cleared $1k. **[verified base rate]** [S1] |
| **Volume/liquidity ratio** | extremely high churn | wash-trading / hot-potato. **[heuristic]** |

**Status in Trenchr:** liquidity floor, churn, supply-overhang (liq/FDV), and
age gates are live in `detector.py`. Mint/freeze-authority and LP-burn checks are
the highest-value *next* upgrade — they're readable **for free** from the public
Solana RPC (validated: `getAccountInfo` returns mint/freeze authority; batchable
via `getMultipleAccounts`), and with a Helius key the top-holder concentration
becomes exact.

---

## 6. Green flags (what a *quality* setup looks like)

- Mint **and** freeze authority **renounced**; LP **burned/locked**.
- Holder distribution broadening (not concentrating) as price rises.
- **Smart-money / KOL inflow** rather than only fresh/retail wallets.
- Healthy buy/sell balance with volume **expanding** into the move (not fading).
- Survived the first chaotic minutes (age past the launch-snipe window) but still
  young enough to have upside.

---

## 7. Selling before the dump (exit playbook)

The realistic edge is risk management, not perfect tops. Common practitioner
patterns (community **[heuristic]**, encoded in `strategy.py`):

1. **Scale out / take-profit ladder.** Sell tranches into strength (e.g. take
   1/3 at +50–100%, more at +200%+) so you bank profit and "play with house
   money." Trenchr uses a take-profit target + trailing stop today; a tranche
   ladder is a natural enhancement.
2. **Trailing stop.** Once in profit, cap give-back from the peak (Trenchr:
   arm at +12%, trail 14% off the high). This is the core "sell before the
   crash" mechanism.
3. **Hard stop-loss.** Non-negotiable floor for capital preservation.
4. **Detect coordinated/insider selling early.** When multiple tracked
   smart/insider wallets (a cabal) sell the *same* token in a short window,
   exit immediately — this is Trenchr's **confirmed multi-wallet-sell** gate
   ("only act when you know for sure": ≥N distinct smart wallets, ≥$X combined).
5. **Phase/momentum reversal.** Exit when the detector flips a held position to
   `distribution`/`dump` with negative momentum while you're up.

---

## 8. Mapping to the code

| Knowledge | Where it lives |
|---|---|
| Phase signatures, pump/dump scoring, churn/liquidity/age safety | `app/engine/detector.py` |
| Smart-money definition, whale/insider/cabal categorisation, coordinated-sell | `app/engine/wallet_intel.py` |
| Entry gates + exit ladder ("sell before the dump") | `app/engine/strategy.py` |
| Wallet taxonomy surfaced (whales/insiders/smart money/pump-&-dump/cabals) | Wallets tab |
| KOL / influencer wallets | Influencers tab + `influencers.yaml` |

### Highest-value next upgrades (research-backed)
1. **On-chain rug gates** (free): batch `getMultipleAccounts` each scan → block
   tokens with live **mint/freeze authority**; flag unburned LP. Directly
   attacks the 93–98% failure rate. [S1]
2. **Graduation listener**: subscribe to the migration program to catch tokens
   the moment they hit Raydium. [S2]
3. **Real smart-money PnL**: with Helius/Birdeye/Nansen, rank wallets by realized
   PnL + win-rate instead of simulated win-rate. [S4]

---

## Sources
- **[S1]** Solidus Labs — *Solana Rug Pulls & Pump-and-Dumps* report
  (98.6% collapse rate; 93% soft-rug; 90% liquidity-sweep threshold).
  https://www.soliduslabs.com/reports/solana-rug-pulls-pump-dumps-crypto-compliance
- **[S2]** Chainstack — *Listening to Pump.fun migrations to Raydium*
  (migration wrapper program + `Migrate` event).
  https://docs.chainstack.com/docs/solana-listening-to-pumpfun-migrations-to-raydium
- **[S3]** GMGN docs — token-page wallet classification (smart money, KOL/VC,
  whales, snipers, developers, rat warehouses).
  https://docs.gmgn.ai/index/token-page-chart-multicharts-activity-trading-system
- **[S4]** Nansen docs — copytrading top wallets (Smart Money labels; realized
  PnL ≥ $1,000 filter; win-rate > 0.5–0.6).
  https://docs.nansen.ai/guides/templates/complex-use-cases/use-case-4-copytrading-top-performing-wallets
- **[S5]** Nansen blog — tracking Solana wallets (claims about fixed $1M whale /
  $50K alert thresholds and 50% top-10 / 48h clustering that did **not** survive
  adversarial verification — listed here as *cautionary*).
  https://www.nansen.ai/post/how-to-track-solana-wallets-complete-guide-for-smart-money-analysis

*Note: the research pass was interrupted by a session limit before the final
synthesis step, so a few additional claims (sniper timing, bonding-curve flag
mechanics, specific high-return base rates) remain unverified and were
deliberately excluded or flagged above.*
