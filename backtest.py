"""
Backtest: replays the strategies over past 1-minute data.

File version: 1.0.0

Why this exists: paper trading takes months to produce enough trades to judge a
strategy. This replays the SAME strategy code the bot runs (strategies.py) over
the last couple of months of 1-minute prices, minute by minute, so you get a
first look at hundreds of trades in one run.

Run it from the Actions tab (Backtest, Run workflow). It writes
backtest/results.json, which the dashboard's Backtest tab reads.

How the replay works, and where it is generous or harsh:
  - Each strategy gets its own pretend account (default $5,000), sized exactly as
    the live bot sizes it, with the same daily loss limit and loss-streak pause.
  - Entries are limit orders placed after a bar closes and filled on the NEXT bar
    only if price is still within the limit. Otherwise the trade is missed, as live.
  - When a stop and a target could both have been hit inside one bar, the stop wins.
  - A stop that price gaps through fills at the worse price.
  - Estimated slippage and fees (strategies.COSTS) are counted on every trade, and
    results are shown before and after them.
  - Prices are the free IEX feed, which covers only part of the market's volume.
  - A good backtest does NOT promise future results. A setting can look great on
    the past and fail next month, so the results are split into an earlier 70% of
    the days and a later 30%. If the later part looks very different, be wary.
"""

import argparse
import bisect
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from indicators import Bar, ET
import strategies as S
from strategies import ALL_STRATEGIES, Context, trade_cost, COSTS

# Keep in step with WATCHLIST in bot.py.
WATCHLIST = ["AAPL", "MSFT", "SPY", "QQQ", "NVDA", "TSLA", "AMD", "PLTR"]
MAX_DAILY_LOSS_PCT = 0.03
OUT_PATH = os.path.join("backtest", "results.json")
BENCHMARK = "SPY"
IN_SAMPLE_SHARE = 0.7

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backtest")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_all(client, symbols, start, end):
    """1-minute IEX bars for each symbol, as {symbol: [Bar, ...]} oldest first."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    out = {}
    for sym in symbols:
        req = StockBarsRequest(symbol_or_symbols=[sym], timeframe=TimeFrame.Minute, start=start, end=end, feed=DataFeed.IEX)
        result = client.get_stock_bars(req)
        raw = None
        try:
            raw = result.data.get(sym)
        except Exception:
            raw = None
        if raw is None:
            try:
                df = result.df
                raw = [] if df is None or df.empty else [_Row(r) for r in df.reset_index().to_dict("records")]
            except Exception:
                raw = []
        bars = []
        for b in raw or []:
            t = getattr(b, "timestamp")
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            bars.append(Bar(t, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume or 0)))
        bars.sort(key=lambda x: x.t)
        out[sym] = bars
        log.info(f"{sym}: {len(bars)} one-minute bars")
    return out


class _Row:
    def __init__(self, d):
        self.__dict__.update(d)


class Series:
    """A symbol's bars with fast time-range slicing."""

    def __init__(self, bars):
        self.bars = bars
        self.times = [b.t.timestamp() for b in bars]

    def between(self, start, end):
        lo = bisect.bisect_left(self.times, start.timestamp())
        hi = bisect.bisect_left(self.times, end.timestamp())
        return self.bars[lo:hi]


