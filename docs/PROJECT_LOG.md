# Project Log — MEX Strategy

Complete record of how this project got here: what was tested, what was decided,
what broke, and what is still unknown. Written so that someone picking this up in
six months — including a future version of the author — can reconstruct every
decision without re-deriving it.

**Status: forward test running. NOT approved for live capital.**

| Phase | What happened | Outcome |
|---|---|---|
| 1. Validation | Full T0–T14 battery on 32 months of ETHUSDT 4H | **FAIL — conviction 25 %** |
| 2. Execution design | How to actually place the orders by hand | Trailing stop, frozen callback rate |
| 3. Forward test | This repo | Live since 2026-08-30 |

---

# Phase 1 — Backtest validation

A TradingView backtest showed +35.87 % to +41.72 % on ETHUSDT.P 4H over
Jan 2024 – Aug 2026, with per-trade expectancy invariant across three execution
models. That invariance suggested real edge rather than an execution artifact.
Nothing had been tested out-of-sample, against random entry, or for statistical
significance.

An independent bar-by-bar Python engine was built (no vectorbt, no backtrader)
and the full T0–T14 suite from the spec was run against it.

## Verdict: FAIL — conviction 25 %

**The entry signal is real. The advertised configuration is not deployable.**

Two different questions deserve two different answers:

| Question | Confidence |
|---|---|
| Does the MEX entry contain *some* real edge over random entry? | **~90 %** |
| Is the baseline configuration stable enough to trade live? | **~25 %** |

MEX beats random entry at the **99.7th percentile** and beats a shuffled-returns
null at **p < 0.001** — that part of the thesis survived every attack. But
out-of-sample expectancy collapsed to **21 % of in-sample**, the probability of
backtest overfitting is **0.567 (Mode B) / 0.738 (Mode C)**, and walk-forward
efficiency is **0.07**.

### Acceptance checklist — 5 of 7 pass, and the rule is "all must hold"

| # | Criterion | Result | |
|---|---|---|---|
| 1 | T0 replication passes | +42.03 % vs +41.72 % reference | ✅ |
| 2 | OOS expectancy ≥ 60 % of IS | **21.4 %** (B) / 4.5 % (C) | ❌ **FAIL** |
| 3 | Real PnL > 95th pct of random entry | **99.7th** percentile | ✅ |
| 4 | PBO < 0.5 | **0.567** (B) / **0.738** (C) | ❌ **FAIL** |
| 5 | PnL positive after removing top 5 winners | +9.65 (B) | ✅ |
| 6 | ≥ 60 % of symbols with positive expectancy | 85.7 % (18 of 21) | ✅ |
| 7 | Break-even cost ≥ 2× actual | **8.2×** | ✅ |

**KILL criterion 3 fired for Mode C** (PBO 0.738 > 0.70). Mode B at 0.567 sits
below the kill line but above the acceptance line.

## Baseline numbers (Mode B, the primary execution model)

| | Value |
|---|---|
| Trades | 118 (101 long / 17 fade short) |
| Net PnL | +42.03 % over 32 months = **+14.09 % / year** |
| Max drawdown | 7.95 % |
| PnL / DD | 3.72 |
| Profit factor | 1.95 |
| Win rate | 44.9 % |
| Expectancy | **+0.31 R** per trade |
| Avg hold | 20 hours (5 bars) |
| Time in market | 10.3 % |
| Sharpe / Sortino / Calmar | 1.54 / 1.28 / 1.77 |
| t-statistic | 2.20 (p = 0.030 raw) |

Data: 7,116 ETHUSDT 4H bars, 2023-06-01 → 2026-08-29, **0 missing bars**, from
`data.binance.vision` official archives. 21 symbols tested, 0 dropped.

## What each test found

