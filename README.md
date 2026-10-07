# Trading Bot (paper trading only)

An automated trading bot that runs for free on GitHub Actions and trades **paper** (pretend-money) Alpaca accounts. It never touches real money. A web dashboard shows everything it does and lets you compare the strategies side by side.

**Current release: 7.0** (multi-strategy edition)

## What it does

- Runs **three strategies at once**, each on its own Alpaca paper account (a free Alpaca login allows three).
- All three watch the same eight stocks: **AAPL, MSFT, SPY, QQQ, NVDA, TSLA, AMD, PLTR**.
- Every entry is a bracket order, so Alpaca manages a stop-loss and a take-profit for you. They stay active overnight.
- Each account has a daily loss limit of 3% (new trades stop for the day), one position per stock, and its own cooldowns.
- Every closed trade is recorded with its profit or loss, so the dashboard can show win rate, average win and loss, and profit factor.

| Strategy | Speed | What it does |
|---|---|---|
| **RSI Reversion** | 1-minute bars, in and out within hours | Goes long when RSI(14) is oversold and short when it is overbought, with a small stop-loss and take-profit. Settings are re-fit each night by the tuner. |
| **Opening Range Breakout** | 5-minute bars, in and out the same day | Looks at the first 5-minute candle (9:30 to 9:35 ET). If it closed up on decent volume, go long; if down, go short. Stop at the other end of that candle. Enters between 9:35 and 9:50 ET, and closes everything 5 minutes before the close. |
| **RSI(2) Pullback** | Daily bars, holds for days | Buys sharp 2-day dips in stocks above their 200-day average, long only. Sells when the price recovers above its 5-day average, or after 7 trading days. Trades are rare, a few a month. |

## The dashboard

`https://greysonbrindley727-wq.github.io/Trading-bot-V6/`

- **Compare** tab: every strategy as a percent-return line on one chart, all starting at 0% on the same date, plus a side-by-side table of return, worst drop, win rate, closed trades, average win and loss, profit factor, account value and open positions. Today and All time views.
- One tab per strategy: account value, profit and loss by day, how the strategy decides, a live reading for each stock, open positions, every order, closed trades, every check the bot ran, and (for RSI Reversion) the nightly tuning results.
- A strategy with no API keys yet shows a "Not set up" screen with the steps, and the others carry on.

The data refreshes about every 5 minutes while the bot runs. The bot publishes its data to a branch called `dashboard-data`, rewritten as a single commit each time so it never fills up the repo's history.

## Files and versions

| File | Version | What it is |
|---|---|---|
| `bot.py` | 7.0.0 | The trading bot. One long job runs all three strategies. |
| `strategies.py` | 2.0.0 | The three strategies. Add new strategies here. |
| `publisher.py` | 2.0.0 | Writes the dashboard's data files and publishes them. |
| `indicators.py` | 1.1.0 | RSI and moving-average math shared by the bot and the tuner. |
| `tune.py` | 1.0.0 | The nightly tuner (RSI Reversion only). |
| `index.html` | 2.0.0 | The dashboard website. |
| `config.json` | auto | RSI Reversion settings. Rewritten by the tuner each night. |
| `config_orb.json`, `config_rsi2-swing.json` | optional | Optional setting overrides for the other two strategies (see below). |
| `tuning_log.csv` | auto | History of what the tuner tried and chose. |
| `requirements.txt` | 1.0.0 | Python packages the bot needs. |
| `.github/workflows/trading-bot.yml` | 7.0.0 | Starts the bot on weekdays (two scheduled starts a day). |
| `.github/workflows/tune.yml` | 1.0.0 | Runs the tuner after the market closes. |

`bot.py`, `strategies.py`, `publisher.py`, `indicators.py` and `index.html` also carry their version at the top of the file. When a file changes, bump its version and add a line to the changelog below.

## Setup

Already done for the first account (RSI Reversion):

1. Alpaca paper account with API keys saved as repo secrets `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`.
2. Settings, Actions, General, Workflow permissions: **Read and write**.
3. Settings, Pages: deploy from branch `main`, folder `/ (root)`.

