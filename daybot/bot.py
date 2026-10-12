#!/usr/bin/env python3
"""
Live SPY day bot: noise-area intraday momentum (rules in strategy.py).

One run covers part or all of a trading day. It acts at every 30-minute check
from now until --until (ET), and if --until is 15:59 or later it closes the
position at 15:59 so nothing is held overnight. GitHub Actions runs it twice a
day (morning, then afternoon) because one job can't run longer than 6 hours.

Environment
  DAYBOT_KEY / DAYBOT_SECRET  Alpaca keys for the day bot's OWN account (required)
  DAYBOT_PAPER                "true" (default) = paper trading, "false" = real money
  DAYBOT_SYMBOL               default SPY
  DAYBOT_MAX_LEVERAGE         default 4 (the paper's cap); lower = smaller positions
  DAYBOT_FEED                 data feed for today's prices: iex (free, default) or sip (paid)
  DAYBOT_DAILY_LOSS_LIMIT     stop for the day if equity drops this far below yesterday's close (default 0.05)
  DAYBOT_DRY_RUN              "true" = log decisions, send no orders
  DAYBOT_CAPITAL              optional: size from at most this many dollars (for a shared account)

Independence from other bots: it only trades DAYBOT_SYMBOL, only cancels orders it
tagged itself, and closes positions with orders for its own share count. Logs show
percentages, not dollar balances, so they are safe in a public repo.
"""
import argparse
import datetime as dt
import math
import os
import sys
import time
import uuid
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy as st  # noqa: E402

ET = ZoneInfo("America/New_York")
SYMBOL = os.getenv("DAYBOT_SYMBOL", "SPY")
PAPER = os.getenv("DAYBOT_PAPER", "true").strip().lower() != "false"
MAX_LEV = float(os.getenv("DAYBOT_MAX_LEVERAGE", st.MAX_LEVERAGE))
LIVE_FEED = os.getenv("DAYBOT_FEED", "iex").strip().lower()
LOSS_LIMIT = float(os.getenv("DAYBOT_DAILY_LOSS_LIMIT", "0.05"))
DRY_RUN = os.getenv("DAYBOT_DRY_RUN", "false").strip().lower() == "true"
CAPITAL = float(os.getenv("DAYBOT_CAPITAL", "0") or 0)  # optional cap on the money the bot sizes from
ORDER_TAG = "daybot-"  # every order this bot sends is tagged, so it only ever cancels its own
GRACE = dt.timedelta(minutes=3)  # a check started up to 3 min late still runs


def log(msg):
    print(f"[{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S} ET] {msg}", flush=True)


def now_et():
    return dt.datetime.now(ET)


def at(day, hh, mm, ss=0):
    return dt.datetime.combine(day, dt.time(hh, mm, ss), ET)


def sleep_until(t):
    while True:
        s = (t - now_et()).total_seconds()
        if s <= 0:
            return
        time.sleep(min(s, 30))