| Test | Question | Finding |
|---|---|---|
| **T0** | Reproduces TradingView? | ✅ +42.03 % vs +41.72 %, 118 vs 117 trades. Engine validated. |
| **T1** | Survives out-of-sample? | ❌ IS expectancy 0.398 R → OOS **0.085 R** (34 trades, p = 0.287) |
| **T2** | Stable across rolling windows? | ❌ 86 % of windows profitable, but **WFE 0.07** — re-optimising actively loses money |
| **T3** | Plateau or fitted spike? | ⚠️ Baseline on a local peak for 4 of 7 parameters; no cliffs into loss |
| **T4** | Robust to trade ordering? | ✅ 5th-pct final equity 111.9. Expect **11 losing trades in a row**, tolerate 20 |
| **T5** | Outlier-dependent? | ⚠️ Top 5 winners = **77 % of PnL**; removing top 10 turns it negative |
| **T6** | Generalises across symbols? | ✅ **18 of 21** positive on unchanged parameters — not ETH-fitted |
| **T7** | Where does the edge live? | ℹ️ Almost entirely BTC bull regimes (**0.444 R vs 0.071 R**) |
| **T8** | Do added filters help? | ℹ️ F1/F2/F5 earn their place on ≥ 76 % of the universe; F3/F4/F6 do not |
| **T9** | Statistically significant? | ❌ t = 2.20 raw, but DSR p = 0.134 and **PBO 0.567 / 0.738** |
| **T10** | Survives real costs? | ✅ Break-even at **8.2×** actual cost; funding only 1.4 pp over 32 months |
| **T11** | What risk level? | ℹ️ 1.0 % default; 1.75 % ceiling for 15 % DD; **ignore Kelly's 25 %** |
| **T12** | Other timeframes? | ℹ️ 1D sample too small (11 trades); 18-symbol portfolio +215 % at 35 % DD |
| **T13** | Beats random entry? | ✅ **99.7th percentile** — the entry signal is real |
| **T14** | Lookahead or snooping? | ✅ None. Shifting signals forward degrades PnL, as it must |

## The three findings that matter most

**1. The signal is real but the configuration is fitted.** T13 and T14 clear the
entry logic of being noise. T1, T2 and T9 say the specific parameter set does not
generalise forward. These are compatible: a real but weak edge, wrapped in
parameters tuned to one sample.

**2. Walk-forward efficiency of 0.07 is the most damning number.** It means
parameters optimised on any 12-month window lose money in the following quarter.
Whatever the optimiser finds is not persistent.

**3. 77 % of profit comes from 5 trades out of 118.** Trend-following is
inherently outlier-driven, so this is a fragility measure rather than a
disqualifier — but it dictates that no honest evaluation is possible on a small
sample, and it sets the psychological requirement.

---

# Phase 2 — Execution design

The backtest defines the strategy. It does not say how a human places the orders.
This phase answered that, and every conclusion was measured rather than assumed.

## Finding: there is no take profit, and that is not an oversight

**118 of 118 backtest trades exited via the trailing stop.** Not one hit a
target. One level does both jobs: below entry it is the stop loss, above entry it
becomes a profit lock.

The cost is visible: winners touch **+6.45 %** at their peak but exit at
**+3.50 %** — about 3 percentage points handed back on every winner. That is the
unavoidable price of a system with no target.

## Where the exits actually land

| Winners exit at | % of entry price | in R |
|---|---|---|
| 25th percentile | +1.02 % | 0.32 R |
| **Median** | **+2.06 %** | 0.79 R |
| 75th percentile | +4.50 % | 1.74 R |
| 90th percentile | +8.53 % | 2.82 R |
| Largest | +21.02 % | 7.46 R |

Only **44.9 %** of trades ever reach 1R in favour; 19.5 % reach 2R; **4.2 % reach
4R**. Those few carry the 77 %.

## What happens if a take profit is forced

Tested two ways, since a trader whose platform demands both TP and SL needs to
know the price.

**With the trailing stop still running:**

| TP | = % of price | PnL | Max DD | PnL/DD |
|---|---|---|---|---|
| **none** | — | **+42.03 %** | 7.95 % | **3.72** |
| 1.0 R | 2.75 % | +28.60 % | 5.89 % | 3.68 |
| 2.5 R | 6.88 % | +40.46 % | 7.95 % | 3.58 |
| 4.0 R | 11.0 % | +42.92 % | 7.95 % | 3.81 |

A tight TP raises win rate and destroys a third of the profit. A wide TP changes
almost nothing because it almost never fires.

**As a pure bracket order — static SL + TP, no trailing at all:**

