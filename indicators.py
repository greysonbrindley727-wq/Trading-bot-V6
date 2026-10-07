"""
Shared indicator math. Both bot.py (live trading) and tune.py (nightly
backtesting) import from here, so the signal the bot actually trades on
and the signal the tuner backtests are guaranteed to be the same formula.

File version: 1.1.0
  1.1.0: added compute_sma and compute_rsi_wilder (used by the RSI(2) swing strategy).
         compute_rsi is unchanged, so the nightly tuner behaves exactly as before.
"""


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
