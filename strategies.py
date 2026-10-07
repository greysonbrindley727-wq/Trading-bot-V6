"""
Trading strategies.

File version: 2.0.0
  2.0.0: strategies now get OHLCV bars and a Context (time, position, ...),
         can ask for different bar sizes, and can set their own stop/target.
         Added OpeningRangeBreakout and RSI2Swing next to RSIReversion.
  1.0.0: RSIReversion only.

Each strategy answers one question: "given the recent prices for this
symbol, should the bot go long, go short, exit, or do nothing, and why?"

A strategy does NOT place orders. bot.py does that. The strategy only
returns a Decision, which also carries the indicator values and the
individual yes/no conditions it checked. The dashboard shows those, so you
can see how the bot read the market at every check.

To add a new strategy later: copy one of the classes below, give it a new
id/name, change data_requests(), evaluate() and describe(), and register it
in the STRATEGIES list in bot.py. Each strategy trades its own Alpaca paper
account; key_suffix says which pair of secrets it uses.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from indicators import compute_rsi, compute_rsi_wilder, compute_sma

ET = ZoneInfo("America/New_York")
OPEN_T = dtime(9, 30)


# ---------------------------------------------------------------------------
# Plain data containers shared with bot.py
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    t: datetime          # start of the bar, timezone-aware (UTC)
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Context:
    now_et: datetime
    minutes_to_close: Optional[float]     # None if unknown
    equity: float
    n_symbols: int
    position: Optional[dict] = None       # {"side", "qty", "avg_entry_price", "entry_date"} if holding
    attempted_today: bool = False         # an entry was already tried in this symbol today


@dataclass
class Decision:
    symbol: str
    price: Optional[float]
    signal: str                      # "long", "short", "exit" or "none"
    reason: str                      # one plain-English sentence for the dashboard
    indicators: dict = field(default_factory=dict)   # e.g. {"rsi": 28.4}
    checks: list = field(default_factory=list)       # [{"label": "...", "ok": True}]
    ready: bool = True               # False while there isn't enough price history
    actionable: bool = True          # False outside the strategy's trading window
    window_note: str = ""            # why it isn't actionable right now
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None


def bracket_levels(price, side, stop_pct, target_pct):
    if side == "long":
        return round(price * (1 - stop_pct), 2), round(price * (1 + target_pct), 2)
    return round(price * (1 + stop_pct), 2), round(price * (1 - target_pct), 2)


def weekdays_between(start: date, end: date) -> int:
    """Trading days elapsed after `start`, up to and including `end` (weekends skipped)."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _fmt_time(minutes_after_open: float) -> str:
    t = (datetime.combine(date(2000, 1, 3), OPEN_T) + timedelta(minutes=minutes_after_open))
    return t.strftime("%-I:%M %p")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class Strategy:
    id = "base"
    name = "Base strategy"
    description = ""
    kind = "intraday"                 # "intraday" or "swing"; shown on the dashboard
    key_suffix = ""                   # which Alpaca secrets it uses: APCA_API_KEY_ID<suffix>
    default_params: dict = {}
    log_every_seconds = 60            # how often a check per symbol is written to the log
    one_entry_per_day = False         # at most one entry attempt per symbol per day
    cooldown_minutes = 0              # minimum gap between order attempts in one symbol
    flatten_minutes_before_close = None   # close everything this long before the close
    symbols = None                    # None = the default watchlist in bot.py

    def data_requests(self, now_utc, ctx, params) -> list:
        """Bars to fetch for each symbol: [{"key", "timeframe", "start", "end"?, "cache_seconds"}]."""
        raise NotImplementedError

    def evaluate(self, symbol, data, params, ctx) -> Decision:
        raise NotImplementedError

    def position_size(self, equity, price, decision, params, n_symbols) -> int:
        """Whole shares. Default: an equal share of the account for each symbol."""
        if not price or price <= 0:
            return 0
        return max(0, math.floor((equity / max(n_symbols, 1)) / price))

    def settings(self, params, n_symbols) -> list:
        return []

    def describe(self, params) -> dict:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. RSI Reversion (1-minute bars, long and short)