def session_offset(b):
    e = b.t.astimezone(ET)
    return e.date(), int((e - e.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() // 60)


# ---------------------------------------------------------------------------
# A pretend account running one strategy
# ---------------------------------------------------------------------------

class Sim:
    def __init__(self, strategy, params, balance, symbols):
        self.s = strategy
        self.params = params
        self.symbols = symbols
        self.cash = balance
        self.start_balance = balance
        self.positions = {}        # sym -> dict
        self.pending = {}          # sym -> entry order waiting for the next bar
        self.trades = []
        self.costs = 0.0
        self.last_price = {}
        self.day = None
        self.day_start_equity = balance
        self.halted = False
        self.losses = 0
        self.attempts = set()
        self.last_attempt = {}     # sym -> minute index of the day
        self.curve = []            # (date, gross equity, net equity)
        self.cache = {}            # (key, sym) -> (when, bars)

    def equity(self):
        return self.cash + sum(p["qty"] * self.last_price.get(sym, p["entry"]) * (1 if p["side"] == "long" else -1)
                               for sym, p in self.positions.items())

    # --- fills ----------------------------------------------------------------
    def open_position(self, sym, order, price, when):
        sign = 1 if order["side"] == "long" else -1
        self.cash -= sign * order["qty"] * price
        self.positions[sym] = {**order, "entry": price, "opened": when}

    def close_position(self, sym, price, kind, when):
        p = self.positions.pop(sym)
        sign = 1 if p["side"] == "long" else -1
        self.cash += sign * p["qty"] * price
        pnl = (price - p["entry"]) * p["qty"] * sign
        cost = trade_cost(p["side"], p["qty"], p["entry"], price, kind)
        self.costs += cost
        risk = abs(p["entry"] - p["stop"]) * p["qty"] if p.get("stop") else None
        self.trades.append({
            "t": when.isoformat(timespec="seconds"), "opened_at": p["opened"].isoformat(timespec="seconds"), "symbol": sym,
            "side": p["side"], "qty": p["qty"], "entry": round(p["entry"], 2), "exit": round(price, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(sign * (price / p["entry"] - 1) * 100, 3),
            "exit_kind": kind, "cost": cost, "pnl_net": round(pnl - cost, 2),
            "risk": round(risk, 2) if risk else None, "r": round(pnl / risk, 2) if risk else None,
            "stop": p.get("stop"), "target": p.get("target"),
            "held_min": round((when - p["opened"]).total_seconds() / 60, 1), "why": p.get("why"), "setup": p.get("setup"),
        })
        self.losses = self.losses + 1 if pnl < 0 else 0

    # --- one bar of price action for one symbol ---------------------------------
    def on_bar(self, sym, bar, when):
        """`bar` is the minute that just completed (or None if nothing traded)."""
        if bar is not None:
            self.last_price[sym] = bar.c
        order = self.pending.pop(sym, None)
        if order is not None and bar is not None:
            long_side = order["side"] == "long"
            lim = order["limit"]
            if long_side:
                fill = bar.o if bar.o <= lim else (lim if bar.l <= lim else None)
            else:
                fill = bar.o if bar.o >= lim else (lim if bar.h >= lim else None)
            if fill is None:
                self.attempts.discard(sym)          # never filled: not a used-up entry
            else:
                self.open_position(sym, order, fill, when)
        p = self.positions.get(sym)
        if p is not None and bar is not None and sym not in self.pending:
            long_side = p["side"] == "long"
            stop, target = p.get("stop"), p.get("target")
            hit = None
            if long_side:
                if stop is not None and bar.l <= stop:
                    hit = (min(stop, bar.o), "Stop-loss")
                elif target is not None and bar.h >= target:
                    hit = (bar.o if bar.o >= target else target, "Take-profit")
            else:
                if stop is not None and bar.h >= stop:
                    hit = (max(stop, bar.o), "Stop-loss")
                elif target is not None and bar.l <= target:
                    hit = (target if bar.o > target else bar.o, "Take-profit")
            if hit is not None:
                self.close_position(sym, hit[0], hit[1], when)

    # --- data the strategy asks for -------------------------------------------------
    def data_for(self, sym, series, now, ctx):
        out = {}
        for r in self.s.data_requests(now, ctx, self.params):
            key = r["key"]
            ck = (key, sym)
            cached = self.cache.get(ck)
            ttl = r.get("cache_seconds", 0)
            if cached and ttl > 0 and (now - cached[0]).total_seconds() < ttl:
                out[key] = cached[1]
                continue
            end = min(r.get("end") or now, now)
            bars = series.between(r["start"], end)
            trim = self.s.backtest_trim.get(key)
            if trim:
                bars = bars[-trim:]
            self.cache[ck] = (now, bars)
            out[key] = bars
        return out


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------

def day_close_minute(max_offset):
    """Minutes after 9:30 at which the market closed that day (390 normally, 210 on a 1 PM early close)."""
    if max_offset >= 380:
        return 390
    if max_offset >= 195:
        return 210
    return max_offset + 1


def replay(strategy, params, balance, series, day_bars, days):
    sim = Sim(strategy, params, balance, WATCHLIST)
    fm = strategy.flatten_minutes_before_close
    n_symbols = len(WATCHLIST)

    for d in days:
        sim.day = d
        sim.day_start_equity = sim.equity()
        sim.halted, sim.losses = False, 0
        sim.attempts.clear()
        sim.last_attempt.clear()
        sim.pending.clear()
        by_min = {sym: day_bars[sym].get(d, {}) for sym in WATCHLIST}
        max_off = max((max(m) if m else 0) for m in by_min.values())
        close_min = day_close_minute(max_off)
        open_utc = datetime.combine(d, datetime.min.time().replace(hour=9, minute=30), tzinfo=ET).astimezone(timezone.utc)

        for m in range(0, close_min):
            when = open_utc + timedelta(minutes=m + 1)          # the bar for minute m has just completed
            for sym in WATCHLIST:
                sim.on_bar(sym, by_min[sym].get(m), when)
            eq = sim.equity()
            if not sim.halted and sim.day_start_equity and (sim.day_start_equity - eq) / sim.day_start_equity >= MAX_DAILY_LOSS_PCT:
                sim.halted = True
            mtc = close_min - (m + 1)
            now_et = when.astimezone(ET)
            no_entries = fm is not None and mtc <= fm
            if no_entries and sim.positions:
                for sym in list(sim.positions):
                    sim.close_position(sym, sim.last_price[sym], "Market exit", when)
            if mtc <= 0:
                break

            paused = bool(strategy.loss_streak_limit) and sim.losses >= strategy.loss_streak_limit
            busy = next(iter(sim.positions), None) or next(iter(sim.pending), None)
            candidates = []
            for sym in WATCHLIST:
                pos = sim.positions.get(sym)
                ctx = Context(now_et=now_et, minutes_to_close=float(mtc), equity=eq, n_symbols=n_symbols, position=(
                    {"side": pos["side"], "qty": pos["qty"], "avg_entry_price": pos["entry"],
                     "entry_date": pos["opened"].astimezone(ET).date().isoformat(), "opened_at": pos["opened"]} if pos else None),
                    attempted_today=sym in sim.attempts)
                if pos is None:
                    if no_entries or sim.halted or paused or sym in sim.pending:
                        continue
                    if strategy.single_position and busy:
                        continue
                    if strategy.one_entry_per_day and sym in sim.attempts:
                        continue
                    last = sim.last_attempt.get(sym)
                    if strategy.cooldown_minutes and last is not None and m - last < strategy.cooldown_minutes:
                        continue
                    if not strategy.can_enter_now(ctx, params):
                        continue
                elif sym not in sim.positions:
                    continue
                if not by_min[sym] or sym not in series:
                    continue
                data = sim.data_for(sym, series[sym], when, ctx)
                dec = strategy.evaluate(sym, data, params, ctx)
                if dec.signal == "exit" and pos is not None and dec.actionable:
                    sim.close_position(sym, sim.last_price[sym], "Market exit", when)
                elif dec.signal in ("long", "short") and pos is None and dec.actionable and dec.price:
                    if strategy.single_position:
                        candidates.append((sym, dec))
                    else:
                        submit(sim, strategy, params, sym, dec, eq, n_symbols, m)

            for sym, dec in sorted(candidates, key=lambda c: -c[1].score):
                if submit(sim, strategy, params, sym, dec, eq, n_symbols, m):
                    break

        # anything still open at the end of the day (strategies that hold overnight) is just marked to market
        end_eq = sim.equity()
        sim.curve.append((d.isoformat(), round(end_eq, 2), round(end_eq - sim.costs, 2)))
    return sim


def submit(sim, strategy, params, sym, dec, equity, n_symbols, minute):
    qty = strategy.position_size(equity, dec.price, dec, params, n_symbols)
    sim.last_attempt[sym] = minute
    sim.attempts.add(sym)
    if qty <= 0:
        sim.attempts.discard(sym)
        return False
    offset = float(params.get("entry_offset_pct", 0.0005))
    limit = round(dec.price * (1 + offset), 2) if dec.signal == "long" else round(dec.price * (1 - offset), 2)
    sim.pending[sym] = {"side": dec.signal, "qty": qty, "limit": limit, "stop": dec.stop_price, "target": dec.take_profit_price,
                        "why": dec.reason, "setup": dec.setup or None}
    return True


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def stats(trades, balance):
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl_net"] > 0]
    losses = [t for t in trades if t["pnl_net"] <= 0]
    gw, gl = sum(t["pnl"] for t in trades if t["pnl"] > 0), -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    nw, nl = sum(t["pnl_net"] for t in wins), -sum(t["pnl_net"] for t in losses)
    rs = [t["r"] for t in trades if t.get("r") is not None]

    def side(s):
        ts = [t for t in trades if t["side"] == s]
        return {"trades": len(ts), "win_rate": round(100 * sum(1 for t in ts if t["pnl_net"] > 0) / len(ts), 1) if ts else None,
                "pnl_net": round(sum(t["pnl_net"] for t in ts), 2)}

    by_hour = defaultdict(lambda: [0, 0.0])
    for t in trades:
        h = datetime.fromisoformat(t["opened_at"]).astimezone(ET).hour
        by_hour[h][0] += 1
        by_hour[h][1] += t["pnl_net"]
    return {
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1),
        "pnl_gross": round(sum(t["pnl"] for t in trades), 2),
        "pnl_net": round(sum(t["pnl_net"] for t in trades), 2),
        "costs": round(sum(t["cost"] for t in trades), 2),
        "avg_win_net": round(nw / len(wins), 2) if wins else 0.0,
        "avg_loss_net": round(-nl / len(losses), 2) if losses else 0.0,
        "profit_factor_gross": round(gw / gl, 2) if gl else None,
        "profit_factor_net": round(nw / nl, 2) if nl else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_hold_min": round(sum(t["held_min"] for t in trades) / n, 1),
        "long": side("long"), "short": side("short"),
        "by_hour": [{"hour": h, "trades": v[0], "pnl_net": round(v[1], 2)} for h, v in sorted(by_hour.items())],
        "return_gross_pct": round(100 * sum(t["pnl"] for t in trades) / balance, 2),
        "return_net_pct": round(100 * sum(t["pnl_net"] for t in trades) / balance, 2),
    }


def max_drawdown_pct(curve_values):
    peak, worst = None, 0.0
    for v in curve_values:
        peak = v if peak is None else max(peak, v)
        worst = min(worst, (v - peak) / peak * 100 if peak else 0.0)
    return round(worst, 2)


def load_params(strategy):
    path = "config.json" if strategy.id == "rsi-reversion" else f"config_{strategy.id}.json"
    try:
        with open(path) as f:
            params = {**strategy.default_params, **json.load(f)}
    except Exception:
        params = dict(strategy.default_params)
    if "risk_per_trade_pct" in params:
        params["risk_per_trade_pct"] = max(0.0, min(float(params["risk_per_trade_pct"]), S.MAX_RISK_PCT))
    return params


def build_results(args, series, day_bars, days):
    results = []
    split = days[int(len(days) * IN_SAMPLE_SHARE) - 1].isoformat() if len(days) >= 4 else days[-1].isoformat()
    for cls in ALL_STRATEGIES:
        strat = cls()
        params = load_params(strat)
        log.info(f"Replaying {strat.name} over {len(days)} days...")
        sim = replay(strat, params, args.balance, series, day_bars, days)
        trades = sim.trades
        net_curve = [c[2] for c in sim.curve]
        before = [t for t in trades if t["t"][:10] <= split]
        after = [t for t in trades if t["t"][:10] > split]
        summary = stats(trades, args.balance)
        summary["max_drawdown_pct"] = max_drawdown_pct(net_curve)
        results.append({
            "id": strat.id, "name": strat.name, "params": {k: v for k, v in params.items()
                                                         if isinstance(v, (int, float, str, bool, type(None)))},
            "summary": summary,
            "earlier_part": {"through": split, **stats(before, args.balance)},
            "later_part": {"after": split, **stats(after, args.balance)},
            "equity": [list(c) for c in sim.curve],
            "trades": trades[-400:],
        })
        log.info(f"  {summary.get('trades')} trades, net {summary.get('return_net_pct')}%")
    return results


def benchmark(series, days):
    """SPY bought on the first day and held, on the same starting balance."""
    spy = series.get(BENCHMARK)
    if not spy:
        return None
    closes = {}
    for b in spy.bars:
        d, off = session_offset(b)
        if 0 <= off < 390:
            closes[d] = b.c
    pts = [(d.isoformat(), closes[d]) for d in days if d in closes]
    return {"symbol": BENCHMARK, "points": pts} if pts else None


def run(client, days_wanted, balance, end=None):
    """Download the data, replay every strategy, and return the results dictionary."""
    args = argparse.Namespace(days=max(5, min(int(days_wanted), 250)), balance=float(balance))
    end = end or (datetime.now(timezone.utc) - timedelta(minutes=20))
    start = end - timedelta(days=int(args.days * 1.5) + 12)     # a few extra days so the first replayed day has history
    log.info(f"Downloading 1-minute bars from {start:%Y-%m-%d} to {end:%Y-%m-%d} ...")
    bars = fetch_all(client, WATCHLIST, start, end)
    series = {sym: Series(b) for sym, b in bars.items() if b}

    day_bars = {sym: defaultdict(dict) for sym in WATCHLIST}
    counts = defaultdict(int)
    for sym, bl in bars.items():
        for b in bl:
            d, off = session_offset(b)
            if 0 <= off < 390:
                day_bars[sym][d][off] = b
                counts[d] += 1
    all_days = sorted(d for d, c in counts.items() if c > 300 * max(1, len(series) // 2))
    days = all_days[-args.days:]
    if len(days) < 5:
        raise SystemExit("Not enough trading days of data came back, so there is nothing to replay.")
    log.info(f"Replaying {len(days)} trading days: {days[0]} to {days[-1]}")

    results = build_results(args, series, day_bars, days)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": len(days), "start": days[0].isoformat(), "end": days[-1].isoformat(),
        "balance": args.balance, "costs": COSTS, "benchmark": benchmark(series, days),
        "data_note": ("Free IEX prices (part of the market's volume). Stops win when a stop and target fall in the same minute. "
                      "Results are after estimated slippage and fees as well as before them. A good backtest does not promise future results."),
        "strategies": results,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--balance", type=float, default=5000.0)
    args = ap.parse_args()

    from alpaca.data.historical import StockHistoricalDataClient
    client = StockHistoricalDataClient(os.environ["APCA_API_KEY_ID"].strip(), os.environ["APCA_API_SECRET_KEY"].strip())
    out = run(client, args.days, args.balance)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, separators=(",", ":"), default=str)
    log.info(f"Saved {OUT_PATH}")


if __name__ == "__main__":
    main()
