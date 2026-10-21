"""
Shared indicator and chart-pattern math. bot.py (live trading), backtest.py
(history replay) and tune.py (the nightly suggestion job) all import from
here, so the signal the bot trades on is the same formula the others test.

File version: 2.0.0
  2.0.0: added Bar, 5-minute aggregation, EMA, VWAP, ATR, and the chart-pattern
         finders for the TJR-style strategy (liquidity sweep, structure shift,
         fair value gap) and the EMA + VWAP trend strategy.
         compute_rsi, compute_sma and compute_rsi_wilder are unchanged.
  1.1.0: added compute_sma and compute_rsi_wilder.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass
class Bar:
    t: datetime          # start of the bar, timezone-aware (UTC)
    o: float
    h: float
    l: float
    c: float
    v: float


# ---------------------------------------------------------------------------
# RSI and simple averages (unchanged from earlier versions)
# ---------------------------------------------------------------------------

def compute_rsi(closes, period=14):
    """
    Simple (non-Wilder-smoothed) RSI over the most recent `period` price
    changes in `closes`. Returns None if there isn't enough history yet.
    Only the last `period + 1` prices in `closes` are used, so it's fine
    to pass a longer list.
    """
    if len(closes) < period + 1:
        return None

    window = closes[-(period + 1):]
    gains, losses = [], []
    for i in range(1, len(window)):
        change = window[i] - window[i - 1]
        if change > 0:
            gains.append(change)
        else:
            losses.append(-change)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_sma(values, period):
    """Simple moving average of the last `period` values, or None if there are too few."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def compute_rsi_wilder(closes, period=14):
    """
    RSI with Wilder's smoothing, the textbook version (and the one the
    RSI(2) strategy was written for). It uses the whole list, so pass a
    long history (a couple of hundred closes) for a settled value.
    Returns None if there isn't enough history yet.
    """
    if len(closes) < period + 1:
        return None

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------------
# EMA, VWAP, ATR
# ---------------------------------------------------------------------------

def ema_series(values, period):
    """
    Exponential moving average at every point. The first `period - 1` entries
    are None; the average starts from the simple average of the first `period`
    values.
    """
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def vwap(bars):
    """Volume-weighted average price of the given bars (typical price = (high + low + close) / 3)."""
    vol = sum(b.v for b in bars)
    if vol <= 0:
        return None
    return sum((b.h + b.l + b.c) / 3.0 * b.v for b in bars) / vol


