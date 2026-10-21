"""
Trading strategies.

File version: 3.0.0
  3.0.0: two new strategies replace Opening Range Breakout and RSI(2) Pullback:
         TJR Liquidity Sweep (sweep -> 5-minute structure shift -> 1-minute fair
         value gap) and EMA + VWAP Trend (trend pullback). Both go long and short,
         put the whole account into one position at a time, and size it from a
         risk limit (0.5% by default, never more than 2%). Every strategy now
         enters with a limit order, and the cost model (estimated slippage and
         fees per closed trade) lives here so the bot and the backtest share it.
         The two retired strategies are kept at the bottom of the file, unused.
  2.0.0: strategies get OHLCV bars and a Context, can ask for different bar
         sizes, and can set their own stop/target. Added ORB and RSI(2).
  1.0.0: RSIReversion only.

Each strategy answers one question: "given the recent prices for this
symbol, should the bot go long, go short, exit, or do nothing, and why?"

A strategy does NOT place orders. bot.py does that. The strategy only
returns a Decision, which also carries the indicator values and the
individual yes/no conditions it checked. The dashboard shows those, so you
can see how the bot read the market at every check.

To add a new strategy later: copy one of the classes below, give it a new
id/name, change data_requests(), evaluate() and describe(), and register it
in ALL_STRATEGIES at the bottom. Each strategy trades its own Alpaca paper
account; key_suffix says which pair of secrets it uses.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from indicators import (
    Bar, ET, compute_rsi, compute_rsi_wilder, compute_sma, aggregate_bars, regular_session,
    previous_session_range, find_long_setup, find_short_setup, trend_pullback_long, trend_pullback_short,
)

OPEN_T = dtime(9, 30)

# ---------------------------------------------------------------------------
# Limits and the cost model (shared by bot.py and backtest.py)
# ---------------------------------------------------------------------------

MAX_RISK_PCT = 0.02          # no strategy may risk more than this share of the account on one trade

# Paper trading fills at clean prices, real trading does not. These are ESTIMATES of what a real
# account would give up on each trade; the dashboard shows results before and after them.
COSTS = {
    "slip_entry_pct": 0.0002,         # 0.02% each way on entries (spread plus a little slippage)
    "slip_market_exit_pct": 0.0002,   # 0.02% when the bot closes a position itself
    "slip_stop_exit_pct": 0.0005,     # 0.05% when a stop-loss triggers (stops fill worse than they read)
    "slip_target_exit_pct": 0.0,      # take-profit limit orders fill at their price
    "sec_fee_per_dollar_sold": 0.0000278,   # approximate US regulatory fee on sells; check the current rate
    "taf_per_share_sold": 0.000166,         # approximate FINRA fee per share sold
    "taf_max": 8.30,
}


def trade_cost(side, qty, entry, exit_price, exit_kind, costs=COSTS) -> float:
    """Estimated cost in dollars of one round trip: slippage on both ends plus the sell-side fees."""
    slip_out = {"Stop-loss": costs["slip_stop_exit_pct"],
                "Take-profit": costs["slip_target_exit_pct"]}.get(exit_kind, costs["slip_market_exit_pct"])
    slippage = qty * entry * costs["slip_entry_pct"] + qty * exit_price * slip_out
    sold = qty * (exit_price if side == "long" else entry)
    fees = sold * costs["sec_fee_per_dollar_sold"] + min(qty * costs["taf_per_share_sold"], costs["taf_max"])
    return round(slippage + fees, 2)


def full_account_size(equity, price, stop_price, risk_pct, max_position_pct=0.95) -> int:
    """
    Whole shares for a one-position-at-a-time strategy: as many as the account allows, but never so many
    that hitting the stop would lose more than `risk_pct` of the account (and never above MAX_RISK_PCT).
    """
    if not price or price <= 0 or stop_price is None:
        return 0
    per_share = abs(price - stop_price)
    if per_share <= 0:
        return 0
    risk_pct = min(float(risk_pct), MAX_RISK_PCT)
    by_risk = math.floor(equity * risk_pct / per_share)
    by_cap = math.floor(equity * max_position_pct / price)
    return max(0, min(by_risk, by_cap))


# ---------------------------------------------------------------------------
# Plain data containers shared with bot.py
# ---------------------------------------------------------------------------

@dataclass
class Context:
    now_et: datetime
    minutes_to_close: Optional[float]     # None if unknown
    equity: float
    n_symbols: int
    position: Optional[dict] = None       # {"side", "qty", "avg_entry_price", "entry_date", "opened_at"} if holding
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
    score: float = 0.0               # when several stocks signal at once, the highest score is taken
    setup: dict = field(default_factory=dict)        # details of the setup, saved with the trade


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


def _clock(minutes_since_midnight: int) -> str:
    return datetime(2000, 1, 3, minutes_since_midnight // 60, minutes_since_midnight % 60).strftime("%-I:%M %p")


def _aware(t):
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def minutes_held(pos, ctx) -> Optional[float]:
    if not pos or not pos.get("opened_at"):
        return None
    try:
        return (ctx.now_et.astimezone(timezone.utc) - _aware(pos["opened_at"])).total_seconds() / 60
    except Exception:
        return None


def entry_window(ctx, params):
    """
    The trading windows shared by the two intraday strategies: nothing in the first minutes after the open
    (wide spreads), nothing in the midday lull, nothing close to the end of the day.
    Returns (allowed, note).
    """
    e = ctx.now_et
    m = e.hour * 60 + e.minute
    start = 9 * 60 + 30 + int(params["first_entry_minute"])
    if m < start:
        return False, f"Entries start at {_clock(start)} ET"
    if params["lunch_start_min"] <= m < params["lunch_end_min"]:
        return False, f"Midday lull ({_clock(params['lunch_start_min'])} to {_clock(params['lunch_end_min'])} ET): no new entries"
    mtc = ctx.minutes_to_close
    if mtc is not None and mtc < params["min_minutes_to_close"]:
        return False, f"Less than {int(params['min_minutes_to_close'])} minutes to the close: no new entries"
    return True, ""


def intraday_requests(now_utc, ctx):
    """Today's 1-minute bars (fresh every pass) and the previous few sessions (fetched rarely)."""
    open_utc = datetime.combine(ctx.now_et.date(), OPEN_T, tzinfo=ET).astimezone(timezone.utc)
    return [
        {"key": "today", "timeframe": "1Min", "start": open_utc, "cache_seconds": 0},
        {"key": "prev", "timeframe": "1Min", "start": now_utc - timedelta(days=5), "end": open_utc,
         "cache_seconds": 6 * 3600},
    ]


