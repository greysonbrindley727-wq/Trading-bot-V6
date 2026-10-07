"""
Automated paper-trading bot for Alpaca (v7, multi-strategy edition).

File version: 7.0.0
  7.0.0: runs several strategies in ONE job, each on its own Alpaca paper
         account. Adds closed-trade tracking, end-of-day flattening, daily
         bars, a watchlist of 8 stocks, and a not-configured state so a
         strategy without API keys is shown on the dashboard instead of
         breaking the others.
  6.1.0: bracket orders stay active overnight instead of expiring at the close.

How it fits together:

  strategies.py   the trading rules (what to buy or sell, and why)
  bot.py          this file: runs every strategy, places orders, records everything
  publisher.py    writes the dashboard's data files to the dashboard-data branch
  index.html      the dashboard website
  config.json     the RSI Reversion settings, rewritten nightly by tune.py

Each strategy trades its own Alpaca paper account, with its own API keys:

  RSI Reversion            APCA_API_KEY_ID        APCA_API_SECRET_KEY
  Opening Range Breakout   APCA_API_KEY_ID_ORB    APCA_API_SECRET_KEY_ORB
  RSI(2) Pullback          APCA_API_KEY_ID_RSI2   APCA_API_SECRET_KEY_RSI2

A strategy whose keys are missing is skipped and shown as "not connected" on
the dashboard. Recording is best-effort: if anything in the dashboard code
fails, trading carries on.
"""

import os
import sys
import time
import math
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file (e.g. in GitHub Actions)
except ImportError:
    pass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, GetOrdersRequest, StopLossRequest, TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from strategies import ALL_STRATEGIES, Bar, Context, ET
from publisher import Publisher, utc_now_iso

# ---------------------------------------------------------------------------
# Fixed configuration
# ---------------------------------------------------------------------------

VERSION = "7.0.0"

# Every strategy watches these stocks. To give one strategy its own list, add
# it below, for example:  WATCHLISTS = {"orb": ["SPY", "QQQ", "NVDA"]}
WATCHLIST = ["AAPL", "MSFT", "SPY", "QQQ", "NVDA", "TSLA", "AMD", "PLTR"]
WATCHLISTS = {}

MAX_DAILY_LOSS_PCT = 0.03   # stop opening new trades if today's drawdown hits 3% (per account)
LOOP_SECONDS = 60                # check every 60 seconds
MAX_RUNTIME_MINUTES = 330        # stop before GitHub's 6-hour job limit
MAX_WAIT_FOR_OPEN_MINUTES = 120  # if the market opens later than this, just exit
MAX_CONSECUTIVE_ERRORS = 5       # a strategy stops (and you are alerted) after this many failures in a row
PUBLISH_EVERY_SECONDS = 300      # how often dashboard data is pushed to GitHub (and an account-value point recorded)

WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()  # optional

TIMEFRAMES = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradingbot")


# ---------------------------------------------------------------------------
# Clock helpers (the tests replace these with a simulated clock)
# ---------------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def mono():
    return time.monotonic()


def sleep(seconds):
    time.sleep(seconds)


def iso_now():
    return now_utc().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _val(x):
    """Enums (like OrderSide.BUY) -> their plain string value."""
    return getattr(x, "value", x)


def _iso(x):
    return x.isoformat() if hasattr(x, "isoformat") else (str(x) if x is not None else None)


def _g(obj, name, default=None):
    return getattr(obj, name, default)


def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        log.warning(f"Dashboard snapshot skipped one section: {e}")
        return default