class Broker:
    """Thin wrapper around Alpaca so the strategy loop stays readable."""

    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key, secret = os.environ["DAYBOT_KEY"], os.environ["DAYBOT_SECRET"]
        self.trading = TradingClient(key, secret, paper=PAPER)
        self.data = StockHistoricalDataClient(key, secret)
        self.sim_qty = 0  # used only in dry-run mode

    # ---- market data -------------------------------------------------
    def bars(self, start, end, feed):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Minute, start=start, end=end,
                               adjustment=Adjustment.SPLIT, feed=DataFeed(feed))
        df = self.data.get_stock_bars(req).df
        if df.empty:
            return df
        df = df.reset_index()
        t = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
        df["date"] = t.dt.date
        df["m"] = t.dt.hour * 60 + t.dt.minute - (9 * 60 + 30)
        return df[(df.m >= 0) & (df.m < st.MINUTES_PER_DAY)].sort_values(["date", "m"])

    def history_bars(self, start, end):
        for feed in ("sip", "iex"):  # free plans can read SIP data older than 15 minutes
            try:
                return self.bars(start, end, feed)
            except Exception as e:  # noqa: BLE001
                log(f"history via {feed} failed ({e}); trying next feed")
        raise RuntimeError("could not load price history")

    def last_price(self):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        tr = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=SYMBOL, feed=DataFeed(LIVE_FEED)))
        return float(tr[SYMBOL].price)

    # ---- account / orders --------------------------------------------
    def account(self):
        return self.trading.get_account()

    def calendar(self, start, end):
        from alpaca.trading.requests import GetCalendarRequest

        return self.trading.get_calendar(GetCalendarRequest(start=start, end=end))

    def qty(self):
        if DRY_RUN:
            return self.sim_qty
        try:
            p = self.trading.get_open_position(SYMBOL)
        except Exception:  # noqa: BLE001  (no position -> API error)
            return 0
        q = abs(float(p.qty))
        return -q if "short" in str(p.side).lower() else q

    def flatten(self, why):
        q = self.qty()
        if q == 0:
            return
        log(f"closing {q:+g} {SYMBOL} ({why})")
        if DRY_RUN:
            self.sim_qty = 0
            return
        self.cancel_own_orders()
        for attempt in range(3):
            q = self.qty()
            if q == 0:
                return
            # close with an order for exactly our shares (never touches other symbols or other bots' orders)
            self.market("sell" if q > 0 else "buy", int(abs(q)))
            if self.qty() == 0:
                return
            log(f"close attempt {attempt + 1} did not flatten; retrying")
        log("WARNING: position still open after 3 close attempts")

    def cancel_own_orders(self):
        """Cancel only open orders this bot placed (client_order_id starts with 'daybot-')."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])):
                if str(o.client_order_id or "").startswith(ORDER_TAG):
                    self.trading.cancel_order_by_id(o.id)
        except Exception as e:  # noqa: BLE001
            log(f"cancel own orders: {e}")

    def market(self, side, qty):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if DRY_RUN:
            self.sim_qty += qty if side == "buy" else -qty
            log(f"[dry run] {side} {qty} {SYMBOL}")
            return True
        o = self.trading.submit_order(MarketOrderRequest(
            symbol=SYMBOL, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, client_order_id=f"{ORDER_TAG}{uuid.uuid4().hex[:20]}"))
        for _ in range(30):
            time.sleep(2)
            o = self.trading.get_order_by_id(o.id)
            s = str(o.status).lower()
            if "filled" in s and "partially" not in s:
                log(f"{side} {qty} {SYMBOL} filled at {o.filled_avg_price}")
                return True
            if any(x in s for x in ("rejected", "canceled", "expired")):
                log(f"order {side} {qty} ended as {o.status}")
                return False
        log(f"order {side} {qty} not filled after 60s (status {o.status})")
        return False


def load_setup(b, today, cal):
    """Sigma per check, leverage and yesterday's close from the last full trading days."""
    past = [c for c in cal if c.date < today]
    full = [c for c in past if c.close.hour == 16 and c.close.minute == 0][-(st.LOOKBACK + 1):]
    if len(full) < st.LOOKBACK + 1:
        raise RuntimeError("not enough trading-day history")
    prev_day = past[-1].date
    df = b.history_bars(at(full[0].date, 9, 30), at(prev_day, 16, 1))
    full_dates = {c.date for c in full}
    opens, closes = [], []
    for d, g in df.groupby("date"):
        if d not in full_dates:
            continue
        c = np.full(st.MINUTES_PER_DAY, np.nan)
        c[g.m.values] = g.close.values
        closes.append(pd.Series(c).ffill().bfill().values)
        opens.append(float(g.open.iloc[0]))
    if len(closes) < st.LOOKBACK + 1:
        raise RuntimeError(f"history has only {len(closes)} usable days")
    closes = np.array(closes)
    sig = st.sigma_for_checks(opens[-st.LOOKBACK:], closes[-st.LOOKBACK:])
    lev = st.leverage(closes[-(st.LOOKBACK + 1):, -1], MAX_LEV)
    prev = df[df.date == prev_day]
    prev_close = float(prev.close.iloc[-1]) if len(prev) else float(closes[-1, -1])
    return sig, lev, prev_close


def today_state(b, today, offset):
    """Today's 9:30 open and the VWAP of all bars that closed by the check time."""
    df = b.bars(at(today, 9, 30), now_et(), LIVE_FEED)
    df = df[df.date == today]
    if df.empty:
        raise RuntimeError("no bars for today yet")
    today_open = float(df.open.iloc[0])
    done = df[df.m < offset]
    if done.empty or done.volume.sum() == 0:
        vwap = float(df.close.iloc[-1])
    else:
        vwap = float((done.vwap * done.volume).sum() / done.volume.sum())
    return today_open, vwap