| TP | PnL | **Max DD** | **PnL/DD** | Win rate |
|---|---|---|---|---|
| 2.0 R | +26.35 % | 13.43 % | 1.41 | 41.0 % |
| **4.0 R** | **+39.17 %** | **23.16 %** | **1.04** | 28.3 % |
| no TP, static SL | −16.62 % | 50.90 % | −0.24 | 2.7 % |
| *trailing (baseline)* | *+42.03 %* | *7.95 %* | ***3.72*** | *44.9 %* |

Profit can almost be matched, but **drawdown nearly triples**. The trailing stop
is not what generates the profit — it is what contains the risk.

Note also how unstable the bracket results are: 5R gives +21 %, 6R gives **−6 %**,
8R gives +4 %. Swings that large from small parameter changes are noise, and are
themselves an argument against the bracket approach.

## The chosen configuration

A manual trader cannot move a stop every four hours. Binance's native
**Trailing Stop** order can, if given a callback rate. Tested:

| Configuration | Trades | PnL/year | Max DD | PnL/DD | Risk per trade |
|---|---|---|---|---|---|
| ATR trail, adjusted every 4 h manually | 118 | +14.10 % | 7.95 % | 3.72 | 1.00 % |
| **Callback = 1.5×ATR/price, frozen at entry** | 114 | **+14.24 %** | 8.34 % | **3.55** | **1.00 %** |
| Fixed 2.00 % callback, sized on 2.00 % | 131 | +10.29 % | 8.38 % | 2.61 | 1.00 % |
| Fixed 2.75 % callback (the *average*) | 114 | +7.62 % | 7.66 % | 2.20 | 1.00 % |
| Fixed 2 % callback, sized on ATR | 131 | +9.98 % | 8.23 % | 🔴 0.4–1.8 % |

**Chosen: callback rate computed per trade as `1.5 × ATR(14) ÷ entry price`,
frozen at entry.** One order, placed once, never touched — and it matches the
manually-adjusted version almost exactly.

### Why a fixed callback rate is wrong

Using the *average* (2.75 %) for every trade **halves annual return** — and in
the 2026 out-of-sample window it turns the strategy **negative (−1.07 %)** where
the per-trade version still returns +2.26 %.

The per-trade callback ranges **1.09 % → 5.24 %** (median 2.70 %). 72 % of trades
differ from 2.75 % by more than 0.25 pp. ATR sets two things at once — stop
distance *and* position size — so a fixed value gets both wrong in the same
direction simultaneously.

Yearly averages drift too: 2.82 % (2024), 2.97 % (2025), **2.40 % (2026)**. A
constant taken from 2024–25 is systematically too wide for 2026.

## What "1 % risk" actually means

Risk is 1 % of capital **at the moment of entry only**. After that the ratchet
means it can only fall.

Measured across 118 trades:

- Average realised loss: **0.55 R = 0.55 % of capital**, not 1 %
- Worst single loss: **1.09 R** — 1.00 R of price plus ~0.09 R of commission and slippage
- Trades losing more than 1.0 R: **3 of 118**
- Trades exiting at the original stop: **0 of 118** — the trail had always moved

No gap ever jumped the stop in this sample. That is not a guarantee; a
liquidation cascade can still do it.

## Regime behaviour

Real trades from the backtest, grouped:

| Regime | n | **Median** | **Mean** | Win rate | Total PnL |
|---|---|---|---|---|---|
| Bull (BTC > SMA200, long, ADX > 20) | 41 | +0.062 R | **+0.468 R** | 51.2 % | +22.88 |
| Bear (fade short) | 8 | 0.000 R | +0.059 R | 50.0 % | **+0.60** |
| Sideways (ADX < 20) | 46 | **−0.187 R** | **+0.354 R** | 43.5 % | +19.02 |

**The gap between median and mean is the whole story.** A typical trade is worth
roughly nothing; in chop the typical trade *loses*. All the profit lives in a few
large outcomes. This is T5 showing up again at regime level, and it is why no
judgement is possible from a small number of trades.

Two secondary observations: the fade-short side produced **+0.60 USDT in 32
months** — harmless but negligible. And "trend following" is a misleading label,
since sideways markets produced nearly as much as bull markets; what the strategy
actually catches is **volume expansion**.