def _aware(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def notify(message: str) -> None:
    """Log a message and, if configured, post it to a Discord/Slack webhook."""
    log.info(message)
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        log.warning(f"Could not send notification: {e}")


def credentials_for(strategy):
    key = os.environ.get("APCA_API_KEY_ID" + strategy.key_suffix, "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY" + strategy.key_suffix, "").strip()
    return (key, secret) if key and secret else None


def run_url():
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if repo and run_id:
        return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{run_id}"
    return None


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def fetch_bars(client, symbol, timeframe, start, end=None):
    """Bars for one symbol as a list of strategies.Bar, oldest first."""
    kwargs = dict(symbol_or_symbols=[symbol], timeframe=timeframe, start=start,
                  feed=DataFeed.IEX)   # free data feed, no subscription needed
    if end is not None:
        kwargs["end"] = end
    result = client.get_stock_bars(StockBarsRequest(**kwargs))

    raw = None
    try:
        raw = result.data.get(symbol)
    except Exception:
        raw = None
    if raw is None:
        raw = _rows_from_dataframe(result)

    bars = []
    for b in raw or []:
        bars.append(Bar(
            t=_aware(_g(b, "timestamp")),
            o=float(_g(b, "open")), h=float(_g(b, "high")), l=float(_g(b, "low")),
            c=float(_g(b, "close")), v=float(_g(b, "volume") or 0),
        ))
    return bars


def _rows_from_dataframe(result):
    try:
        df = result.df
        if df is None or df.empty:
            return []
        return [SimpleNamespace(**{k: r.get(k) for k in ("timestamp", "open", "high", "low", "close", "volume")})
                for r in df.reset_index().to_dict("records")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# One strategy on one Alpaca account
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, strategy, pub, default_symbols):
        self.s = strategy
        self.id = strategy.id
        self.store = pub.store(strategy.id)
        self.symbols = list(WATCHLISTS.get(strategy.id) or strategy.symbols or default_symbols)

        creds = credentials_for(strategy)
        self.configured = creds is not None
        self.trading = self.data = None
        if creds:
            self.trading = TradingClient(creds[0], creds[1], paper=True)
            self.data = StockHistoricalDataClient(creds[0], creds[1])

        self.params = self.load_params()
        self.saved = self.store.read_json("state.json", {}) or {}
        self.bot = {
            "state": "starting", "message": "", "started_at": utc_now_iso(), "passes": 0,
            "last_pass_at": None, "errors_in_row": 0, "last_error": None, "last_error_at": None,
        }
        self.errors = 0
        self.stopped = False
        self.final_message = None
        self.halted_day = None
        self.last_attempt = {}     # symbol -> mono() of the last order attempt
        self.last_exit = {}        # symbol -> mono() of the last exit order
        self.last_log = {}         # symbol -> mono() of the last logged check
        self.cache = {}            # (key, symbol) -> (mono, bars)
        self.market = {}

    # ----------------------------------------------------------------- setup

    def load_params(self):
        path = "config.json" if self.id == "rsi-reversion" else f"config_{self.id}.json"
        try:
            with open(path) as f:
                return {**self.s.default_params, **json.load(f)}
        except FileNotFoundError:
            if self.id == "rsi-reversion":
                log.warning(f"Could not find {path}; using defaults for {self.id}.")
            return dict(self.s.default_params)
        except Exception as e:
            log.warning(f"Could not read {path} ({e}); using defaults for {self.id}.")
            return dict(self.s.default_params)

    def set_state(self, state, message=""):
        self.bot["state"] = state
        self.bot["message"] = message

    def save_state(self):
        try:
            self.store.write_json("state.json", self.saved)
        except Exception:
            log.warning("Could not save the bot's notes between runs.", exc_info=True)

    @property
    def halted_today(self):
        return self.halted_day is not None and self.halted_day == self.saved.get("day")

    # ------------------------------------------------------------------ data

    def fetch(self, symbol, requests_):
        out = {}
        for r in requests_:
            ck = (r["key"], symbol)
            cached = self.cache.get(ck)
            ttl = r.get("cache_seconds", 0)
            if cached and ttl > 0 and mono() - cached[0] < ttl:
                out[r["key"]] = cached[1]
                continue
            bars = fetch_bars(self.data, symbol, TIMEFRAMES[r["timeframe"]], r["start"], r.get("end"))
            self.cache[ck] = (mono(), bars)
            out[r["key"]] = bars
        return out

    # ---------------------------------------------------------------- orders

    def recently_traded(self, symbol) -> bool:
        """True if an order for this symbol was attempted within the strategy's cooldown window."""
        minutes = self.s.cooldown_minutes
        if not minutes:
            return False
        last = self.last_attempt.get(symbol)
        if last is not None and mono() - last < minutes * 60:
            return True
        cutoff = now_utc() - timedelta(minutes=minutes)
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=10)
        return any(_aware(o.submitted_at) >= cutoff for o in self.trading.get_orders(req))

    def enter(self, symbol, d, equity):
        """Place a bracket order (entry + stop-loss + take-profit) and record the attempt."""
        self.last_attempt[symbol] = mono()
        attempts = self.saved.setdefault("attempts", [])
        if symbol not in attempts:
            attempts.append(symbol)

        price, direction = d.price, d.signal
        qty = self.s.position_size(equity, price, d, self.params, len(self.symbols))
        base = {
            "t": iso_now(), "symbol": symbol, "direction": direction, "kind": "entry",
            "side": "buy" if direction == "long" else "sell",
            "qty": qty, "ref_price": round(price, 2), "reason": d.reason,
            "stop_price": d.stop_price, "take_profit_price": d.take_profit_price,
        }

        if qty <= 0:
            msg = "The position size rounds down to 0 shares at this price."
            self.store.append_jsonl("orders.jsonl", {**base, "status": "not_placed", "message": msg})
            return "not_placed", msg

        try:
            order = self.trading.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if direction == "long" else OrderSide.SELL,
                time_in_force=TimeInForce.GTC,   # keeps the stop-loss/take-profit alive overnight
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=d.stop_price),
                take_profit=TakeProfitRequest(limit_price=d.take_profit_price),
            ))
        except Exception as e:
            msg = str(e)[:300]
            self.store.append_jsonl("orders.jsonl", {**base, "status": "rejected", "message": msg})
            notify(f"[{self.s.name}] Order rejected: {direction.upper()} {symbol} x{qty}. {msg}")
            return "order_rejected", msg

        order_id = str(_g(order, "id"))
        self.store.append_jsonl("orders.jsonl", {
            **base, "status": "submitted", "order_id": order_id,
            "alpaca_status": str(_val(_g(order, "status"))),
        })
        # A note so the trade is recorded even if it opens and closes between two checks.
        self.saved.setdefault("positions", {})[symbol] = {
            "opened_at": iso_now(), "side": direction, "qty": qty, "entry": price,
            "pending": True, "order_id": order_id,
        }
        notify(f"[{self.s.name}] {direction.upper()} {symbol} x{qty} @ ~${price:.2f} "
               f"(stop ${d.stop_price}, target ${d.take_profit_price})")
        return "order_submitted", f"Bracket order sent for {qty} shares."

    def exit_symbol(self, symbol, reason, pos):
        """Cancel the protective orders on a position, then close it at the market."""
        self.last_exit[symbol] = mono()
        long_side = str(_val(_g(pos, "side"))) == "long"
        base = {
            "t": iso_now(), "symbol": symbol, "direction": "exit", "kind": "exit",
            "side": "sell" if long_side else "buy", "qty": abs(_f(_g(pos, "qty"), 0.0)),
            "ref_price": _f(_g(pos, "current_price")), "reason": reason, "message": reason,
        }
        try:
            for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=50)):
                try:
                    self.trading.cancel_order_by_id(_g(o, "id"))
                except Exception as e:
                    log.warning(f"{symbol}: could not cancel order {_g(o, 'id')}: {e}")
            sleep(1)
            self.trading.close_position(symbol)
        except Exception as e:
            msg = str(e)[:300]
            self.store.append_jsonl("orders.jsonl", {**base, "status": "rejected", "message": msg})
            notify(f"[{self.s.name}] Exit rejected for {symbol}. {msg}")
            return "order_rejected", msg
        self.store.append_jsonl("orders.jsonl", {**base, "status": "submitted"})
        notify(f"[{self.s.name}] EXIT {symbol}: {reason}")
        return "order_submitted", "Closing order sent."

    def flatten_all(self, positions, clk):
        """End of day: cancel every open order and close every position."""
        today = self.saved.get("day")
        if positions:
            try:
                self.trading.close_all_positions(cancel_orders=True)
            except Exception as e:
                msg = str(e)[:300]
                log.warning(f"[{self.s.name}] Could not close all positions: {e}")
                self.store.append_jsonl("orders.jsonl", {
                    "t": iso_now(), "symbol": "ALL", "direction": "exit", "kind": "exit", "side": None,
                    "qty": None, "reason": "End of day: close everything", "status": "rejected", "message": msg})
                return False   # try again on the next pass
            for sym, p in positions.items():
                long_side = str(_val(_g(p, "side"))) == "long"
                self.store.append_jsonl("orders.jsonl", {
                    "t": iso_now(), "symbol": sym, "direction": "exit", "kind": "exit",
                    "side": "sell" if long_side else "buy", "qty": abs(_f(_g(p, "qty"), 0.0)),
                    "ref_price": _f(_g(p, "current_price")), "reason": "End of day: close everything",
                    "message": "Closed before the market closes. No positions are held overnight.",
                    "status": "submitted"})
            notify(f"[{self.s.name}] Closed {len(positions)} position(s) before the close.")
        self.saved["flattened_day"] = today
        return True

    # ---------------------------------------------------------- closed trades

    def position_info(self, symbol, pos):
        if pos is None:
            return None
        rec = self.saved.get("positions", {}).get(symbol)
        entry_date = None
        if rec and rec.get("opened_at"):
            try:
                entry_date = _aware(rec["opened_at"]).astimezone(ET).date().isoformat()
            except Exception:
                entry_date = None
        return {"side": str(_val(_g(pos, "side"))), "qty": abs(_f(_g(pos, "qty"), 0.0)),
                "avg_entry_price": _f(_g(pos, "avg_entry_price")), "entry_date": entry_date}

    def track_trades(self, positions, open_symbols):
        """Compare the positions held now with the last pass; log a closed trade for each one that went away."""
        prev = self.saved.setdefault("positions", {})
        for sym, p in positions.items():
            rec = prev.get(sym) or {"opened_at": iso_now()}
            rec.update({
                "side": str(_val(_g(p, "side"))), "qty": abs(_f(_g(p, "qty"), 0.0)),
                "entry": _f(_g(p, "avg_entry_price")), "last_price": _f(_g(p, "current_price")),
                "pending": False,
            })
            prev[sym] = rec

        for sym in list(prev):
            if sym in positions:
                continue
            rec = prev[sym]
            if rec.get("pending") and sym in open_symbols:
                continue   # the entry order is still working
            del prev[sym]
            try:
                trade = self.build_trade(sym, rec)
            except Exception:
                log.warning(f"{sym}: could not work out the closed trade.", exc_info=True)
                trade = None
            if trade:
                self.store.append_jsonl("trades.jsonl", trade)
                notify(f"[{self.s.name}] Closed {sym} {trade['side']}: "
                       f"{'+' if trade['pnl'] >= 0 else '-'}${abs(trade['pnl']):,.2f} ({trade['exit_kind']})")

    def build_trade(self, sym, rec):
        opp = "sell" if rec["side"] == "long" else "buy"
        opened = _aware(rec["opened_at"])
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[sym], limit=30))
        fills = [o for o in orders
                 if _g(o, "filled_at") is not None and str(_val(_g(o, "side"))) == opp
                 and (_f(_g(o, "filled_qty"), 0.0) or 0) > 0
                 and _aware(_g(o, "filled_at")) >= opened - timedelta(minutes=2)]

        if fills:
            o = max(fills, key=lambda x: _aware(_g(x, "filled_at")))
            exit_price = _f(_g(o, "filled_avg_price"))
            t_close = _aware(_g(o, "filled_at"))
            otype = str(_val(_g(o, "order_type") or _g(o, "type")))
            kind = ("Stop-loss" if "stop" in otype else "Take-profit" if otype == "limit" else "Market exit")
        elif rec.get("pending"):
            return None    # the entry never filled, so there was no trade
        else:
            exit_price, t_close, kind = rec.get("last_price"), now_utc(), "Closed (price approximate)"

        entry = rec.get("entry")
        if rec.get("pending") and rec.get("order_id"):
            try:
                e = _f(_g(self.trading.get_order_by_id(rec["order_id"]), "filled_avg_price"))
                entry = e if e else entry
            except Exception:
                pass
        if entry is None or exit_price is None:
            return None

        qty = rec.get("qty") or 0
        sign = 1 if rec["side"] == "long" else -1
        pnl = (exit_price - entry) * qty * sign
        return {
            "t": t_close.isoformat(timespec="seconds"), "opened_at": rec["opened_at"], "symbol": sym,
            "side": rec["side"], "qty": qty, "entry": round(entry, 2), "exit": round(exit_price, 2),
            "pnl": round(pnl, 2), "pnl_pct": round(sign * (exit_price / entry - 1) * 100, 3) if entry else None,
            "exit_kind": kind,
        }

    # ------------------------------------------------------------- one pass

    def check_symbol(self, symbol, clk, equity, positions, open_symbols, halted, no_entries):
        pos = positions.get(symbol)
        ctx = Context(
            now_et=clk.now_et, minutes_to_close=clk.minutes_to_close, equity=equity,
            n_symbols=len(self.symbols), position=self.position_info(symbol, pos),
            attempted_today=symbol in self.saved.get("attempts", []),
        )
        data = self.fetch(symbol, self.s.data_requests(clk.now_utc, ctx, self.params))
        d = self.s.evaluate(symbol, data, self.params, ctx)
        action, detail = "none", None

        if d.signal == "exit":
            blocker = None
            if not d.actionable:
                blocker = d.window_note or "Outside this strategy's trading window"
            # Note: the position's own stop-loss and take-profit orders are open too. exit_symbol()
            # cancels them before closing, so they must not block an exit.
            elif mono() - self.last_exit.get(symbol, -1e9) < 300:
                blocker = "A closing order was just sent"
            if blocker:
                d.checks.append({"label": blocker, "ok": False})
                action, detail = "skipped", blocker
            else:
                action, detail = self.exit_symbol(symbol, d.reason, pos)

        elif d.signal in ("long", "short"):
            blocker = None
            if not d.actionable:
                blocker = d.window_note or "Outside this strategy's trading window"
            elif halted:
                blocker = "Daily loss limit reached, no new trades today"
            elif no_entries:
                blocker = "Too close to the market close for new entries"
            elif symbol in positions:
                blocker = "Already holding a position in this stock"
            elif symbol in open_symbols:
                blocker = "An earlier order is still pending"
            elif self.s.one_entry_per_day and symbol in self.saved.get("attempts", []):
                blocker = "Already traded this stock today"
            elif self.recently_traded(symbol):
                blocker = f"Cooldown: an order was attempted in the last {self.s.cooldown_minutes} min"

            if blocker:
                d.checks.append({"label": blocker, "ok": False})
                action, detail = "skipped", blocker
            else:
                d.checks.append({"label": "No open position or pending order", "ok": True})
                action, detail = self.enter(symbol, d, equity)

        due = mono() - self.last_log.get(symbol, -1e9) >= self.s.log_every_seconds - 5
        if action != "none" or due:
            self.last_log[symbol] = mono()
            self.store.append_jsonl("decisions.jsonl", {
                "t": iso_now(), "symbol": symbol,
                "price": round(d.price, 2) if d.price is not None else None,
                "indicators": d.indicators, "signal": d.signal, "reason": d.reason,
                "checks": d.checks, "action": action, "detail": detail,
            })
        log.info(f"[{self.id}] {symbol}: {d.signal} | {d.reason}" + (f" -> {action}" if action != "none" else ""))

    def tick(self, clk):
        """One pass while the market is open."""
        today = clk.now_et.date().isoformat()
        if self.saved.get("day") != today:
            self.saved["day"] = today
            self.saved["attempts"] = []

        account = self.trading.get_account()
        equity = float(account.equity)
        positions = {p.symbol: p for p in self.trading.get_all_positions()}
        open_symbols = {o.symbol for o in self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100))}
        self.track_trades(positions, open_symbols)

        last_equity = float(account.last_equity)
        if (not self.halted_today and last_equity
                and (last_equity - equity) / last_equity >= MAX_DAILY_LOSS_PCT):
            self.halted_day = today
            notify(f"[{self.s.name}] Daily loss limit hit: no new trades for the rest of today.")
        halted = self.halted_today

        mtc = clk.minutes_to_close
        fm = self.s.flatten_minutes_before_close
        no_entries = fm is not None and mtc is not None and mtc <= fm
        if no_entries and self.saved.get("flattened_day") != today:
            if self.flatten_all(positions, clk):
                positions = {}

        failures, last_error = 0, None
        for symbol in self.symbols:
            try:
                self.check_symbol(symbol, clk, equity, positions, open_symbols, halted, no_entries)
            except Exception as e:
                failures += 1
                last_error = e
                log.exception(f"[{self.id}] {symbol}: check failed")
                self.store.append_jsonl("decisions.jsonl", {
                    "t": iso_now(), "symbol": symbol, "price": None, "indicators": {},
                    "signal": "error", "reason": f"The check failed: {str(e)[:200]}",
                    "checks": [], "action": "none", "detail": None,
                })
        self.save_state()
        if failures == len(self.symbols):
            # Count this as a failed pass so repeated outages stop this strategy and alert you.
            raise RuntimeError(f"Every symbol check failed. Last error: {last_error}")

        if halted:
            self.set_state("halted", f"Daily loss limit of {MAX_DAILY_LOSS_PCT * 100:.0f}% reached. No new trades today.")
        else:
            self.set_state("running", "Checking the market.")

    # ------------------------------------------------------------ dashboard

    def descriptor(self):
        d = self.s.describe(self.params)
        d["symbols"] = self.symbols
        d["configured"] = self.configured
        d["settings"] = [
            {"label": "Max daily loss", "display": f"{MAX_DAILY_LOSS_PCT * 100:.1f}%",
             "hint": "New trades stop for the day if the account falls this far"},
            {"label": "Check interval", "display": f"{LOOP_SECONDS} sec",
             "hint": "How often the bot looks at the market"},
        ] + self.s.settings(self.params, len(self.symbols))
        return d

    @staticmethod
    def _position_dict(p):
        return {
            "symbol": _g(p, "symbol"),
            "side": str(_val(_g(p, "side"))),
            "qty": abs(_f(_g(p, "qty"), 0.0)),
            "avg_entry_price": _f(_g(p, "avg_entry_price")),
            "current_price": _f(_g(p, "current_price")),
            "market_value": _f(_g(p, "market_value")),
            "unrealized_pl": _f(_g(p, "unrealized_pl")),
            "unrealized_plpc": _f(_g(p, "unrealized_plpc")),
        }

    @staticmethod
    def _order_dict(o):
        otype = _g(o, "order_type") or _g(o, "type")
        return {
            "id": str(_g(o, "id")),
            "symbol": _g(o, "symbol"),
            "side": str(_val(_g(o, "side"))),
            "qty": _f(_g(o, "qty")),
            "filled_qty": _f(_g(o, "filled_qty")),
            "type": str(_val(otype)) if otype is not None else None,
            "order_class": str(_val(_g(o, "order_class"))) if _g(o, "order_class") is not None else None,
            "status": str(_val(_g(o, "status"))),
            "limit_price": _f(_g(o, "limit_price")),
            "stop_price": _f(_g(o, "stop_price")),
            "filled_avg_price": _f(_g(o, "filled_avg_price")),
            "submitted_at": _iso(_g(o, "submitted_at")),
            "filled_at": _iso(_g(o, "filled_at")),
        }

    def build_status(self):
        base = {
            "schema": 2, "updated_at": utc_now_iso(),
            "strategy": self.descriptor(),
            "bot": {**self.bot, "run_url": run_url(), "version": VERSION},
            "market": self.market,
        }
        if not self.configured:
            suffix = self.s.key_suffix
            base["bot"] = {**base["bot"], "state": "not_configured",
                           "message": f"No API keys yet. Add the secrets APCA_API_KEY_ID{suffix} and "
                                      f"APCA_API_SECRET_KEY{suffix} in the repo settings to switch this strategy on."}
            base.update({"account": {}, "positions": [], "open_orders": [], "recent_orders": [], "tuning": None})
            return base

        t = self.trading
        acct = _safe(t.get_account, None)
        equity = _f(_g(acct, "equity")) if acct else None
        last_equity = _f(_g(acct, "last_equity")) if acct else None
        day_pnl = (equity - last_equity) if equity is not None and last_equity else None
        day_pnl_pct = (day_pnl / last_equity * 100) if day_pnl is not None and last_equity else None
        meta = self.store.meta(equity)

        base.update({
            "account": {
                "equity": equity, "last_equity": last_equity,
                "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct,
                "cash": _f(_g(acct, "cash")) if acct else None,
                "buying_power": _f(_g(acct, "buying_power")) if acct else None,
                "status": str(_val(_g(acct, "status"))) if acct else None,
                "starting_equity": meta.get("starting_equity"),
                "tracking_since": meta.get("tracking_since"),
            },
            "positions": _safe(lambda: [self._position_dict(p) for p in t.get_all_positions()], []),
            "open_orders": _safe(lambda: [self._order_dict(o) for o in t.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))], []),
            "recent_orders": _safe(lambda: [self._order_dict(o) for o in t.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.ALL, limit=60))], []),
            "tuning": ({"last_tuned": self.params.get("last_tuned"),
                        "backtest_return_pct": self.params.get("backtest_return_pct")}
                       if self.id == "rsi-reversion" else None),
        })
        return base

    def write_status(self, final=False):
        """Write the latest snapshot and an account-value point. Never raises."""
        try:
            status = self.build_status()
            self.store.write_json("status.json", status)
            equity = (status.get("account") or {}).get("equity")
            if equity is not None:
                first_point = self.store.equity_rows() == 0
                running = self.bot["state"] in ("running", "halted")
                closing = final and self.bot["passes"] > 0
                if first_point or running or closing:
                    self.store.append_equity(status["updated_at"], equity)
        except Exception:
            log.warning(f"[{self.id}] Dashboard update failed (trading is not affected).", exc_info=True)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