def atr(bars, period=14):
    """Average true range over the last `period` bars, or None if there are too few bars."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        b, prev = bars[i], bars[i - 1]
        trs.append(max(b.h - b.l, abs(b.h - prev.c), abs(b.l - prev.c)))
    return sum(trs) / period


# ---------------------------------------------------------------------------
# Bars: regular-hours filter, 5-minute candles, mirroring for shorts
# ---------------------------------------------------------------------------

def _minutes_since_open(t):
    e = t.astimezone(ET)
    return e, (e - e.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60.0


def regular_session(bars):
    """Only the bars inside the regular session, 9:30 to 4:00 PM ET."""
    out = []
    for b in bars:
        _, m = _minutes_since_open(b.t)
        if 0 <= m < 390:
            out.append(b)
    return out


def aggregate_bars(bars, minutes, now=None):
    """
    Group 1-minute bars into `minutes`-minute candles that start on the session
    clock (9:30, 9:35, ...). If `now` is given, a candle that has not finished
    yet is left out, so a half-built candle never triggers a signal.
    """
    out, cur, cur_key = [], None, None
    for b in bars:
        e, m = _minutes_since_open(b.t)
        if m < 0 or m >= 390:
            continue
        bucket = int(m // minutes)
        key = (e.date(), bucket)
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            start = (e.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=bucket * minutes))
            cur = Bar(start.astimezone(timezone.utc), b.o, b.h, b.l, b.c, b.v)
            cur_key = key
        else:
            cur.h, cur.l, cur.c, cur.v = max(cur.h, b.h), min(cur.l, b.l), b.c, cur.v + b.v
    if cur is not None:
        out.append(cur)
    if now is not None:
        out = [c for c in out if c.t + timedelta(minutes=minutes) <= now]
    return out


def previous_session_range(bars, today):
    """(date, high, low) of the most recent regular session before `today`, or None."""
    days = {}
    for b in regular_session(bars):
        d = b.t.astimezone(ET).date()
        if d < today:
            days.setdefault(d, []).append(b)
    if not days:
        return None
    d = max(days)
    return d, max(b.h for b in days[d]), min(b.l for b in days[d])


def mirror_bars(bars):
    """
    The same chart flipped upside down (prices negated, high and low swapped).
    The short-side rules are the long-side rules run on this mirror image, so
    the two sides can never drift apart.
    """
    return [Bar(b.t, -b.o, -b.l, -b.h, -b.c, b.v) for b in bars]


# ---------------------------------------------------------------------------
# TJR-style setup: liquidity sweep -> 5-minute structure shift -> 1-minute fair value gap
# ---------------------------------------------------------------------------

def find_long_setup(bars1, bars5, prev_low, p):
    """
    Look for the long version of the setup on today's completed 1-minute bars
    (`bars1`) and completed 5-minute candles (`bars5`).

      1. Sweep: a recent bar pushes below a resting low (the previous day's low,
         or today's low from at least `level_gap` bars earlier) and price closes
         back above that low soon after.
      2. Structure shift: a finished 5-minute candle after the sweep closes above
         the highest high of the 5-minute candles just before the sweep.
      3. Fair value gap: three 1-minute bars after the sweep where the third
         bar's low is above the first bar's high, with price not yet closed
         back below the gap.
      4. Entry zone: the latest bar has traded back down into the gap.

    Returns a dict with "stage" 0 to 4 and the details found so far.
    """
    n = len(bars1)
    res = {"stage": 0}
    if n < 15:
        return res

    gap = int(p["level_gap"])
    prefix_min = [None] * (n + 1)          # prefix_min[k] = lowest low among bars1[:k]
    lo = None
    for k in range(n):
        prefix_min[k] = lo
        lo = bars1[k].l if lo is None else min(lo, bars1[k].l)
    prefix_min[n] = lo

    sweep = None
    first = max(5, n - int(p["setup_max_age"]))
    for i in range(first, n):
        b = bars1[i]
        levels = []
        if prev_low is not None:
            levels.append(("Previous day low", prev_low))
        if i - gap >= 5 and prefix_min[i - gap] is not None:
            levels.append(("Today's low", prefix_min[i - gap]))
        for name, level in levels:
            if b.l >= level - abs(level) * p["min_sweep_pct"]:
                continue
            j = next((k for k in range(i, min(n, i + int(p["reclaim_bars"]) + 1)) if bars1[k].c > level), None)
            if j is None or not all(x.c > level for x in bars1[j:]):
                continue
            sweep = {"i": i, "j": j, "level": level, "name": name}
            break
        if sweep is not None:
            break          # the FIRST bar that ran the level, so the stop is measured from the real low
    if sweep is None:
        return res

    i = sweep["i"]
    res.update(stage=1, sweep_name=sweep["name"], sweep_level=sweep["level"], sweep_time=bars1[i].t,
               extreme=min(x.l for x in bars1[i:]))

    # --- structure shift on 5-minute candles
    sweep_t = bars1[i].t
    pre = [c for c in bars5 if c.t + timedelta(minutes=5) <= sweep_t]
    ref_high = max((c.h for c in pre[-4:]), default=None)
    mss = None
    if ref_high is not None:
        mss = next((c for c in bars5 if c.t + timedelta(minutes=5) > sweep_t and c.c > ref_high), None)
    res["ref_high"] = ref_high

    # --- fair value gap on 1-minute bars (the most recent one that has not been closed through)
    fvg = None
    for k in range(i, n - 2):
        b1, b2, b3 = bars1[k], bars1[k + 1], bars1[k + 2]
        if b3.l > b1.h and b2.c > b2.o and (b3.l - b1.h) / abs(b2.c) >= p["min_fvg_pct"]:
            if all(x.c >= b1.h for x in bars1[k + 3:]):
                fvg = {"bottom": b1.h, "top": b3.l, "time": b3.t, "height_pct": (b3.l - b1.h) / abs(b2.c)}
    if mss is None:
        return res
    res.update(stage=2, mss_time=mss.t + timedelta(minutes=5))
    if fvg is None:
        return res
    res.update(stage=3, fvg_bottom=fvg["bottom"], fvg_top=fvg["top"], fvg_time=fvg["time"],
               fvg_height_pct=fvg["height_pct"])

    last = bars1[-1]
    if last.l <= fvg["top"] and fvg["bottom"] <= last.c <= fvg["top"] + abs(fvg["top"]) * p["chase_pct"]:
        res["stage"] = 4
    return res


def _unmirror(res):
    """Turn a result found on the mirrored chart back into real prices (a short setup)."""
    out = dict(res)
    for key in ("sweep_level", "extreme", "ref_high"):
        if out.get(key) is not None:
            out[key] = -out[key]
    if "fvg_bottom" in out:
        out["fvg_bottom"], out["fvg_top"] = -res["fvg_top"], -res["fvg_bottom"]
    names = {"Previous day low": "Previous day high", "Today's low": "Today's high"}
    if "sweep_name" in out:
        out["sweep_name"] = names.get(out["sweep_name"], out["sweep_name"])
    return out


def find_short_setup(bars1, bars5, prev_high, p):
    """The short version: a sweep of a resting high, a bearish structure shift and a bearish gap."""
    res = find_long_setup(mirror_bars(bars1), mirror_bars(bars5), None if prev_high is None else -prev_high, p)
    return _unmirror(res)


# ---------------------------------------------------------------------------
# EMA + VWAP trend pullback
# ---------------------------------------------------------------------------

def trend_pullback_long(bars1, bars5, p):
    """
    Long version of the trend pullback.

      Trend: price above today's VWAP, the fast EMA above the slow EMA (5-minute
             candles) by at least `min_trend_gap_pct`, and the slow EMA rising.
      Pullback: within the last `pullback_bars` 1-minute bars the price dipped to the
             fast EMA, but not further than `max_dip_atr` ATRs below the slow EMA.
      Trigger: the latest 1-minute bar is an up bar that closes above the previous
             bar's high (the pullback is over).

    Returns a dict. "ready" is False while there is too little history. "stage" counts
    how many of trend, pullback and trigger are true (0 to 3).
    """
    closes5 = [c.c for c in bars5]
    fast = ema_series(closes5, int(p["fast_ema"]))
    slow = ema_series(closes5, int(p["slow_ema"]))
    a5 = atr(bars5, 14)
    vw = vwap(bars1)
    nb = int(p["pullback_bars"])
    if len(bars1) < nb + 2 or slow[-1] is None or len(slow) < 4 or slow[-4] is None or a5 is None or vw is None:
        return {"ready": False, "stage": 0}

    price, last, prev = bars1[-1].c, bars1[-1], bars1[-2]
    f, s = fast[-1], slow[-1]
    gap_pct = (f - s) / abs(price)
    trend = price > vw and gap_pct >= p["min_trend_gap_pct"] and s > slow[-4]
    window = bars1[-nb:]
    pb_low = min(x.l for x in window)
    touched = pb_low <= f + abs(f) * p["touch_pct"]
    held = pb_low >= s - p["max_dip_atr"] * a5
    trigger = last.c > last.o and last.c > prev.h
    stage = int(trend) + int(trend and touched and held) + int(trend and touched and held and trigger)
    return {"ready": True, "stage": stage, "trend": trend, "touched": touched, "held": held, "trigger": trigger,
            "price": price, "vwap": vw, "fast": f, "slow": s, "gap_pct": gap_pct, "atr5": a5,
            "pullback_low": pb_low, "vs_vwap_pct": (price - vw) / abs(vw) * 100}


def trend_pullback_short(bars1, bars5, p):
    res = trend_pullback_long(mirror_bars(bars1), mirror_bars(bars5), p)
    if not res.get("ready"):
        return res
    out = dict(res)
    for key in ("price", "vwap", "fast", "slow", "pullback_low"):
        out[key] = -res[key]
    # gap_pct stays as measured on the mirror image (positive = a downtrend); "pullback_low" is really
    # the pullback HIGH here. vs_vwap_pct is the real signed distance (negative = below VWAP).
    out["vs_vwap_pct"] = (out["price"] - out["vwap"]) / abs(out["vwap"]) * 100
    return out