To switch on the other two strategies, each needs its own paper account:

1. In Alpaca, open the paper trading dashboard and choose **Open New Paper Account**. Do it twice.
2. For each new account, generate an API key and secret and copy them somewhere safe. The secret is shown only once.
3. In the GitHub repo, go to Settings, Secrets and variables, Actions, and add these four secrets with exactly these names:

| Secret name | What goes in it |
|---|---|
| `APCA_API_KEY_ID_ORB` | key for the Opening Range Breakout account |
| `APCA_API_SECRET_KEY_ORB` | secret for the Opening Range Breakout account |
| `APCA_API_KEY_ID_RSI2` | key for the RSI(2) Pullback account |
| `APCA_API_SECRET_KEY_RSI2` | secret for the RSI(2) Pullback account |

4. Run the **Trading Bot** workflow once from the Actions tab.

## Changing things

- **Watchlist:** `WATCHLIST` near the top of `bot.py`. To give one strategy its own list, fill in `WATCHLISTS`, for example `WATCHLISTS = {"orb": ["SPY", "QQQ", "NVDA"]}`.
- **Daily loss limit:** `MAX_DAILY_LOSS_PCT` in `bot.py`.
- **RSI Reversion settings:** `config.json`. The tuner overwrites it each night.
- **Opening Range Breakout or RSI(2) settings:** create `config_orb.json` or `config_rsi2-swing.json` containing only the settings you want to change, for example `{"min_rel_volume": 1.2}` or `{"entry_rsi": 5}`. The setting names are the ones in `strategies.py`.
- **Add a new strategy:** copy a class in `strategies.py`, change its rules, add it to `ALL_STRATEGIES`, and add a pair of secrets named with its `key_suffix`. Alpaca allows three paper accounts per login, so a fourth strategy needs a second Alpaca login.

## Troubleshooting

- **Dashboard says "No bot data yet":** run the Trading Bot workflow from the Actions tab and wait a couple of minutes.
- **A strategy says "Not set up":** its two secrets are missing or misspelled. The dashboard shows the exact names it expects.
- **Bot placed no trades:** open the strategy's **Every check** tab. Each signal shows why it was skipped, for example "Already holding a position in this stock".
- **Red X on a run:** open the run in the Actions tab, click the failed step, and read the last lines of the log.
- Scheduled workflows on a public repo pause after 60 days without repo activity. Press **Run workflow** once to wake them.

## Changelog

- **7.0** (bot.py 7.0.0, strategies.py 2.0.0, publisher.py 2.0.0, indicators.py 1.1.0, index.html 2.0.0): three strategies at once, each on its own paper account; eight-stock watchlist; Opening Range Breakout and RSI(2) Pullback added; closed-trade tracking; end-of-day flatten for same-day strategies; dashboard Compare view, strategy tabs and closed-trades list; "Not set up" state for a strategy without keys.
- **6.1** (bot.py 6.1.0): bracket orders now stay active overnight instead of expiring at the close.
- **6.0**: dashboard edition. New `strategies.py`, `publisher.py` and `index.html`. The bot now records every check, order and account value.
- **5.0**: stop-loss and take-profit on every entry, nightly tuner, long and short.
- **1.0 to 4.0**: first working bot, then a faster long-running loop.

## Notes

- Paper trading has no slippage or borrow fees, so real results would differ.
- Short selling needs whole shares, and Alpaca may refuse shorts on some stocks. Refusals show on the dashboard.
- The free Alpaca data feed (IEX) covers only a slice of market volume. The Opening Range Breakout compares IEX volume with IEX volume, so its volume filter stays consistent, but the opening candle itself is an IEX-only view.
- A stock the old account is still holding (for example from RSI Reversion before the upgrade) blocks a new RSI Reversion entry in that stock until it is closed.
- The nightly tuner is a re-fit of a few numbers to recent prices, not real machine learning, and a good backtest does not guarantee future results.
- 