LAST_PUSH = {"t": None}


def publish_all(pub, runners, final=False):
    """Every few minutes (and at the end), write every strategy's snapshot and push it. Never raises."""
    try:
        due = final or LAST_PUSH["t"] is None or mono() - LAST_PUSH["t"] >= PUBLISH_EVERY_SECONDS
        if not due:
            return
        for r in runners:
            r.write_status(final=final)
        if pub.push():
            LAST_PUSH["t"] = mono()
    except Exception:
        log.warning("Dashboard update failed (trading is not affected).", exc_info=True)


def write_index(pub, runners):
    try:
        entries = []
        for r in runners:
            d = r.s.describe(r.params)
            entries.append({"id": r.id, "name": r.s.name, "description": r.s.description,
                            "kind": r.s.kind, "timeframe": d.get("timeframe"),
                            "configured": r.configured, "symbols": r.symbols})
        pub.write_index(entries)
    except Exception:
        log.warning("Could not write the strategy index.", exc_info=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop() -> None:
    pub = Publisher()
    runners = [Runner(cls(), pub, WATCHLIST) for cls in ALL_STRATEGIES]
    active = [r for r in runners if r.configured]
    log.info(f"Bot {VERSION} started. Strategies: " + ", ".join(
        f"{r.id} ({'on' if r.configured else 'no keys'})" for r in runners))
    write_index(pub, runners)

    if not active:
        for r in runners:
            r.set_state("not_configured", "No API keys.")
        publish_all(pub, runners, final=True)
        log.error("None of the strategies has API keys. Add the repo secrets and run again.")
        sys.exit(1)

    clock_client = active[0].trading
    deadline = now_utc() + timedelta(minutes=MAX_RUNTIME_MINUTES)
    errors = 0
    final_state, final_message = "stopped", "Stopped."

    try:
        while now_utc() < deadline:
            pass_started = mono()
            try:
                clock = clock_client.get_clock()
                market = {
                    "is_open": bool(clock.is_open),
                    "next_open": _iso(clock.next_open),
                    "next_close": _iso(clock.next_close),
                }
                for r in runners:
                    r.market = market

                if not clock.is_open:
                    wait = (clock.next_open - clock.timestamp).total_seconds()
                    if wait > MAX_WAIT_FOR_OPEN_MINUTES * 60:
                        final_message = "The market is closed. The next scheduled run starts before the open."
                        log.info("Market is closed and won't open soon. Exiting.")
                        return
                    for r in active:
                        if not r.stopped:
                            r.set_state("waiting", f"Waiting for the market to open in about {int(wait // 60)} min.")
                    log.info(f"Waiting for the market to open in about {int(wait // 60)} min.")
                    publish_all(pub, runners)
                    sleep(min(wait + 5, LOOP_SECONDS))
                    continue

                ts = _aware(clock.timestamp)
                clk = SimpleNamespace(
                    now_utc=now_utc(), now_et=now_utc().astimezone(ET),
                    minutes_to_close=max(0.0, (_aware(clock.next_close) - ts).total_seconds() / 60),
                )
                for r in active:
                    if r.stopped:
                        continue
                    try:
                        r.tick(clk)
                        r.errors = 0
                        r.bot["errors_in_row"] = 0
                        r.bot["passes"] += 1
                        r.bot["last_pass_at"] = iso_now()
                    except Exception as e:
                        r.errors += 1
                        r.bot["errors_in_row"] = r.errors
                        r.bot["last_error"] = str(e)[:300]
                        r.bot["last_error_at"] = iso_now()
                        r.set_state("error", f"A check failed ({r.errors} of {MAX_CONSECUTIVE_ERRORS} before this strategy stops).")
                        log.exception(f"[{r.id}] Pass failed ({r.errors}/{MAX_CONSECUTIVE_ERRORS})")
                        if r.errors >= MAX_CONSECUTIVE_ERRORS:
                            r.stopped = True
                            r.final_message = "Stopped after repeated errors."
                            notify(f"[{r.s.name}] stopping after repeated errors: {e}")

                errors = 0
                publish_all(pub, runners)
                if all(r.stopped for r in active):
                    final_message = "Every strategy stopped after repeated errors."
                    sys.exit(1)

            except Exception as e:
                errors += 1
                for r in active:
                    r.set_state("error", f"A check failed ({errors} of {MAX_CONSECUTIVE_ERRORS} before the bot stops).")
                    r.bot["last_error"] = str(e)[:300]
                    r.bot["last_error_at"] = iso_now()
                    r.bot["errors_in_row"] = errors
                log.exception(f"Pass failed ({errors}/{MAX_CONSECUTIVE_ERRORS})")
                publish_all(pub, runners)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    final_message = "Stopped after repeated errors."
                    notify(f"Trading bot stopping after repeated errors: {e}")
                    sys.exit(1)

            sleep(max(5, LOOP_SECONDS - (mono() - pass_started)))

        final_message = "Reached the maximum run time. The next scheduled run takes over."
        log.info(final_message)
    finally:
        for r in active:
            if r.stopped:
                r.set_state("error", r.final_message or "Stopped.")
            elif r.halted_today:
                r.set_state("halted", f"Daily loss limit of {MAX_DAILY_LOSS_PCT * 100:.0f}% reached. No new trades today.")
            else:
                r.set_state(final_state, final_message)
            r.save_state()
        publish_all(pub, runners, final=True)


if __name__ == "__main__":
    main_loop()