def intraday_bars(data, ctx):
    """(today's finished regular-session 1-minute bars, earlier bars)."""
    now = ctx.now_et.astimezone(timezone.utc)
    today = ctx.now_et.date()
    cur = [b for b in regular_session(data.get("today", []))
           if b.t.astimezone(ET).date() == today and b.t + timedelta(minutes=1) <= now]
    return cur, data.get("prev", [])


ENTRY_SETTING = {
    "label": "Entry order", "display": "Limit order just past the price",
    "hint": "A limit order a few hundredths of a percent beyond the last price. It is cancelled if it has not "
            "filled within 90 seconds, so the bot never chases a fast move",
}


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
    single_position = False           # True: the whole account in ONE position at a time, across all stocks
    loss_streak_limit = None          # stop for the day after this many losing trades in a row (None = never)
    entry_tif = "gtc"                 # "gtc" keeps stop-loss/take-profit alive overnight, "day" for same-day strategies

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

    def can_enter_now(self, ctx, params) -> bool:
        """False when no entry could possibly be taken right now. backtest.py uses it to skip needless work."""
        return True

    backtest_trim = {}                # {data key: how many recent bars the strategy really uses} (backtest speed-up)

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
        "entry_offset_pct": 0.0005,   # entry limit price sits this far beyond the last price
    }
    backtest_trim = {"bars": 400}

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
            ENTRY_SETTING,
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
# 2. TJR Liquidity Sweep (1-minute entries, in and out within the hour, long and short)
# ---------------------------------------------------------------------------

def _money(x):
    return f"{x:,.2f}"


def _lvl(name):
    """'Today's low' -> "today's low"; 'Previous day high' -> 'the previous day high'."""
    n = name.lower()
    return n if n.startswith("today") else f"the {n}"


