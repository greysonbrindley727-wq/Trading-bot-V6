"""
Nightly tuner for RSI Reversion, in SUGGEST-ONLY mode.

File version: 2.0.0
  2.0.0: no longer changes config.json. It still runs every night, but it only
         records what it WOULD choose (tuning_log.csv and tuning_suggestion.json)
         so you can judge whether its picks are stable before ever letting it
         change anything. Other changes:
           - tests all 8 watchlist stocks (it used to test only AAPL, MSFT and SPY)
           - uses 30 days of history instead of 10
           - judges each settings combination on ALL stocks together, after
             estimated trading costs, and ignores combinations with too few trades
           - only suggests a change when it beats the current settings by a clear margin
  1.0.0: nightly re-fit that overwrote config.json.

This is a re-fit of a few rule parameters to recent prices, not machine
learning, and it can still be fooled by a lucky stretch. Every night's result
is logged so you can see how stable it is.
"""

import os
import csv
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file (e.g. in GitHub Actions)
except ImportError:
    pass

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from strategies import COSTS

WATCHLIST = ["AAPL", "MSFT", "SPY", "QQQ", "NVDA", "TSLA", "AMD", "PLTR"]
LOOKBACK_DAYS = 30
MIN_TRADES = 20            # a settings combination needs at least this many trades (all stocks together)
MIN_IMPROVEMENT = 1.0      # ...and must beat the current settings by this many percentage points of total return
CONFIG_PATH = "config.json"
TUNING_LOG_PATH = "tuning_log.csv"
SUGGESTION_PATH = "tuning_suggestion.json"

# The grid to search. Keep this small since it's re-run every night.
RSI_PERIODS = [10, 14, 21]
THRESHOLD_PAIRS = [(25, 75), (30, 70), (35, 65)]                     # (oversold, overbought)
STOP_TARGET_PAIRS = [(0.003, 0.006), (0.004, 0.008), (0.006, 0.012)]  # (stop_pct, take_profit_pct)

# Estimated cost of one trade, as a share of its price, by how it ended (see strategies.COSTS)
COST_STOP = COSTS["slip_entry_pct"] + COSTS["slip_stop_exit_pct"]
COST_TARGET = COSTS["slip_entry_pct"] + COSTS["slip_target_exit_pct"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tuner")


def get_client():
    key = os.environ["APCA_API_KEY_ID"].strip()
    secret = os.environ["APCA_API_SECRET_KEY"].strip()
    return StockHistoricalDataClient(key, secret)


def fetch_closes_highs_lows(client, symbol: str):
    start = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Minute, start=start,
                           feed=DataFeed.IEX)   # free data feed, no subscription needed
    result = client.get_stock_bars(req)
    raw = None
    try:
        raw = result.data.get(symbol)
    except Exception:
        raw = None
    if raw:
        return [float(b.close) for b in raw], [float(b.high) for b in raw], [float(b.low) for b in raw]
    try:
        df = result.df
        if df is None or df.empty:
            return [], [], []
        return df["close"].tolist(), df["high"].tolist(), df["low"].tolist()
    except Exception:
        return [], [], []


def rsi_series(closes, period):
    """The same simple RSI as indicators.compute_rsi, for every bar at once. None until there is enough history."""
    n = len(closes)
    out = [None] * n
    gains, losses = [0.0] * (n + 1), [0.0] * (n + 1)      # running sums
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains[i + 1] = gains[i] + (ch if ch > 0 else 0.0)
        losses[i + 1] = losses[i] + (-ch if ch <= 0 else 0.0)
    for i in range(period, n):
        g = (gains[i + 1] - gains[i + 1 - period]) / period
        l = (losses[i + 1] - losses[i + 1 - period]) / period
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def backtest(closes, highs, lows, rsis, oversold, overbought, stop_pct, target_pct):
    """
    Bar-by-bar test of the RSI reversion rule, one position at a time in this stock.
    Returns a list of trade results, each as a fraction of the position (after estimated costs).
    """
    position = None  # {"side", "entry", "stop", "target"}
    results = []
    for i in range(len(closes)):
        price, high, low = closes[i], highs[i], lows[i]
        if position is None:
            rsi = rsis[i]
            if rsi is None:
                continue
            if rsi <= oversold:
                position = {"side": "long", "entry": price, "stop": price * (1 - stop_pct), "target": price * (1 + target_pct)}
            elif rsi >= overbought:
                position = {"side": "short", "entry": price, "stop": price * (1 + stop_pct), "target": price * (1 - target_pct)}
            continue

        if position["side"] == "long":
            if low <= position["stop"]:
                results.append(-stop_pct - COST_STOP)
                position = None
            elif high >= position["target"]:
                results.append(target_pct - COST_TARGET)
                position = None
        else:
            if high >= position["stop"]:
                results.append(-stop_pct - COST_STOP)
                position = None
            elif low <= position["target"]:
                results.append(target_pct - COST_TARGET)
                position = None
    return results


