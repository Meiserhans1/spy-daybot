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

Errors: a refused order or a failed API call never stops the run. The bot logs
what Alpaca said, posts it as an annotation on the GitHub run page, keeps going,
and still closes at 15:59. If anything went wrong it exits with code 1 at the end,
so GitHub still sends the failure email.
"""
import argparse
import datetime as dt
import json
import math
import os
import re
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
PDT_EQUITY = 25_000  # under this (at yesterday's close) the pattern day trader rule applies
PDT_MAX_DAY_TRADES = 3  # day trades allowed in 5 business days while under PDT_EQUITY

PROBLEMS = []  # everything that went wrong this run; non-empty -> exit code 1 at the end


def log(msg):
    print(f"[{dt.datetime.now(ET):%Y-%m-%d %H:%M:%S} ET] {msg}", flush=True)


def _annotate(level, msg):
    """Show msg on the GitHub run page (visible without signing in)."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = str(msg).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} title=Day bot::{safe}", flush=True)


def notice(msg):
    log(msg)
    _annotate("notice", msg)


def warn(msg):
    log(f"WARNING: {msg}")
    _annotate("warning", msg)


def problem(msg):
    log(f"PROBLEM: {msg}")
    _annotate("error", msg)
    PROBLEMS.append(msg)


def describe(e):
    """Readable error text without dollar amounts (Alpaca error bodies can include balances)."""
    raw = str(e)
    try:
        body = json.loads(raw)
        return f"Alpaca said '{body.get('message', '?')}' (code {body.get('code', '?')})"
    except Exception:  # noqa: BLE001
        pass
    if raw.lstrip().startswith("{"):
        raw = re.sub(r"\d[\d,]*\.?\d*", "#", raw)
    return f"{type(e).__name__}: {raw[:300]}"


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
        self.last_refusal = ""  # what Alpaca said the last time it refused an order

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
            time.sleep(5)
        problem(f"{SYMBOL} position still open after 3 close attempts ({why}) - check the Alpaca dashboard")

    def cancel_own_orders(self):
        """Cancel only open orders this bot placed (client_order_id starts with 'daybot-')."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])):
                if str(o.client_order_id or "").startswith(ORDER_TAG):
                    self.trading.cancel_order_by_id(o.id)
        except Exception as e:  # noqa: BLE001
            log(f"cancel own orders: {describe(e)}")

    def market(self, side, qty):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if DRY_RUN:
            self.sim_qty += qty if side == "buy" else -qty
            log(f"[dry run] {side} {qty} {SYMBOL}")
            return True
        try:
            o = self.trading.submit_order(MarketOrderRequest(
                symbol=SYMBOL, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY, client_order_id=f"{ORDER_TAG}{uuid.uuid4().hex[:20]}"))
        except Exception as e:  # noqa: BLE001  (refused order: log it, never crash)
            self.last_refusal = describe(e)
            problem(f"{side} {qty} {SYMBOL} order refused: {self.last_refusal}")
            return False
        order_id = o.id
        for _ in range(30):
            time.sleep(2)
            try:
                o = self.trading.get_order_by_id(order_id)
            except Exception as e:  # noqa: BLE001  (a blip while polling; keep polling)
                log(f"order status check failed ({describe(e)}); retrying")
                continue
            s = str(o.status).lower()
            if "filled" in s and "partially" not in s:
                log(f"{side} {qty} {SYMBOL} filled at {o.filled_avg_price}")
                return True
            if any(x in s for x in ("rejected", "canceled", "expired")):
                problem(f"{side} {qty} {SYMBOL} order ended as {o.status}")
                return False
        problem(f"{side} {qty} {SYMBOL} order not filled after 60s (status {o.status}); cancelling it")
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception as e:  # noqa: BLE001
            log(f"cancel failed ({describe(e)})")
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
    for attempt in range(3):
        try:
            cal = b.calendar(today - dt.timedelta(days=45), today)
            break
        except Exception as e:  # noqa: BLE001
            log(f"calendar request failed ({describe(e)}); retrying")
            time.sleep(20)
    else:
        problem("could not load the market calendar; no trades this run")
        if until >= at(today, 15, 59):
            end_of_day(b, at(today, 15, 59), None)
        return
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

    close_t = at(today, 15, 59)
    try:
        acct = b.account()
        if acct.trading_blocked or acct.account_blocked:
            problem("account is blocked from trading; no trades this run")
            return
        last_equity = float(acct.last_equity)
        size_base = min(last_equity, CAPITAL) if CAPITAL > 0 else last_equity
        sig, lev, prev_close = load_setup(b, today, cal)
    except Exception as e:  # noqa: BLE001
        problem(f"setup failed, no trades this run: {describe(e)}")
        if until >= close_t:
            end_of_day(b, close_t, None)
        return

    # Pattern day trader rule: under $25k (at yesterday's close) an account gets 3 day
    # trades per 5 business days, and Alpaca refuses the order that would be a 4th.
    # Every trade this bot makes is a day trade (it is always flat by 15:59), so once the
    # limit is reached it must not open anything it could not close the same day.
    under_pdt = last_equity < PDT_EQUITY
    start_day_trades = int(acct.daytrade_count or 0)
    notice(f"{mode} | leverage today {lev:.2f}x | prev close {prev_close:.2f} | "
           f"PDT rule applies: {'yes (under $25k)' if under_pdt else 'no'} | "
           f"day trades in last 5 days: {start_day_trades} | PDT flagged: {'yes' if acct.pattern_day_trader else 'no'} | "
           f"shorting: {'on' if acct.shorting_enabled else 'off'} | multiplier {acct.multiplier}"
           + (" | capital cap on" if CAPITAL > 0 else ""))

    # anything left from a previous day gets closed at the open (not a day trade)
    if now_et() < at(today, 10, 0) and b.qty() != 0:
        sleep_until(at(today, 9, 30, 30))
        b.flatten("left over from a previous day")

    state = {
        "stopped": False,
        "bot_day_trades": 0,
        # a position found after 10:00 was most likely opened today by the morning run
        "opened_today": b.qty() != 0,
        "failed_entries": 0,
        "no_more_entries": "",  # reason, once entries are switched off for the rest of the run
    }

    def day_trades_used(a):
        return max(int(a.daytrade_count or 0), start_day_trades + state["bot_day_trades"])

    def pdt_blocks_entry(a):
        if not under_pdt:
            return False
        return bool(a.pattern_day_trader) or day_trades_used(a) >= PDT_MAX_DAY_TRADES

    def close_position(why):
        b.flatten(why)
        if b.qty() == 0 and state["opened_today"]:
            state["bot_day_trades"] += 1
            state["opened_today"] = False

    def check(t, off, today_open, vwap, px):
        ub, lb = st.bands(today_open, prev_close, sig[off])
        q = b.qty()
        pos = (q > 0) - (q < 0)
        new = st.decide(pos, px, ub, lb, vwap)
        log(f"{t:%H:%M} check | price {px:.2f} | upper {ub:.2f} | lower {lb:.2f} | vwap {vwap:.2f} | "
            f"position {pos:+d} -> {new:+d}")

        equity = float(b.account().equity) if not DRY_RUN else last_equity
        if equity < last_equity * (1 - LOSS_LIMIT):
            close_position(f"daily loss limit {LOSS_LIMIT:.0%} hit")
            state["stopped"] = True
            return

        if new == pos:
            return
        if pos != 0:
            close_position("trailing stop / reversal")
        if new == 0:
            return

        side = "buy" if new == 1 else "sell"
        a = b.account()
        if new == -1 and not DRY_RUN and not a.shorting_enabled:
            log("shorting is not enabled on this account; skipping short")
            return
        if state["no_more_entries"]:
            log(f"{t:%H:%M} entry skipped ({state['no_more_entries']})")
            return
        if pdt_blocks_entry(a):
            warn(f"{t:%H:%M} skipped the {'long' if new == 1 else 'short'} entry: account is under $25k and has "
                 f"used {day_trades_used(a)} of {PDT_MAX_DAY_TRADES} day trades in the last 5 days, "
                 f"so Alpaca would refuse the same-day close")
            return
        shares = math.floor(size_base * lev / today_open)
        if not DRY_RUN:
            # buying_power is already the right number for this account type (cash 1x, margin 2x, PDT 4x)
            shares = min(shares, math.floor(0.95 * float(a.buying_power or 0) / px))
        if shares < 1:
            log("position size rounds to 0 shares; skipping")
            return
        b.last_refusal = ""
        ok = b.market(side, shares)
        if not ok and not DRY_RUN and b.qty() == 0:
            if "pattern day" in b.last_refusal.lower():
                state["no_more_entries"] = "Alpaca refused an entry under the pattern day trader rule earlier today"
            else:
                a = b.account()
                smaller = min(math.floor(0.95 * float(a.regt_buying_power or 0) / px), shares // 2)
                if smaller >= 1:
                    log(f"retrying with {smaller} shares")
                    b.market(side, smaller)
        if b.qty() != 0:
            state["opened_today"] = True
        else:
            state["failed_entries"] += 1
            if state["failed_entries"] >= 3 and not state["no_more_entries"]:
                state["no_more_entries"] = "3 entries failed today; not trying again until tomorrow"

    for off in st.CHECK_OFFSETS:
        t = at(today, 9, 30) + dt.timedelta(minutes=off)
        if t > until:
            break
        if t < start_from or now_et() > t + GRACE:
            continue  # this check already passed before the run started
        sleep_until(t + dt.timedelta(seconds=2))
        if state["stopped"]:
            break
        try:
            today_open, vwap = today_state(b, today, off)
            px = b.last_price()
        except Exception as e:  # noqa: BLE001
            warn(f"{t:%H:%M} check skipped, no price data: {describe(e)}")
            continue
        try:
            check(t, off, today_open, vwap, px)
        except Exception as e:  # noqa: BLE001  (one bad check never ends the day)
            problem(f"{t:%H:%M} check failed: {describe(e)}")

    if until >= close_t:
        end_of_day(b, close_t, last_equity)
    log("run finished")


def end_of_day(b, close_t, last_equity):
    """Flat by 15:59 no matter what happened earlier in the day."""
    sleep_until(close_t)
    for attempt in range(3):
        try:
            b.flatten("end of day")
            break
        except Exception as e:  # noqa: BLE001
            log(f"end-of-day close attempt {attempt + 1} failed ({describe(e)})")
            time.sleep(10)
    else:
        problem(f"could not close {SYMBOL} at the end of the day - check the Alpaca dashboard")
    if DRY_RUN or not last_equity:
        return
    try:
        a = b.account()
        log(f"day done | account {float(a.equity) / last_equity - 1:+.2%} today")
    except Exception as e:  # noqa: BLE001
        log(f"end-of-day account check failed ({describe(e)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="00:00", help="skip checks before this time (ET, HH:MM)")
    ap.add_argument("--until", default="16:05", help="last time (ET, HH:MM) this run should cover")
    a = ap.parse_args()
    d = now_et().date()
    try:
        run(at(d, *map(int, a.start.split(":"))), at(d, *map(int, a.until.split(":"))))
    except Exception as e:  # noqa: BLE001  (last resort: report it clearly, no dollar amounts)
        import traceback

        traceback.print_tb(e.__traceback__)
        problem(f"bot crashed: {describe(e)}")
    if PROBLEMS:
        log(f"{len(PROBLEMS)} problem(s) this run - details above and on the run page")
        sys.exit(1)