---

# Phase 3 — Forward test infrastructure

This repo. GitHub Actions screens ETHUSDT on every closed 4H bar, sends a Telegram
message **only when something happens**, and commits every observation back to the
repo as CSV.

## Design decisions

| Decision | Reason |
|---|---|
| Exit = Mode B trailing stop, no TP | 118/118 backtest exits were the trail; PnL/DD 3.72 vs 1.04 for a bracket |
| Callback frozen at entry = 1.5×ATR/price | Matches manual-adjustment performance with a single order |
| Callback recomputed **per trade** | A fixed value halves annual return and goes negative in 2026 |
| Fade shorts kept enabled | Matches the validated baseline; disabling it slightly worsened results (T8-F6) |
| Signal carries R and callback %, not quantity | User sizes against whatever capital is current |
| Entry valid within ±0.5R, expires after 8 h | GitHub Actions cron can be delayed by hours |
| Run hourly, not 4-hourly | Delayed runs catch up; processed bars are skipped, so no duplicate messages |
| State committed back to the repo | Free audit trail, no external database |

## The parity guarantee

`tests/test_strategy.py` replays **4,000 real Binance perp bars** and asserts the
live signal arrays are **bit-identical** to those from the validated backtest
engine — 131 long and 13 fade signals, zero differences.

It also pins the properties that are easy to break silently:

- the trail never moves against the position (ratchet)
- every exit is justified by the **previous** bar's trail level (no lookahead)
- no exit on the entry bar, since the stop level depends on that bar's own ATR
- `callback % == 1R / price` and `initial stop == entry ∓ 1R`, always
- indicators match hand-computed Wilder values

**If this file goes red, the repo has drifted from the validated strategy and the
signals should not be trusted.**

## Constraint: Binance's API is unreachable

`fapi.binance.com` returns **HTTP 451** from GitHub-hosted runners (US IP ranges
are geo-blocked) and times out from the author's ISP. Confirmed live on a runner.
The backtest's own data source is therefore unavailable to the forward test.

Measured over 3,636 overlapping bars against Binance perp:

| Source | Long signals matched | Price error | ATR error |
|---|---|---|---|
| **`data-api.binance.vision`** (Binance SPOT) — primary | **110/115 = 96 %** | 0.046 % | 1.91 % |
| `api.gateio.ws` (Gate.io perp) — automatic failover | 102/115 = 89 % | 0.008 % | 0.88 % |

Signal agreement was prioritised over price precision. **This is permanent
tracking error: roughly 1 signal in 25 will differ from what the backtest would
have produced.** The source used is recorded on every logged row so it can be
separated during evaluation, and CI re-checks whether `fapi` has become reachable.

## What gets recorded

| File | Contents |
|---|---|
| `state/events.csv` | 45 columns — one row per SIGNAL / ENTRY / EXIT, with the full indicator snapshot at that bar |
| `state/trades.csv` | 36 columns — one row per completed round trip; the evaluation table |
| `state/runs.csv` | 17 columns — one row per run: liveness, latency, data source, delivery status |

Columns are deliberately wide. A live forward test cannot be re-run, so anything
not captured at the time is gone permanently.

Four columns are left blank for the human: `actual_fill_price`, `actual_qty`,
`actual_exit_price`, `notes`. **`actual_fill_price` − `entry_price` is real
slippage** — a number no backtest can produce.

`signal_to_send_minutes` records delivery latency on every signal, so the
question *"did late signals perform worse?"* can be answered later rather than
guessed at.

---

# Incidents

## 1. Credential leak (2026-08-31) — resolved

A service-account JSON key was pasted into a secret expecting a URL. The code
printed the failing value in an exception message, and **GitHub's secret masking
does not cover multi-line values**, so a private key appeared in full in the
public repository's Actions log.

Contained within minutes: the key was rotated, the affected run was deleted, and
all recent runs were scanned to confirm no other copy remained.

**Fixes:** exceptions are now reported by type only, never message. Secret values
are shape-validated (`https://`, single line, < 2048 chars) before ever being
handed to `requests`. The only field ever echoed is `client_email`, which is an
identifier rather than a credential and is needed to share the spreadsheet.