# ---------------------------------------------------------------------------

class RSIReversion(Strategy):
    id = "rsi-reversion"
    name = "RSI Reversion"
    description = (
        "Bets that a sharp drop or spike on the 1-minute chart will snap back. "
        "It goes long when RSI falls to the oversold level and short when RSI "
        "rises to the overbought level. Every entry comes with its own "
        "stop-loss and take-profit, which Alpaca manages."
    )
    kind = "intraday"
    key_suffix = ""                   # the original account keeps the original secret names
    log_every_seconds = 60
    cooldown_minutes = 5
    default_params = {
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "stop_loss_pct": 0.004,
        "take_profit_pct": 0.008,
    }

    def data_requests(self, now_utc, ctx, params):
        return [{"key": "bars", "timeframe": "1Min", "start": now_utc - timedelta(days=3), "cache_seconds": 0}]

    def evaluate(self, symbol, data, params, ctx) -> Decision:
        closes = [b.c for b in data.get("bars", [])][-300:]
        period = int(params["rsi_period"])
        low = params["rsi_oversold"]
        high = params["rsi_overbought"]
        price = closes[-1] if closes else None

        rsi = compute_rsi(closes, period) if closes else None
        if rsi is None:
            return Decision(
                symbol=symbol, price=price, signal="none", ready=False,
                reason=f"Not enough price history yet (needs {period + 1} one-minute bars).",
            )

        oversold = rsi <= low
        overbought = rsi >= high
        checks = [
            {"label": f"RSI at or below {low} (oversold)", "ok": oversold},
            {"label": f"RSI at or above {high} (overbought)", "ok": overbought},
        ]

        if oversold:
            signal = "long"
            reason = f"RSI {rsi:.1f} is at or below {low}. The price looks oversold, so the bot looks for a bounce (long)."
        elif overbought:
            signal = "short"
            reason = f"RSI {rsi:.1f} is at or above {high}. The price looks overbought, so the bot looks for a pullback (short)."
        else:
            signal = "none"
            reason = f"RSI {rsi:.1f} is between {low} and {high}. No signal."

        d = Decision(symbol=symbol, price=price, signal=signal, reason=reason,
                     indicators={"rsi": round(rsi, 2)}, checks=checks)
        if signal in ("long", "short"):
            d.stop_price, d.take_profit_price = bracket_levels(
                price, signal, params["stop_loss_pct"], params["take_profit_pct"])
        return d

    def settings(self, params, n_symbols):
        return [
            {"label": "Position size", "display": f"{100 / max(n_symbols, 1):.1f}% of the account",
             "hint": "Per stock, rounded down to whole shares"},
            {"label": "Cooldown", "display": f"{self.cooldown_minutes} min",
             "hint": "Minimum gap between order attempts in one stock"},
        ]

    def describe(self, params) -> dict:
        p = int(params["rsi_period"])
        low, high = params["rsi_oversold"], params["rsi_overbought"]
        sl, tp = params["stop_loss_pct"] * 100, params["take_profit_pct"] * 100
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "timeframe": "1-minute bars",
            "schedule": "Checks every minute while the market is open.",
            "rules": [
                f"Go long when RSI({p}) falls to {low} or lower.",
                f"Go short when RSI({p}) rises to {high} or higher.",
                f"Each entry gets a stop-loss {sl:.2f}% away and a take-profit {tp:.2f}% away.",
                "Only one position per stock at a time. The stop-loss or take-profit closes it.",
            ],
            "params": [
                {"key": "rsi_period", "label": "RSI period", "display": f"{p} bars",
                 "hint": "How many 1-minute bars RSI looks back over"},
                {"key": "rsi_oversold", "label": "Oversold level", "display": f"{low}",
                 "hint": "RSI at or below this triggers a long"},
                {"key": "rsi_overbought", "label": "Overbought level", "display": f"{high}",
                 "hint": "RSI at or above this triggers a short"},
                {"key": "stop_loss_pct", "label": "Stop-loss", "display": f"{sl:.2f}%",
                 "hint": "Distance from entry where the position is closed at a loss"},
                {"key": "take_profit_pct", "label": "Take-profit", "display": f"{tp:.2f}%",
                 "hint": "Distance from entry where the position is closed at a gain"},
            ],
            # The dashboard draws a gauge for any indicator that has a range. "zone" says which part of the
            # gauge to shade as "conditions met" (outside / between / below / above the marks), and
            # "spark" asks for a small history line under the gauges.
            "indicators": [
                {"key": "rsi", "label": f"RSI ({p})", "decimals": 1, "range": [0, 100],
                 "zone": "outside", "spark": True,
                 "marks": [{"value": low, "label": "Oversold"}, {"value": high, "label": "Overbought"}]},
            ],
        }