def score(data, rsi_cache, period, oversold, overbought, stop_pct, target_pct):
    """Total return (in percent) and trade count across every stock for one settings combination."""
    total, trades = 0.0, 0
    for sym, (closes, highs, lows) in data.items():
        res = backtest(closes, highs, lows, rsi_cache[(sym, period)], oversold, overbought, stop_pct, target_pct)
        total += sum(res)
        trades += len(res)
    return total * 100, trades


def load_current():
    defaults = {"rsi_period": 14, "rsi_oversold": 30, "rsi_overbought": 70, "stop_loss_pct": 0.004, "take_profit_pct": 0.008}
    try:
        with open(CONFIG_PATH) as f:
            return {**defaults, **json.load(f)}
    except Exception:
        return defaults


def log_result(row: dict) -> None:
    fieldnames = ["date", "symbol", "rsi_period", "rsi_oversold", "rsi_overbought",
                  "stop_loss_pct", "take_profit_pct", "return_pct", "num_trades"]
    file_has_content = os.path.isfile(TUNING_LOG_PATH) and os.path.getsize(TUNING_LOG_PATH) > 0
    with open(TUNING_LOG_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_has_content:
            writer.writeheader()
        writer.writerow(row)


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    client = get_client()

    data = {}
    for symbol in WATCHLIST:
        closes, highs, lows = fetch_closes_highs_lows(client, symbol)
        if len(closes) < 200:
            log.warning(f"{symbol}: not enough history to tune ({len(closes)} bars), skipping it.")
            continue
        data[symbol] = (closes, highs, lows)
    if not data:
        log.warning("No stock had enough data to tune; nothing logged tonight.")
        return

    periods = set(RSI_PERIODS) | {int(load_current()["rsi_period"])}
    rsi_cache = {(sym, p): rsi_series(d[0], p) for sym, d in data.items() for p in periods}

    results = []
    for period in RSI_PERIODS:
        for oversold, overbought in THRESHOLD_PAIRS:
            for stop_pct, target_pct in STOP_TARGET_PAIRS:
                ret, trades = score(data, rsi_cache, period, oversold, overbought, stop_pct, target_pct)
                results.append({"rsi_period": period, "rsi_oversold": oversold, "rsi_overbought": overbought,
                                "stop_loss_pct": stop_pct, "take_profit_pct": target_pct,
                                "return_pct": round(ret, 3), "num_trades": trades})

    current = load_current()
    cur_ret, cur_trades = score(data, rsi_cache, int(current["rsi_period"]), current["rsi_oversold"], current["rsi_overbought"],
                                current["stop_loss_pct"], current["take_profit_pct"])
    current_row = {"rsi_period": int(current["rsi_period"]), "rsi_oversold": current["rsi_oversold"],
                   "rsi_overbought": current["rsi_overbought"], "stop_loss_pct": current["stop_loss_pct"],
                   "take_profit_pct": current["take_profit_pct"], "return_pct": round(cur_ret, 3), "num_trades": cur_trades}

    eligible = [r for r in results if r["num_trades"] >= MIN_TRADES]
    best = max(eligible, key=lambda r: r["return_pct"]) if eligible else None
    would_change = bool(best and (best["return_pct"] - cur_ret) >= MIN_IMPROVEMENT
                        and {k: best[k] for k in current_row if k not in ("return_pct", "num_trades")}
                        != {k: current_row[k] for k in current_row if k not in ("return_pct", "num_trades")})

    log_result({"date": today, "symbol": "ALL (current)", **current_row})
    if best:
        log_result({"date": today, "symbol": "ALL (suggested)", **best})

    if not best:
        note = f"No settings combination reached {MIN_TRADES} trades in {LOOKBACK_DAYS} days, so there is no suggestion."
    elif would_change:
        note = (f"The best combination beat the current settings by {best['return_pct'] - cur_ret:.2f} percentage points "
                f"(need {MIN_IMPROVEMENT:g}). Not applied: the tuner only suggests.")
    else:
        note = "The current settings are good enough. No change suggested."

    suggestion = {
        "date": today, "lookback_days": LOOKBACK_DAYS, "stocks": sorted(data), "min_trades": MIN_TRADES,
        "min_improvement_pct": MIN_IMPROVEMENT, "current": current_row, "best": best,
        "would_change": would_change, "note": note, "applied": False,
    }
    with open(SUGGESTION_PATH, "w") as f:
        json.dump(suggestion, f, indent=2)

    log.info(f"Current settings: {current_row}")
    log.info(f"Best found: {best}")
    log.info(note)
    log.info("config.json was NOT changed.")


if __name__ == "__main__":
    main()
