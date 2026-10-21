# Trading Bot (paper trading only)

An automated trading bot that runs for free on GitHub Actions and trades **paper** (pretend-money) Alpaca accounts. It never touches real money. A web dashboard shows everything it does, compares the strategies side by side, and can replay them over past days.

**Current release: 8.0** (TJR + Trend edition)

## What it does

- Runs **three strategies at once**, each on its own Alpaca paper account (a free Alpaca login allows three).
- All three watch the same eight stocks: **AAPL, MSFT, SPY, QQQ, NVDA, TSLA, AMD, PLTR**.
- Every entry is a **limit order** placed just past the last price, with a stop-loss and take-profit attached. If it has not filled within 90 seconds it is cancelled, so the bot never chases a fast move.
- Each account has a daily loss limit of 3% (new trades stop for the day).
- Every closed trade is recorded with its profit or loss, **an estimate of what slippage and fees would have cost in a real account**, how much it risked, the result in multiples of that risk, and the reason the bot took it.

| Strategy | Position style | What it does |
|---|---|---|
| **RSI Reversion** | One small position per stock (12.5% of the account each) | Goes long when RSI(14) is oversold and short when it is overbought, with a small stop-loss and take-profit. Settings are fixed. A nightly tuner only *suggests* changes. |
| **TJR Liquidity Sweep** | The whole account in **one position at a time**, long or short, under an hour | Waits for price to run the previous day's (or an earlier) high or low and snap back, for the 5-minute chart to shift structure the other way, and for price to return into a 1-minute fair value gap. Enters in the new direction with the stop beyond the sweep. Take-profit at 2x the risk. Everything is closed 10 minutes before the close. |
| **EMA + VWAP Trend** | The whole account in **one position at a time**, long or short, same day | Trend following. In an uptrend (price above the day's VWAP, 9-period average above the 21-period average on 5-minute candles) it buys the first dip to the fast average once a 1-minute bar turns back up. Downtrends are the mirror image (short). Stop beyond the dip, take-profit at 2x the risk, closed before the close. |

If several stocks set up at the same moment, the two whole-account strategies take only the best one.

### How big is "the whole account"?

The two new strategies size each trade so that hitting the stop-loss loses **0.5% of the account** (the default), and never more than **95%** of the account is put into one position. When the stop is close, that means a position close to the whole account. When the stop is wide, the position is smaller than the whole account, because the 0.5% rule wins. On a $5,000 account, for example, a stock with a stop 0.2% away gets about the full account, while a stop 1% away gets about half of it.

You can raise or lower the risk per trade. **The bot will never risk more than 2% per trade, whatever the settings file says.** See "Changing things".

### Making paper results more realistic

Paper accounts fill at clean prices, which flatters fast strategies. The bot does these things to narrow the gap:

- **Limit-order entries** that are cancelled if not filled in 90 seconds (see above).
- **Estimated costs on every trade**: 0.02% slippage on entry, 0.02% on a market exit, 0.05% on a stop-loss exit, nothing on a take-profit, plus approximate US sell-side regulatory fees. The dashboard shows results both before and after these costs. They are estimates, not a promise of what a real broker would charge.
- **A 3-losing-trades-in-a-row pause** for the two new strategies: no new trades for the rest of that day.
- **Early-close days** (for example the day after Thanksgiving) are read from Alpaca's own clock, so the bot flattens before the real close.
- **SPY buy-and-hold** is drawn on the Compare chart so you can see whether a strategy beats just owning the market.
- **A sample-size label** next to each strategy: fewer than 30 closed trades is "Too early to judge", 30 to 99 is "Still building", and 100 or more is "Large enough to read".

## The dashboard

`https://greysonbrindley727-wq.github.io/Trading-bot-V6/`

- **Compare** tab: every strategy as a percent-return line on one chart, all starting at 0% on the same date, with SPY buy-and-hold as a dashed line. A switch flips between "As reported" and "After estimated costs". A side-by-side table shows return (before and after costs), worst drop, win rate, closed trades, average win and loss, average result in risk units, profit factor, **long trades versus short trades**, estimated costs, account value and open positions.
- One tab per strategy: account value, profit and loss by day, how the strategy decides, a live reading for each stock, open positions, every order (including cancelled ones), closed trades, every check the bot ran, and (for RSI Reversion) the tuner's nightly suggestions. Each closed trade has a **Why and details** drawer showing what the bot saw when it entered, the risk, the result after costs, and (for TJR) the sweep and gap levels.
- **Backtest** tab: replays all three strategies over past days (see below).
- A strategy with no API keys yet shows a "Not switched on yet" screen with the steps, and the others carry on.
- A strategy paused after three losing trades shows "Paused for today".

The data refreshes about every 5 minutes while the bot runs. The bot publishes its data to a branch called `dashboard-data`, rewritten as a single commit each time so it never fills up the repo's history.

## The backtest

In the repo, open **Actions**, choose **Backtest**, press **Run workflow**. The defaults (60 trading days, $5,000) are a good start. It takes roughly 10 to 30 minutes, then saves `backtest/results.json` to the repo and the **Backtest** tab shows it after a refresh.

It replays each strategy on past 1-minute prices using the same rules, whole-share sizing, limit-order entries and estimated costs as the live bot, then shows return before and after costs, worst drop, trade counts, win rate, profit factor, long versus short, and SPY buy-and-hold for the same days. A "Does it hold up?" table compares the first 70% of the days with the last 30%, so a strategy that made all its money early gets flagged.

A backtest is a rehearsal, not a promise. The free price feed (IEX) shows only part of the market's volume, and a backtest cannot know what a real order would have done.

## The nightly tuner (suggest only)

The tuner no longer changes `config.json`. Each night it tests a grid of RSI settings on the last 30 days of 1-minute prices for all eight stocks, after estimated costs, ignoring any combination with fewer than 20 trades. It records the current settings and what it would pick in `tuning_log.csv` and `tuning_suggestion.json`, and the dashboard's Tuning tab shows both and how often it would have changed something. It only calls a change worthwhile if it beats the current settings by at least 1 percentage point.

Why: a tuner that rewrites the strategy every night can chase a lucky stretch and throw away settings that were working. If, after a few weeks, its suggestion is stable, you can copy the suggested numbers into `config.json` yourself.

## Files and versions

| File | Version | What it is |
|---|---|---|
| `bot.py` | 8.0.0 | The trading bot. One long job runs all three strategies. |
| `strategies.py` | 3.0.0 | The strategies, the cost estimate and the position sizing. |
| `indicators.py` | 2.0.0 | Indicator math and the sweep / fair value gap / trend pattern finders. |
| `publisher.py` | 2.1.0 | Writes the dashboard's data files and publishes them. |
| `tune.py` | 2.0.0 | The nightly tuner (suggest only). |
| `backtest.py` | 1.0.0 | The backtest engine. |
| `index.html` | 3.0.0 | The dashboard website. |
| `config.json` | reset | RSI Reversion settings. Reset to the defaults in this release. |
| `config_tjr-sweep.json`, `config_ema-vwap.json` | optional | Setting overrides for the two new strategies (see below). |
| `tuning_log.csv`, `tuning_suggestion.json` | auto | The tuner's nightly record. |
| `backtest/results.json` | auto | The latest backtest. |
| `requirements.txt` | 1.0.0 | Python packages the bot needs. |
| `.github/workflows/trading-bot.yml` | 8.0.0 | Starts the bot on weekdays (two scheduled starts a day). |
| `.github/workflows/tune.yml` | 2.0.0 | Runs the suggest-only tuner after the market closes. |
| `.github/workflows/backtest.yml` | 1.0.0 | Runs the backtest when you press the button. |

Opening Range Breakout and RSI(2) Pullback are still in `strategies.py` under a "Retired strategies" heading, switched off. To bring one back, add its class to `ALL_STRATEGIES` and give it a pair of secrets named after its `key_suffix`.

## Setup and the reset

### Secrets

The workflow accepts the old secret names, so nothing needs renaming. Either name works:

| Strategy | New secret names | Old names that still work |
|---|---|---|
| RSI Reversion | `APCA_API_KEY_ID_RSI`, `APCA_API_SECRET_KEY_RSI` | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` |
| TJR Liquidity Sweep | `APCA_API_KEY_ID_TJR`, `APCA_API_SECRET_KEY_TJR` | `APCA_API_KEY_ID_ORB`, `APCA_API_SECRET_KEY_ORB` |
| EMA + VWAP Trend | `APCA_API_KEY_ID_TREND`, `APCA_API_SECRET_KEY_TREND` | `APCA_API_KEY_ID_RSI2`, `APCA_API_SECRET_KEY_RSI2` |

If you keep the old secret names, the account that used to run Opening Range Breakout now runs TJR, and the account that used to run RSI(2) now runs the Trend strategy.

### Resetting the paper accounts to $5,000

Starting every account at the same $5,000 keeps the comparison fair, and shorting on a margin account needs more than $2,000 (a $1,000 account cannot short at all).

1. **Screenshot the Opening Range Breakout and RSI(2) tabs first** if you want to keep their results. They disappear from the dashboard after the reset.
2. In Alpaca, open each paper account, find the **Reset account** option, and choose $5,000. If it offers no amount, or you cannot reset it to $5,000, delete the account and open a new one with $5,000, then generate new API keys and update the repo secrets with them.
3. In the GitHub repo, open **Branches** and **delete the `dashboard-data` branch**. The bot recreates it on its next run, so the dashboard starts from a clean $5,000 for every strategy.
4. Upload the new files (see below), then run the **Trading Bot** workflow once.

### Pattern day trading

The TJR and Trend strategies make several same-day trades. The old US pattern-day-trader rule needed $25,000 for that. The SEC approved removing it in 2026, but brokers have until October 2027 to switch over, and it is not confirmed how a paper account behaves. If Alpaca rejects orders with a day-trading message, it will show on the dashboard's Orders tab, and the account would need to be larger or the strategies slowed down.

## Changing things

- **Risk per trade (TJR and Trend):** create `config_tjr-sweep.json` or `config_ema-vwap.json` containing, for example, `{"risk_per_trade_pct": 0.01}` for 1%. The default is 0.5%. The bot **clamps it to 2%** in the code, so a larger number is treated as 2%.
- **Other settings for those strategies:** put only the numbers you want to change in the same file, for example `{"target_r": 3, "max_hold_minutes": 45}`. The setting names are the `default_params` of each class in `strategies.py`.
- **RSI Reversion settings:** `config.json`. Nothing rewrites it now.
- **Watchlist:** `WATCHLIST` near the top of `bot.py`. To give one strategy its own list, fill in `WATCHLISTS`, for example `WATCHLISTS = {"tjr-sweep": ["SPY", "QQQ", "NVDA"]}`.
- **Daily loss limit:** `MAX_DAILY_LOSS_PCT` in `bot.py`. **Entry timeout:** `ENTRY_TIMEOUT_SECONDS`.
- **Cost estimate:** `COSTS` near the top of `strategies.py`.
- **Add a new strategy:** copy a class in `strategies.py`, change its rules, add it to `ALL_STRATEGIES`, and add a pair of secrets named with its `key_suffix`. Alpaca allows three paper accounts per login, so a fourth strategy needs a second Alpaca login.

## Troubleshooting

- **Dashboard says "No bot data yet":** run the Trading Bot workflow from the Actions tab and wait a couple of minutes.
- **A strategy says "Not switched on yet":** its two secrets are missing or misspelled. The dashboard shows the exact names it expects.
- **Bot placed no trades:** open the strategy's **Every check** tab. Each check shows why it was skipped. The two new strategies are selective and may go days with no trade.
- **Position smaller than expected:** with 0.5% risk and a wide stop the position is smaller than the account. Raise `risk_per_trade_pct` (up to 2%) if you want larger positions.
- **Entry "Cancelled":** the limit order was not filled within 90 seconds. That is intended.
- **Red X on a run:** open the run in the Actions tab, click the failed step, and read the last lines of the log.
- **Backtest tab says no results:** run the Backtest workflow once.
- Scheduled workflows on a public repo pause after 60 days without repo activity. Press **Run workflow** once to wake them.

## Changelog

- **8.0** (bot.py 8.0.0, strategies.py 3.0.0, indicators.py 2.0.0, publisher.py 2.1.0, tune.py 2.0.0, backtest.py 1.0.0, index.html 3.0.0): Opening Range Breakout and RSI(2) replaced by TJR Liquidity Sweep and EMA + VWAP Trend, both trading the whole account in one position at a time, long and short, with 0.5% default risk and a hard 2% cap; limit-order entries cancelled after 90 seconds; estimated slippage and fees on every trade; pause after three losing trades in a row; early-close days handled; SPY benchmark; backtest button and tab; nightly tuner is now suggest-only; dashboard shows long versus short, sample-size labels, results after costs and a "why" for each trade; accounts reset to $5,000.
- **7.0** (bot.py 7.0.0, strategies.py 2.0.0, publisher.py 2.0.0, indicators.py 1.1.0, index.html 2.0.0): three strategies at once, each on its own paper account; eight-stock watchlist; Opening Range Breakout and RSI(2) Pullback added; closed-trade tracking; end-of-day flatten for same-day strategies; dashboard Compare view, strategy tabs and closed-trades list; "Not set up" state for a strategy without keys.
- **6.1** (bot.py 6.1.0): bracket orders now stay active overnight instead of expiring at the close.
- **6.0**: dashboard edition. New `strategies.py`, `publisher.py` and `index.html`. The bot now records every check, order and account value.
- **5.0**: stop-loss and take-profit on every entry, nightly tuner, long and short.
- **1.0 to 4.0**: first working bot, then a faster long-running loop.

## Notes

- Paper trading is kinder than real trading. The cost estimate narrows the gap but is only an estimate, and a real broker has queue position, partial fills and short-borrow limits that paper cannot show.
- Short selling needs whole shares, and Alpaca may refuse shorts on some stocks. Refusals show on the dashboard.
- The free Alpaca data feed (IEX) covers only a slice of market volume, so the bot and the backtest both see a thinner picture than a paid feed would give.
- TJR-style trading is normally done by eye. This is an automated approximation, so the results say whether *this version* works, not whether the whole idea does.
- A stock an old account is still holding blocks a new entry in that stock until it is closed. Resetting the accounts clears this.
- The tuner is a re-fit of a few numbers to recent prices, not real machine learning, and a good backtest does not guarantee future results.
- None of this is financial advice. It is a paper-trading experiment.