# ---------------------------------------------------------------------------
# 2. Opening Range Breakout (5-minute bars, in and out the same day)
# ---------------------------------------------------------------------------

class OpeningRangeBreakout(Strategy):
    id = "orb"
    name = "Opening Range Breakout"
    description = (
        "Looks at the first 5-minute candle of the day. If it closed up, the bot goes long; "
        "if it closed down, it goes short. The stop-loss sits at the other end of that candle, "
        "and everything is closed shortly before the market closes. It only trades stocks "
        "whose opening volume is at least normal, because the idea works best on busy days."
    )
    kind = "intraday"
    key_suffix = "_ORB"
    log_every_seconds = 300
    one_entry_per_day = True
    flatten_minutes_before_close = 5
    default_params = {
        "min_rel_volume": 1.0,       # opening volume vs the recent average, 1.0 = normal
        "rel_volume_days": 14,       # how many past sessions make up that average
        "entry_window_minutes": 15,  # how long after the first candle the bot may still enter
        "target_r": 10.0,            # take-profit distance as a multiple of the risk (stop distance)
        "risk_per_trade_pct": 0.005,  # share of the account risked per trade
        "max_position_pct": 0.25,    # largest position as a share of the account
        "min_range_pct": 0.0005,     # first candle must be at least this big (0.05% of price)
        "max_range_pct": 0.015,      # ...and no bigger than this (1.5%)
        "min_risk_pct": 0.0003,      # skip if the stop would be closer than this to the entry
    }

    def data_requests(self, now_utc, ctx, params):
        open_et = datetime.combine(ctx.now_et.date(), OPEN_T, tzinfo=ET)
        open_utc = open_et.astimezone(timezone.utc)
        minutes = (ctx.now_et - open_et).total_seconds() / 60
        busy = minutes <= 5 + params["entry_window_minutes"] + 5
        days = int(params["rel_volume_days"] * 1.6) + 4
        return [
            {"key": "today", "timeframe": "5Min", "start": open_utc, "cache_seconds": 0 if busy else 900},
            # the latest 1-minute bars, so the entry price is fresh rather than the close of the 9:30 candle
            {"key": "recent", "timeframe": "1Min", "start": now_utc - timedelta(minutes=15),
             "cache_seconds": 0 if busy else 900},
            {"key": "history", "timeframe": "5Min", "start": now_utc - timedelta(days=days),
             "end": open_utc, "cache_seconds": 6 * 3600},
        ]

    @staticmethod
    def _first_bar_volumes(history, today):
        by_date = {}
        for b in history:
            e = b.t.astimezone(ET)
            if e.time() == OPEN_T and e.date() < today:
                by_date[e.date()] = b.v
        return [by_date[d] for d in sorted(by_date)]

    def evaluate(self, symbol, data, params, ctx) -> Decision:
        now_et = ctx.now_et
        open_et = datetime.combine(now_et.date(), OPEN_T, tzinfo=ET)
        minutes = (now_et - open_et).total_seconds() / 60

        todays = [b for b in data.get("today", []) if b.t.astimezone(ET).date() == now_et.date()]
        price = todays[-1].c if todays else None
        recent = [b for b in data.get("recent", []) if b.t.astimezone(ET).date() == now_et.date()]
        if recent and (not todays or recent[-1].t >= todays[-1].t):
            price = recent[-1].c

        if minutes < 5:
            return Decision(symbol=symbol, price=price, signal="none", ready=False,
                            reason="Waiting for the first 5-minute candle to finish (9:35 AM ET).")

        first = next((b for b in todays if b.t.astimezone(ET).time() == OPEN_T), None)
        if first is None:
            return Decision(symbol=symbol, price=price, signal="none", ready=False,
                            reason="No trades were recorded in the first 5 minutes, so there is no opening candle.")

        # --- the opening candle -------------------------------------------------
        up, down = first.c > first.o, first.c < first.o
        direction = "long" if up else ("short" if down else "none")
        range_pct = (first.h - first.l) / first.o * 100 if first.o else 0.0

        vols = self._first_bar_volumes(data.get("history", []), now_et.date())
        vols = vols[-int(params["rel_volume_days"]):]
        rel_vol = None
        if len(vols) >= 5 and sum(vols) > 0:
            rel_vol = first.v / (sum(vols) / len(vols))

        min_rv = params["min_rel_volume"]
        lo_r, hi_r = params["min_range_pct"] * 100, params["max_range_pct"] * 100

        checks = []
        if direction == "none":
            checks.append({"label": "First 5-minute candle closed flat (no direction)", "ok": False})
        else:
            checks.append({"label": f"First 5-minute candle closed {'up' if up else 'down'}", "ok": True})
        if rel_vol is None:
            checks.append({"label": "Opening volume check skipped (not enough history yet)", "ok": True})
            volume_ok = True
        else:
            volume_ok = rel_vol >= min_rv
            checks.append({"label": f"Opening volume at least {min_rv:.1f}x the recent average ({rel_vol:.2f}x)", "ok": volume_ok})
        range_ok = lo_r <= range_pct <= hi_r
        checks.append({"label": f"Opening candle size between {lo_r:.2f}% and {hi_r:.2f}% ({range_pct:.2f}%)", "ok": range_ok})

        indicators = {
            "rel_vol": round(rel_vol, 2) if rel_vol is not None else None,
            "range_pct": round(range_pct, 3),
            "opening_high": round(first.h, 2),
            "opening_low": round(first.l, 2),
        }

        d = Decision(symbol=symbol, price=price, signal="none", reason="", indicators=indicators, checks=checks)

        if direction == "none":
            d.reason = "The first candle closed flat, so there is no direction to follow today."
            return d
        if not volume_ok:
            d.reason = f"Opening volume is only {rel_vol:.2f}x the recent average, below the {min_rv:.1f}x needed. Skipping this stock today."
            return d
        if not range_ok:
            d.reason = f"The opening candle is {range_pct:.2f}% of the price, outside the {lo_r:.2f}% to {hi_r:.2f}% range the bot accepts."
            return d

        stop = first.l if direction == "long" else first.h
        risk = abs(price - stop) if price is not None else 0
        risk_ok = price is not None and risk / price >= params["min_risk_pct"] and (
            price > stop if direction == "long" else price < stop)
        d.checks.append({"label": "Price is on the right side of the stop level with room to spare", "ok": bool(risk_ok)})
        if not risk_ok:
            d.reason = "Price is too close to (or beyond) the stop level at the other end of the opening candle."
            return d

        d.signal = direction
        d.stop_price = round(stop, 2)
        d.take_profit_price = round(price + params["target_r"] * risk if direction == "long"
                                    else price - params["target_r"] * risk, 2)
        d.reason = (f"The first 5-minute candle closed {'up' if up else 'down'} on {'strong' if rel_vol and rel_vol >= 1.5 else 'normal'} volume, "
                    f"so the bot goes {direction}, with its stop at the {'low' if up else 'high'} of that candle.")

        window_end = 5 + params["entry_window_minutes"]
        if minutes > window_end:
            d.actionable = False
            d.window_note = f"The entry window closed at {_fmt_time(window_end)} ET"
        return d

    def position_size(self, equity, price, decision, params, n_symbols):
        if not price or decision.stop_price is None:
            return 0
        risk = abs(price - decision.stop_price)
        if risk <= 0:
            return 0
        by_risk = math.floor(equity * params["risk_per_trade_pct"] / risk)
        by_cap = math.floor(equity * params["max_position_pct"] / price)
        return max(0, min(by_risk, by_cap))

    def settings(self, params, n_symbols):
        return [
            {"label": "Risk per trade", "display": f"{params['risk_per_trade_pct'] * 100:.2f}% of the account",
             "hint": "How much the account loses if the stop-loss is hit"},
            {"label": "Largest position", "display": f"{params['max_position_pct'] * 100:.0f}% of the account",
             "hint": "Caps the size when the stop is very close"},
            {"label": "Entry window", "display": f"{_fmt_time(5)} to {_fmt_time(5 + params['entry_window_minutes'])} ET",
             "hint": "The bot only enters during this time"},
            {"label": "Closes everything", "display": f"{self.flatten_minutes_before_close} min before the close",
             "hint": "No positions are held overnight"},
        ]

    def describe(self, params) -> dict:
        w_end = _fmt_time(5 + params["entry_window_minutes"])
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "timeframe": "5-minute bars",
            "schedule": f"Looks at the first candle at 9:35 AM ET, may enter until {w_end} ET, and closes everything {self.flatten_minutes_before_close} minutes before the close.",
            "rules": [
                "If the first 5-minute candle (9:30 to 9:35 AM ET) closed up, go long. If it closed down, go short.",
                f"Only trade a stock when its opening volume is at least {params['min_rel_volume']:.1f}x its recent average.",
                "The stop-loss goes at the low (long) or high (short) of that first candle.",
                f"The take-profit is {params['target_r']:g} times the risk away, so most trades end at the close instead.",
                f"Close every position {self.flatten_minutes_before_close} minutes before the market closes. One trade per stock per day.",
            ],
            "params": [
                {"key": "min_rel_volume", "label": "Minimum opening volume", "display": f"{params['min_rel_volume']:.1f}x",
                 "hint": "Compared with the average opening volume of recent sessions"},
                {"key": "rel_volume_days", "label": "Average over", "display": f"{int(params['rel_volume_days'])} sessions",
                 "hint": "How many past days make up the volume average"},
                {"key": "entry_window_minutes", "label": "Entry window", "display": f"{int(params['entry_window_minutes'])} min",
                 "hint": "How long after the first candle the bot may still enter"},
                {"key": "target_r", "label": "Take-profit", "display": f"{params['target_r']:g}x the risk",
                 "hint": "Distance of the profit target, as a multiple of the stop distance"},
                {"key": "risk_per_trade_pct", "label": "Risk per trade", "display": f"{params['risk_per_trade_pct'] * 100:.2f}%",
                 "hint": "Share of the account lost if the stop-loss is hit"},
            ],
            "indicators": [
                {"key": "rel_vol", "label": "Opening volume vs average", "decimals": 2, "range": [0, 3],
                 "zone": "above",
                 "marks": [{"value": params["min_rel_volume"], "label": "Minimum"}]},
                {"key": "range_pct", "label": "Opening candle size (% of price)", "decimals": 2, "range": [0, 2],
                 "zone": "between",
                 "marks": [{"value": params["min_range_pct"] * 100, "label": "Min"},
                           {"value": params["max_range_pct"] * 100, "label": "Max"}]},
            ],
        }