def run(start_from, until):
    b = Broker()
    today = now_et().date()
    cal = b.calendar(today - dt.timedelta(days=45), today)
    todays = [c for c in cal if c.date == today]
    mode = "PAPER" if PAPER else "LIVE MONEY"
    log(f"day bot start ({mode}{', dry run' if DRY_RUN else ''}), symbol {SYMBOL}, checks {start_from:%H:%M}-{until:%H:%M}")
    if not todays:
        log("market closed today; nothing to do")
        return
    if not (todays[0].close.hour == 16 and todays[0].close.minute == 0):
        log("half day: the bot sits out (backtest skips half days)")
        sleep_until(at(today, 9, 31))
        b.flatten("half day")
        return

    acct = b.account()
    if acct.trading_blocked or acct.account_blocked:
        log("account is blocked from trading; stopping")
        return
    last_equity = float(acct.last_equity)
    size_base = min(last_equity, CAPITAL) if CAPITAL > 0 else last_equity
    sig, lev, prev_close = load_setup(b, today, cal)
    log(f"leverage today {lev:.2f}x | prev close {prev_close:.2f}" + (" | capital cap on" if CAPITAL > 0 else ""))

    # anything left from a previous day gets closed at the open
    if now_et() < at(today, 10, 0) and b.qty() != 0:
        sleep_until(at(today, 9, 30, 30))
        b.flatten("left over from a previous day")

    stopped = False
    for off in st.CHECK_OFFSETS:
        t = at(today, 9, 30) + dt.timedelta(minutes=off)
        if t > until:
            break
        if t < start_from or now_et() > t + GRACE:
            continue  # this check already passed before the run started
        sleep_until(t + dt.timedelta(seconds=2))
        if stopped:
            break
        try:
            today_open, vwap = today_state(b, today, off)
            px = b.last_price()
        except Exception as e:  # noqa: BLE001
            log(f"{t:%H:%M} check skipped: {e}")
            continue
        ub, lb = st.bands(today_open, prev_close, sig[off])
        q = b.qty()
        pos = (q > 0) - (q < 0)
        new = st.decide(pos, px, ub, lb, vwap)
        log(f"{t:%H:%M} check | price {px:.2f} | upper {ub:.2f} | lower {lb:.2f} | vwap {vwap:.2f} | "
            f"position {pos:+d} -> {new:+d}")

        equity = float(b.account().equity) if not DRY_RUN else last_equity
        if equity < last_equity * (1 - LOSS_LIMIT):
            b.flatten(f"daily loss limit {LOSS_LIMIT:.0%} hit")
            stopped = True
            continue

        if new == pos:
            continue
        if pos != 0:
            b.flatten("trailing stop / reversal")
        if new != 0:
            shares = math.floor(size_base * lev / today_open)
            a = b.account()
            bp = max(float(a.buying_power or 0), float(a.daytrading_buying_power or 0))
            shares = min(shares, math.floor(0.95 * bp / px)) if not DRY_RUN else shares
            if new == -1 and not DRY_RUN and not a.shorting_enabled:
                log("shorting is not enabled on this account; skipping short")
                continue
            if shares < 1:
                log("position size rounds to 0 shares; skipping")
                continue
            ok = b.market("buy" if new == 1 else "sell", shares)
            if not ok and not DRY_RUN:
                a = b.account()
                smaller = math.floor(0.95 * float(a.regt_buying_power or 0) / px)
                if 1 <= smaller < shares:
                    log(f"retrying with {smaller} shares")
                    b.market("buy" if new == 1 else "sell", smaller)

    close_t = at(today, 15, 59)
    if until >= close_t:
        sleep_until(close_t)
        b.flatten("end of day")
        a = b.account() if not DRY_RUN else None
        if a is not None:
            log(f"day done | account {float(a.equity) / last_equity - 1:+.2%} today")
    log("run finished")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="00:00", help="skip checks before this time (ET, HH:MM)")
    ap.add_argument("--until", default="16:05", help="last time (ET, HH:MM) this run should cover")
    a = ap.parse_args()
    d = now_et().date()
    run(at(d, *map(int, a.start.split(":"))), at(d, *map(int, a.until.split(":"))))