def _et_hhmm(t):
    return t.astimezone(ET).strftime("%-I:%M %p")


class TJRSweep(Strategy):
    id = "tjr-sweep"
    name = "TJR Liquidity Sweep"
    description = (
        "A rule-based version of the popular liquidity sweep and fair value gap style. Traders say stop-losses "
        "pile up just beyond obvious highs and lows, and that price often runs those levels and then reverses. "
        "The bot waits for price to run the previous day's high or low (or a high or low from earlier today) and snap "
        "back, for the 5-minute chart to shift structure the other way, and for price to pull back into a gap on the "
        "1-minute chart. Then it trades in the new direction with the stop beyond the sweep. The whole account goes "
        "into one position at a time, and each trade is closed within the hour. This is an automated approximation "
        "of a style that is normally traded by eye, so the results say whether this version works, not the whole idea."
    )
    kind = "intraday"
    key_suffix = "_TJR"
    log_every_seconds = 60
    one_entry_per_day = True            # one attempt per stock per day, so a failed setup is not retried over and over
    cooldown_minutes = 10
    flatten_minutes_before_close = 10
    single_position = True
    loss_streak_limit = 3
    entry_tif = "day"
    default_params = {
        "setup_max_age": 45,          # a sweep must be this recent (minutes)
        "level_gap": 10,              # today's high/low only counts as a resting level once it is this many minutes old
        "min_sweep_pct": 0.0002,      # price must poke at least 0.02% beyond the level
        "reclaim_bars": 5,            # ...and close back inside within this many minutes
        "min_fvg_pct": 0.0003,        # the gap must be at least 0.03% of the price
        "chase_pct": 0.0005,          # entry zone reaches this far beyond the gap's edge
        "stop_buffer_pct": 0.0002,    # stop sits this far beyond the sweep's extreme
        "min_stop_pct": 0.0010,       # skip if the stop would be closer than 0.10%...
        "max_stop_pct": 0.0100,       # ...or further than 1.00%
        "target_r": 2.0,              # take-profit distance as a multiple of the risk
        "max_hold_minutes": 60,       # close the trade after this long
        "risk_per_trade_pct": 0.005,  # share of the account lost if the stop is hit (never above 2%)
        "max_position_pct": 0.95,     # largest position as a share of the account
        "first_entry_minute": 10,     # no entries in the first 10 minutes after the open
        "lunch_start_min": 690,       # 11:30 AM ET
        "lunch_end_min": 810,         # 1:30 PM ET
        "min_minutes_to_close": 40,   # no new entries in the last 40 minutes
        "entry_offset_pct": 0.0005,
    }

    def data_requests(self, now_utc, ctx, params):
        return intraday_requests(now_utc, ctx)

    def can_enter_now(self, ctx, params):
        return entry_window(ctx, params)[0]

    @staticmethod
    def _pick(up, dn):
        def key(s):
            t = s.get("sweep_time")
            return (s["stage"], t.timestamp() if t else 0)
        return ("long", up) if key(up) >= key(dn) else ("short", dn)

    def evaluate(self, symbol, data, params, ctx) -> Decision:
        cur, prev = intraday_bars(data, ctx)
        price = cur[-1].c if cur else None
        pos = ctx.position

        if pos:
            held = minutes_held(pos, ctx)
            limit = params["max_hold_minutes"]
            over = held is not None and bool(limit) and held >= limit
            d = Decision(symbol=symbol, price=price, signal="exit" if over else "none", reason="",
                         indicators={"stage": 0},
                         checks=[{"label": f"Open for {int(limit)} minutes or more", "ok": over}])
            d.reason = (f"The trade has been open {held:.0f} minutes, past the {int(limit)} minute limit, so the bot closes it."
                        if over else f"Holding a {pos['side']} position"
                        + (f" for {held:.0f} minutes" if held is not None else "")
                        + ". Its stop-loss and take-profit orders are active.")
            return d

        if len(cur) < 20:
            return Decision(symbol=symbol, price=price, signal="none", ready=False,
                            reason=f"Waiting for the day's chart to build (needs 20 one-minute bars, has {len(cur)}).")

        now = ctx.now_et.astimezone(timezone.utc)
        rng = previous_session_range(prev, ctx.now_et.date())
        pdh, pdl = (rng[1], rng[2]) if rng else (None, None)
        b5 = aggregate_bars(cur, 5, now)
        up = find_long_setup(cur, b5, pdl, params)
        dn = find_short_setup(cur, b5, pdh, params)
        direction, s = self._pick(up, dn)
        stage = s["stage"]
        long_side = direction == "long"
        indicators = {"stage": stage}
        d = Decision(symbol=symbol, price=price, signal="none", reason="", indicators=indicators)

        if stage == 0:
            d.checks = [{"label": "Price ran a resting high or low and snapped back (last "
                                  f"{int(params['setup_max_age'])} min)", "ok": False}]
            d.reason = ("No liquidity sweep yet. Price has not run the previous day's or today's high or low and snapped back.")
        else:
            word = "low" if long_side else "high"
            ago = (now - s["sweep_time"]).total_seconds() / 60
            d.checks = [
                {"label": f"Price ran {_lvl(s['sweep_name'])} ({_money(s['sweep_level'])}) and closed back inside", "ok": True},
                {"label": f"5-minute chart shifted structure {'up' if long_side else 'down'}", "ok": stage >= 2},
                {"label": f"1-minute {'bullish' if long_side else 'bearish'} fair value gap formed", "ok": stage >= 3},
                {"label": "Price has come back into the gap", "ok": stage >= 4},
            ]
            lead = f"Price ran {_lvl(s['sweep_name'])} ({_money(s['sweep_level'])}) {ago:.0f} minutes ago and closed back inside."
            if stage == 1:
                d.reason = f"{lead} Waiting for the 5-minute chart to shift structure {'up' if long_side else 'down'}."
            elif stage == 2:
                d.reason = f"{lead} The 5-minute chart has shifted {'up' if long_side else 'down'}. Waiting for a fair value gap on the 1-minute chart."
            elif stage == 3:
                d.reason = (f"{lead} A fair value gap formed between {_money(s['fvg_bottom'])} and {_money(s['fvg_top'])}. "
                            f"Waiting for price to come back into it.")

        if stage == 4 and price is not None:
            buf = params["stop_buffer_pct"]
            stop = s["extreme"] * (1 - buf) if long_side else s["extreme"] * (1 + buf)
            risk = abs(price - stop)
            risk_pct = risk / price if price else 0
            lo_s, hi_s = params["min_stop_pct"], params["max_stop_pct"]
            stop_ok = (price > stop if long_side else price < stop) and lo_s <= risk_pct <= hi_s
            d.checks.append({"label": f"Stop distance between {lo_s * 100:.2f}% and {hi_s * 100:.2f}% ({risk_pct * 100:.2f}%)",
                             "ok": bool(stop_ok)})
            if not stop_ok:
                d.reason = (f"The setup is complete, but the stop would be {risk_pct * 100:.2f}% away, outside the "
                            f"{lo_s * 100:.2f}% to {hi_s * 100:.2f}% the bot accepts. Skipping it.")
            else:
                d.signal = direction
                d.stop_price = round(stop, 2)
                d.take_profit_price = round(price + params["target_r"] * risk if long_side else price - params["target_r"] * risk, 2)
                d.score = s["fvg_height_pct"] / risk_pct if risk_pct else 0.0
                d.setup = {
                    "direction": direction, "swept": s["sweep_name"], "swept_level": round(s["sweep_level"], 2),
                    "sweep_time": _et_hhmm(s["sweep_time"]), "sweep_extreme": round(s["extreme"], 2),
                    "structure_shift_level": round(s["ref_high"], 2), "structure_shift_time": _et_hhmm(s["mss_time"]),
                    "gap_low": round(s["fvg_bottom"], 2), "gap_high": round(s["fvg_top"], 2),
                    "gap_time": _et_hhmm(s["fvg_time"]),
                }
                d.reason = (f"Price ran {_lvl(s['sweep_name'])} ({_money(s['sweep_level'])}), the 5-minute chart shifted "
                            f"{'up' if long_side else 'down'}, and price is back in a {'bullish' if long_side else 'bearish'} fair value gap "
                            f"({_money(s['fvg_bottom'])} to {_money(s['fvg_top'])}). The bot goes {direction}, with the stop "
                            f"just {'under' if long_side else 'over'} the sweep {word} ({_money(s['extreme'])}).")

        ok, note = entry_window(ctx, params)
        if stage >= 3:
            d.checks.append({"label": "Inside the trading window" if ok else note, "ok": ok})
        if d.signal in ("long", "short") and not ok:
            d.actionable = False
            d.window_note = note
        return d

    def position_size(self, equity, price, decision, params, n_symbols):
        return full_account_size(equity, price, decision.stop_price, params["risk_per_trade_pct"], params["max_position_pct"])

    def settings(self, params, n_symbols):
        return _intraday_settings(self, params)

    def describe(self, params) -> dict:
        p = params
        return {
            "id": self.id, "name": self.name, "description": self.description, "kind": self.kind,
            "timeframe": "1-minute entries, 5-minute structure",
            "schedule": ("Checks every minute. Enters between {a} and {b} ET and after {c} ET, never in the last {m} minutes, "
                         "and closes everything {f} minutes before the close.").format(
                a=_clock(9 * 60 + 30 + int(p["first_entry_minute"])), b=_clock(p["lunch_start_min"]),
                c=_clock(p["lunch_end_min"]), m=int(p["min_minutes_to_close"]), f=self.flatten_minutes_before_close),
            "rules": [
                "Sweep: price runs past the previous day's high or low, or a high or low from earlier today, then closes back inside within a few minutes.",
                "Structure shift: a finished 5-minute candle closes beyond the recent swing, in the opposite direction of the sweep.",
                "Fair value gap: on the 1-minute chart, a three-bar gap forms after the sweep. The bot enters when price comes back into it.",
                "Sweep of a low means long, sweep of a high means short. The stop goes just beyond the sweep.",
                f"Take-profit at {p['target_r']:g} times the risk. Closed after {int(p['max_hold_minutes'])} minutes at most, and before the market closes.",
                "The whole account goes into one position at a time. If several stocks set up together, the cleanest setup is taken.",
            ],
            "params": [
                {"key": "setup_max_age", "label": "Sweep must be within", "display": f"{int(p['setup_max_age'])} min",
                 "hint": "How recent the sweep has to be"},
                {"key": "min_sweep_pct", "label": "Smallest sweep", "display": f"{p['min_sweep_pct'] * 100:.2f}%",
                 "hint": "How far past the level price has to poke"},
                {"key": "min_fvg_pct", "label": "Smallest gap", "display": f"{p['min_fvg_pct'] * 100:.2f}%",
                 "hint": "Size of the fair value gap, as a share of the price"},
                {"key": "target_r", "label": "Take-profit", "display": f"{p['target_r']:g}x the risk",
                 "hint": "Distance of the profit target, as a multiple of the stop distance"},
                {"key": "max_hold_minutes", "label": "Longest hold", "display": f"{int(p['max_hold_minutes'])} min",
                 "hint": "A trade is closed after this long"},
                {"key": "risk_per_trade_pct", "label": "Risk per trade",
                 "display": f"{min(p['risk_per_trade_pct'], MAX_RISK_PCT) * 100:.2f}%",
                 "hint": "Share of the account lost if the stop-loss is hit. The bot never goes above 2%"},
            ],
            "indicators": [
                {"key": "stage", "label": "Setup progress (0 to 4)", "decimals": 0, "range": [0, 4], "zone": "above",
                 "marks": [{"value": 3.5, "label": "Entry"}]},
            ],
        }