# ---------------------------------------------------------------------------
# 3. RSI(2) Pullback (daily bars, long only, holds for days)
# ---------------------------------------------------------------------------

class RSI2Swing(Strategy):
    id = "rsi2-swing"
    name = "RSI(2) Pullback"
    description = (
        "Buys short, sharp dips in stocks that are in a longer uptrend. When a stock closes far "
        "below where it was two days ago while still trading above its 200-day average, the bot "
        "buys it and sells once the price recovers above its 5-day average. Trades last a few days."
    )
    kind = "swing"
    key_suffix = "_RSI2"
    log_every_seconds = 900
    one_entry_per_day = True
    default_params = {
        "rsi_period": 2,
        "entry_rsi": 10,             # buy when the 2-day RSI is below this
        "trend_sma": 200,            # ...and the price is above this many days' average
        "exit_sma": 5,               # sell when the price closes above this many days' average
        "max_hold_days": 7,          # ...or after this many trading days
        "stop_loss_pct": 0.08,       # wide safety stop, only for disasters
        "take_profit_pct": 0.20,     # wide safety target, rarely reached
        "window_start_minutes": 15,  # place orders from this many minutes before the close...
        "window_end_minutes": 3,     # ...until this many minutes before the close
    }

    def _in_window(self, ctx, params):
        m = ctx.minutes_to_close
        return m is not None and params["window_end_minutes"] <= m <= params["window_start_minutes"]

    def data_requests(self, now_utc, ctx, params):
        days = int(params["trend_sma"] * 1.6) + 20
        return [{"key": "daily", "timeframe": "1Day", "start": now_utc - timedelta(days=days),
                 "cache_seconds": 0 if self._in_window(ctx, params) else 3600}]

    def evaluate(self, symbol, data, params, ctx) -> Decision:
        closes = [b.c for b in data.get("daily", [])]
        period, trend_n, exit_n = int(params["rsi_period"]), int(params["trend_sma"]), int(params["exit_sma"])
        price = closes[-1] if closes else None

        rsi = compute_rsi_wilder(closes, period) if len(closes) > period else None
        sma_t, sma_x = compute_sma(closes, trend_n), compute_sma(closes, exit_n)
        if rsi is None or sma_t is None or sma_x is None:
            return Decision(symbol=symbol, price=price, signal="none", ready=False,
                            reason=f"Not enough daily history yet (needs {trend_n + 1} days, has {len(closes)}).")

        indicators = {
            "rsi2": round(rsi, 1),
            "vs_sma200": round((price / sma_t - 1) * 100, 2),
            "vs_sma5": round((price / sma_x - 1) * 100, 2),
        }
        pos = ctx.position
        d = Decision(symbol=symbol, price=price, signal="none", reason="", indicators=indicators)

        if pos:
            held = None
            if pos.get("entry_date"):
                try:
                    held = weekdays_between(date.fromisoformat(pos["entry_date"]), ctx.now_et.date())
                except ValueError:
                    held = None
            above_exit = price > sma_x
            too_long = held is not None and held >= params["max_hold_days"]
            d.checks = [
                {"label": f"Price above its {exit_n}-day average (time to sell)", "ok": above_exit},
                {"label": f"Held {params['max_hold_days']} trading days or more" + (f" ({held} so far)" if held is not None else ""), "ok": too_long},
            ]
            if above_exit or too_long:
                d.signal = "exit"
                d.reason = (f"The price recovered above its {exit_n}-day average, so the bot sells."
                            if above_exit else f"The position has been held {held} trading days, so the bot sells.")
            else:
                d.reason = (f"Holding. RSI({period}) is {rsi:.1f}; the bot sells when the price closes above its "
                            f"{exit_n}-day average ({sma_x:.2f}).")
        else:
            trend_ok = price > sma_t
            dip_ok = rsi < params["entry_rsi"]
            d.checks = [
                {"label": f"Price above its {trend_n}-day average (uptrend)", "ok": trend_ok},
                {"label": f"RSI({period}) below {params['entry_rsi']:g} (sharp dip)", "ok": dip_ok},
            ]
            if trend_ok and dip_ok:
                d.signal = "long"
                d.stop_price, d.take_profit_price = bracket_levels(
                    price, "long", params["stop_loss_pct"], params["take_profit_pct"])
                d.reason = (f"RSI({period}) is {rsi:.1f}, a sharp dip, and the price is still above its {trend_n}-day average, "
                            f"so the bot buys the dip.")
            elif not trend_ok:
                d.reason = f"The price is below its {trend_n}-day average, so dips are not bought. RSI({period}) is {rsi:.1f}."
            else:
                d.reason = f"RSI({period}) is {rsi:.1f}, not low enough for a dip (needs below {params['entry_rsi']:g}). No signal."

        if d.signal in ("long", "exit") and not self._in_window(ctx, params):
            d.actionable = False
            d.window_note = (f"Orders go out in the last {params['window_start_minutes']} to "
                             f"{params['window_end_minutes']} minutes before the close")
        return d

    def settings(self, params, n_symbols):
        return [
            {"label": "Position size", "display": f"{100 / max(n_symbols, 1):.1f}% of the account",
             "hint": "Per stock, rounded down to whole shares"},
            {"label": "Order window", "display": f"Last {params['window_start_minutes']} min of the day",
             "hint": "Buys and sells happen shortly before the close, using the day's price so far"},
            {"label": "Longest hold", "display": f"{int(params['max_hold_days'])} trading days",
             "hint": "The bot sells after this long even if the price has not recovered"},
        ]

    def describe(self, params) -> dict:
        period, trend_n, exit_n = int(params["rsi_period"]), int(params["trend_sma"]), int(params["exit_sma"])
        sl, tp = params["stop_loss_pct"] * 100, params["take_profit_pct"] * 100
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "timeframe": "Daily bars",
            "schedule": f"Checks once near the close each day (last {params['window_start_minutes']} minutes). Long only.",
            "rules": [
                f"Buy when the price is above its {trend_n}-day average and RSI({period}) closes below {params['entry_rsi']:g}.",
                f"Sell when the price closes above its {exit_n}-day average.",
                f"Sell anyway after {int(params['max_hold_days'])} trading days.",
                f"A wide safety stop-loss ({sl:.0f}% away) and take-profit ({tp:.0f}% away) protect against a crash.",
                "Long only. One position per stock. Trades are rare, a few a month across the watchlist.",
            ],
            "params": [
                {"key": "rsi_period", "label": "RSI period", "display": f"{period} days",
                 "hint": "A very short RSI that reacts to a 2-day drop"},
                {"key": "entry_rsi", "label": "Buy when RSI below", "display": f"{params['entry_rsi']:g}",
                 "hint": "How sharp the dip has to be"},
                {"key": "trend_sma", "label": "Uptrend filter", "display": f"{trend_n}-day average",
                 "hint": "Only buy dips in stocks trading above this"},
                {"key": "exit_sma", "label": "Sell above", "display": f"{exit_n}-day average",
                 "hint": "Sell when the price closes above this"},
                {"key": "max_hold_days", "label": "Longest hold", "display": f"{int(params['max_hold_days'])} days",
                 "hint": "Time limit if the price never recovers"},
                {"key": "stop_loss_pct", "label": "Safety stop-loss", "display": f"{sl:.0f}%",
                 "hint": "Only for disasters"},
            ],
            "indicators": [
                {"key": "rsi2", "label": f"RSI ({period})", "decimals": 1, "range": [0, 100],
                 "zone": "below",
                 "marks": [{"value": params["entry_rsi"], "label": "Buy below"}]},
                {"key": "vs_sma200", "label": f"Price vs {trend_n}-day average (%)", "decimals": 1, "range": [-20, 20],
                 "zone": "above",
                 "marks": [{"value": 0, "label": "Uptrend above"}]},
            ],
        }


ALL_STRATEGIES = [RSIReversion, OpeningRangeBreakout, RSI2Swing]