**Lesson worth keeping:** never let a library exception carrying a secret reach a
log, and never assume GitHub masking will save you — it will not for multi-line
values.

## 2. State commit conflicts — resolved

Two workflows appending rows to the same CSV produce a rebase conflict that git
cannot auto-resolve. The original retry loop made it worse: once a rebase left
unmerged files, every subsequent `git pull` failed, so all five attempts were
guaranteed to fail. One heartbeat run failed exactly this way.

**Fix:** `state/*.csv` now use git's **union** merge strategy — appropriate for
append-only logs, keeping rows from both sides so nothing is lost. Only
`position.json` needs a decision, and there the currently-running job wins since
it has processed the latest bar. Verified live: a rejected push rebased cleanly
and succeeded on the first retry.

## 3. GitHub cron is unreliable — mitigated

Measured over the first 16 hours: of roughly 16 hourly schedules, **only 4
actually ran**. Worst gap **6 hours**; median delay from bar close **92 minutes**,
worst 205 minutes.

Signals expire after 8 hours, so a 6-hour gap comes dangerously close to a signal
never being delivered. Note the record itself was never at risk — catch-up worked,
with one run processing two bars at once — the risk was to *timeliness of
delivery*.

**Fix:** attempts are now clustered in the hour after each 4H close (four
attempts at :07, :23, :39, :53 past 00/04/08/12/16/20 UTC) with hourly attempts
as backup — 42 scheduled attempts per day instead of 24, four per bar instead of
one. Odd minutes are used deliberately: round minutes are the most contended slots
in GitHub's queue and the most frequently dropped.

**If latency stays high**, the options are a self-hosted runner, a small VPS with
ordinary cron, or relaxing `expiry_hours` — the last changes agreed behaviour and
is the user's call, not an implementation detail.

## 4. Environment constraints (pre-existing)

- `pyarrow._parquet` is blocked by Windows Application Control, which breaks
  `import pandas` outright. Worked around with an import shim; all caching uses
  gzipped CSV instead of parquet.
- Python 3.14 / pandas 3.0 on the development machine; the runners use 3.11.

---

# What remains unknown

Stated plainly, because a forward test that pretends to more certainty than it
has is worse than none.

1. **Whether the OOS collapse was regime or decay.** The 2026 window had 34
   trades and a p-value of 0.287 — too few to distinguish "the edge decayed" from
   "eight months of unfavourable regime". T7 showed the edge lives in BTC bull
   markets; 2026 was not one. The forward test is the only way to separate these.

2. **Whether the tracking error matters.** 96 % signal agreement is measured, but
   the 4 % that differs has never been evaluated for whether it is systematically
   better or worse.

3. **Real slippage.** Every backtest number assumes 1 tick. The `actual_*` columns
   exist to replace that assumption with evidence.

4. **Execution discipline.** The backtest assumes every signal is taken. A human
   who skips the scary ones gets a different, and usually worse, distribution —
   particularly for a strategy where 5 trades carry 77 % of the profit.

5. **Whether 11 consecutive losses can be sat through.** T4 says to expect them
   and to tolerate 20. That is a psychological question no backtest can answer.

---

# How to evaluate this

**Do not judge from the first 10–20 trades.** The median trade is worth roughly
nothing by design. At ~3.7 trades/month a meaningful sample needs **6 months
minimum, 12 preferably**.

Benchmarks from the backtest:

| Metric | Backtest baseline |
|---|---|
| Expectancy | **+0.31 R** per trade |
| Win rate | ~45 % |
| Average hold | 20 hours |
| Trades per month | 3.7 |
| Max drawdown | ~8 % |
| Worst losing streak | 11 (tolerate 20) |

The decisive comparison is `AVERAGE(result_R)` against **+0.31 R**. If after 30+
trades it sits far below, the forward test is confirming the T1/T9 suspicion —
**and that is a valuable finding, not a failure.** The purpose of this repo is to
find out, not to be right.

**Never re-tune parameters after seeing forward-test results.** That destroys the
only honest evidence this project has. Any change to `config.yaml` must be
recorded in `CHANGELOG.md` with a date and a reason.