def _intraday_settings(strategy, params):
    return [
        {"label": "Risk per trade", "display": f"{min(params['risk_per_trade_pct'], MAX_RISK_PCT) * 100:.2f}% of the account",
         "hint": "What the account loses if the stop-loss is hit. The bot never risks more than 2%"},
        {"label": "Position size", "display": f"Up to {params['max_position_pct'] * 100:.0f}% of the account, one at a time",
         "hint": "The whole account goes into one position. A close stop gets the full size; a wide stop gets a smaller one"},
        {"label": "Longest hold",
         "display": (f"{int(params['max_hold_minutes'])} min" if params.get("max_hold_minutes") else "Until the target, the stop, or the close"),
         "hint": "Every position is closed before the market closes"},
        {"label": "Closes everything", "display": f"{strategy.flatten_minutes_before_close} min before the close",
         "hint": "No positions are held overnight"},
        {"label": "Pause after losses", "display": f"{strategy.loss_streak_limit} losing trades in a row",
         "hint": "No new trades for the rest of the day"},
        ENTRY_SETTING,
    ]


# ---------------------------------------------------------------------------
# 3. EMA + VWAP Trend (trend following, pullback entries, long and short)
# ---------------------------------------------------------------------------

class EMAVWAPTrend(Strategy):
    id = "ema-vwap"
    name = "EMA + VWAP Trend"
    description = (
        "A trend-following strategy. When a stock is trending (price on the right side of the day's VWAP and a fast "
        "moving average pulled away from a slow one), the bot waits for the first dip against the trend, buys it in an "
        "uptrend or sells it in a downtrend, and rides the move with a stop beyond the dip. It is the opposite of "
        "RSI Reversion: that one bets on snap-backs, this one bets that strong moves keep going. The whole account goes "
        "into one position at a time and everything is closed before the market closes."
    )
    kind = "intraday"
    key_suffix = "_TREND"
    log_every_seconds = 60
    cooldown_minutes = 15
    flatten_minutes_before_close = 10
    single_position = True
    loss_streak_limit = 3
    entry_tif = "day"
    default_params = {
        "fast_ema": 9,                # on 5-minute candles
        "slow_ema": 21,
        "pullback_bars": 8,           # how many recent 1-minute bars count as "the pullback"
        "touch_pct": 0.0003,          # the dip must reach within 0.03% of the fast average
        "max_dip_atr": 0.5,           # ...but not drop more than half an ATR below the slow average
        "min_trend_gap_pct": 0.0003,  # fast and slow averages must be at least 0.03% apart
        "stop_atr_mult": 0.3,         # stop sits this many ATRs beyond the pullback's extreme
        "min_stop_pct": 0.0010,
        "max_stop_pct": 0.0100,
        "target_r": 2.0,
        "max_hold_minutes": 0,        # 0 = no time limit, the trade ends at its stop, target or the close
        "risk_per_trade_pct": 0.005,
        "max_position_pct": 0.95,
        "first_entry_minute": 10,
        "lunch_start_min": 690,
        "lunch_end_min": 810,
        "min_minutes_to_close": 40,
        "entry_offset_pct": 0.0005,
    }

    def data_requests(self, now_utc, ctx, params):
        return intraday_requests(now_utc, ctx)

    def can_enter_now(self, ctx, params):
        return entry_window(ctx, params)[0]

    _prev5 = (None, [])

    def _earlier_candles(self, prev):
        """The 5-minute candles of the earlier sessions (they never change during a day, so work them out once)."""
        key = (id(prev), len(prev), prev[-1].t if prev else None)
        if self._prev5[0] != key:
            self._prev5 = (key, aggregate_bars(prev, 5))
        return self._prev5[1]

    def evaluate(self, symbol, data, params, ctx) -> Decision:
        cur, prev = intraday_bars(data, ctx)
        price = cur[-1].c if cur else None
        pos = ctx.position

        if pos:
            held = minutes_held(pos, ctx)
            limit = params["max_hold_minutes"]
            over = bool(limit) and held is not None and held >= limit
            d = Decision(symbol=symbol, price=price, signal="exit" if over else "none", reason="",
                         indicators={"trend_stage": 0})
            d.reason = (f"The trade has been open {held:.0f} minutes, past its {int(limit)} minute limit, so the bot closes it."
                        if over else f"Holding a {pos['side']} position"
                        + (f" for {held:.0f} minutes" if held is not None else "")
                        + ". Its stop-loss and take-profit orders are active.")
            return d

        if len(cur) < 12:
            return Decision(symbol=symbol, price=price, signal="none", ready=False,
                            reason=f"Waiting for the day's chart to build (needs 12 one-minute bars, has {len(cur)}).")

        now = ctx.now_et.astimezone(timezone.utc)
        b5 = self._earlier_candles(prev) + aggregate_bars(cur, 5, now)
        up = trend_pullback_long(cur, b5, params)
        dn = trend_pullback_short(cur, b5, params)
        if not up.get("ready") or not dn.get("ready"):
            return Decision(symbol=symbol, price=price, signal="none", ready=False,
                            reason="Not enough 5-minute history yet to measure the trend.")

        if up["stage"] != dn["stage"]:
            direction, s = ("long", up) if up["stage"] > dn["stage"] else ("short", dn)
        else:
            direction, s = ("long", up) if up["vs_vwap_pct"] >= 0 else ("short", dn)
        long_side = direction == "long"
        real_gap = s["gap_pct"] if long_side else -s["gap_pct"]
        d = Decision(symbol=symbol, price=price, signal="none", reason="", indicators={
            "vs_vwap_pct": round(s["vs_vwap_pct"], 3), "ema_gap_pct": round(real_gap * 100, 3), "trend_stage": s["stage"]})

        fast, slow = int(params["fast_ema"]), int(params["slow_ema"])
        side_word = "above" if long_side else "below"
        d.checks = [
            {"label": f"Trend {'up' if long_side else 'down'}: price {side_word} VWAP, EMA {fast} {side_word} EMA {slow}, EMA {slow} "
                      f"{'rising' if long_side else 'falling'}", "ok": s["trend"]},
            {"label": f"Dip against the trend reached EMA {fast} and held {side_word if long_side else 'below'} EMA {slow}",
             "ok": bool(s["trend"] and s["touched"] and s["held"])},
            {"label": f"Latest 1-minute bar turned back {'up' if long_side else 'down'} through the previous bar's {'high' if long_side else 'low'}",
             "ok": bool(s["trend"] and s["touched"] and s["held"] and s["trigger"])},
        ]

        stage = s["stage"]
        if stage == 0:
            d.reason = (f"No clear trend. Price is {s['vs_vwap_pct']:+.2f}% from VWAP and the EMA {fast} / EMA {slow} gap is "
                        f"{real_gap * 100:+.3f}%.")
        elif stage == 1:
            d.reason = f"{'Uptrend' if long_side else 'Downtrend'} in place. Waiting for a dip against it that reaches EMA {fast}."
        elif stage == 2:
            d.reason = f"The dip reached EMA {fast} and held. Waiting for a bar that turns back with the trend."
        else:
            extreme = s["pullback_low"]
            buf = params["stop_atr_mult"] * s["atr5"]
            stop = extreme - buf if long_side else extreme + buf
            risk = abs(price - stop)
            risk_pct = risk / price if price else 0
            lo_s, hi_s = params["min_stop_pct"], params["max_stop_pct"]
            stop_ok = (price > stop if long_side else price < stop) and lo_s <= risk_pct <= hi_s
            d.checks.append({"label": f"Stop distance between {lo_s * 100:.2f}% and {hi_s * 100:.2f}% ({risk_pct * 100:.2f}%)",
                             "ok": bool(stop_ok)})
            if not stop_ok:
                d.reason = (f"The setup is complete, but the stop would be {risk_pct * 100:.2f}% away, outside the "
                            f"{lo_s * 100:.2f}% to {hi_s * 100:.2f}% the bot accepts. Skipping it.")
            else:
                d.signal = direction
                d.stop_price = round(stop, 2)
                d.take_profit_price = round(price + params["target_r"] * risk if long_side else price - params["target_r"] * risk, 2)
                d.score = abs(real_gap) * 1000 / max(risk_pct * 100, 0.01)
                d.setup = {
                    "direction": direction, "vwap": round(s["vwap"], 2), "fast_ema": round(s["fast"], 2),
                    "slow_ema": round(s["slow"], 2), "pullback_extreme": round(extreme, 2),
                    "ema_gap_pct": round(real_gap * 100, 3),
                }
                d.reason = (f"{'Uptrend' if long_side else 'Downtrend'} (price {s['vs_vwap_pct']:+.2f}% from VWAP, EMA {fast} "
                            f"{side_word} EMA {slow}). The dip to EMA {fast} held and price turned back, so the bot goes {direction}, "
                            f"with the stop {'under' if long_side else 'over'} the dip ({_money(extreme)}).")

        ok, note = entry_window(ctx, params)
        if stage >= 2:
            d.checks.append({"label": "Inside the trading window" if ok else note, "ok": ok})
        if d.signal in ("long", "short") and not ok:
            d.actionable = False
            d.window_note = note
        return d

    def position_size(self, equity, price, decision, params, n_symbols):
        return full_account_size(equity, price, decision.stop_price, params["risk_per_trade_pct"], params["max_position_pct"])

    def settings(self, params, n_symbols):
        return _intraday_settings(self, params)

    def describe(self, params) -> dict:
        p = params
        fast, slow = int(p["fast_ema"]), int(p["slow_ema"])
        return {
            "id": self.id, "name": self.name, "description": self.description, "kind": self.kind,
            "timeframe": "1-minute entries, 5-minute trend",
            "schedule": ("Checks every minute. Enters between {a} and {b} ET and after {c} ET, never in the last {m} minutes, "
                         "and closes everything {f} minutes before the close.").format(
                a=_clock(9 * 60 + 30 + int(p["first_entry_minute"])), b=_clock(p["lunch_start_min"]),
                c=_clock(p["lunch_end_min"]), m=int(p["min_minutes_to_close"]), f=self.flatten_minutes_before_close),
            "rules": [
                f"Uptrend: price above the day's VWAP, the {fast}-period average above the {slow}-period average (5-minute candles), and the {slow}-period average rising. Downtrend is the mirror image.",
                f"Pullback: within the last {int(p['pullback_bars'])} minutes price dipped to the {fast}-period average without breaking well below the {slow}-period one.",
                "Trigger: the latest 1-minute bar turns back with the trend and closes beyond the previous bar's high (long) or low (short).",
                f"The stop goes just beyond the dip. Take-profit at {p['target_r']:g} times the risk, or the trade ends at the close.",
                "The whole account goes into one position at a time. If several stocks set up together, the strongest trend is taken.",
            ],
            "params": [
                {"key": "fast_ema", "label": "Fast average", "display": f"{fast} candles", "hint": "5-minute candles"},
                {"key": "slow_ema", "label": "Slow average", "display": f"{slow} candles", "hint": "5-minute candles"},
                {"key": "pullback_bars", "label": "Pullback window", "display": f"{int(p['pullback_bars'])} min",
                 "hint": "How far back the dip may have happened"},
                {"key": "min_trend_gap_pct", "label": "Smallest trend gap", "display": f"{p['min_trend_gap_pct'] * 100:.2f}%",
                 "hint": "How far apart the two averages must be to call it a trend"},
                {"key": "target_r", "label": "Take-profit", "display": f"{p['target_r']:g}x the risk",
                 "hint": "Distance of the profit target, as a multiple of the stop distance"},
                {"key": "risk_per_trade_pct", "label": "Risk per trade",
                 "display": f"{min(p['risk_per_trade_pct'], MAX_RISK_PCT) * 100:.2f}%",
                 "hint": "Share of the account lost if the stop-loss is hit. The bot never goes above 2%"},
            ],
            "indicators": [
                {"key": "vs_vwap_pct", "label": "Price vs VWAP (%)", "decimals": 2, "range": [-1, 1], "zone": "outside",
                 "marks": [{"value": -0.05, "label": "Down"}, {"value": 0.05, "label": "Up"}]},
                {"key": "ema_gap_pct", "label": f"EMA {fast} vs EMA {slow} (%)", "decimals": 3, "range": [-0.3, 0.3], "zone": "outside",
                 "marks": [{"value": -p["min_trend_gap_pct"] * 100, "label": "Down"}, {"value": p["min_trend_gap_pct"] * 100, "label": "Up"}]},
                {"key": "trend_stage", "label": "Setup progress (0 to 3)", "decimals": 0, "range": [0, 3], "zone": "above",
                 "marks": [{"value": 2.5, "label": "Entry"}]},
            ],
        }


# ---------------------------------------------------------------------------
# Retired strategies (kept for reference, not run). To bring one back, add its class to ALL_STRATEGIES
# and give it a pair of secrets named with its key_suffix.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Opening Range Breakout (5-minute bars, in and out the same day)
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
# RSI(2) Pullback (daily bars, long only, holds for days)
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


ALL_STRATEGIES = [RSIReversion, TJRSweep, EMAVWAPTrend]
