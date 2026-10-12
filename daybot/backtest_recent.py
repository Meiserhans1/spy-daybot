#!/usr/bin/env python3
"""
Backtest the day bot's rules on real SPY minute data from Alpaca (2016 -> yesterday).

Runs in GitHub Actions (workflow "Day bot backtest") with the DAYBOT_KEY /
DAYBOT_SECRET secrets, or locally:  python daybot/backtest_recent.py --start 2016-01-01

Results go to stdout and, inside GitHub Actions, to the run's summary page.
"""
import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy as st  # noqa: E402

ET = ZoneInfo("America/New_York")


def simulate(dates, opens, closes, vwaps, cost_bps=0.2, max_leverage=st.MAX_LEVERAGE, prev_closes=None):
    """
    dates: list of days; opens (n,) 9:30 opens; closes / vwaps (n, 390).
    prev_closes (n,) optional: prior session close (defaults to previous row's last close).
    Returns daily strategy returns (fraction of equity) and number of round trips.
    """
    n = len(dates)
    cost = cost_bps / 1e4
    rets = np.zeros(n)
    trips = 0
    for d in range(st.LOOKBACK + 1, n):
        sig = st.sigma_for_checks(opens[d - st.LOOKBACK:d], closes[d - st.LOOKBACK:d])
        lev = st.leverage(closes[d - st.LOOKBACK - 1:d, -1], max_leverage)
        if lev == 0:
            continue
        prev_close = closes[d - 1, -1] if prev_closes is None else prev_closes[d]
        pos, entry, r = 0, 0.0, 0.0
        for off in st.CHECK_OFFSETS:
            i = st.bar_index(off)
            px = closes[d, i]
            ub, lb = st.bands(opens[d], prev_close, sig[off])
            new = st.decide(pos, px, ub, lb, vwaps[d, i])
            if new != pos:
                if pos != 0:
                    r += (px / entry - 1) if pos == 1 else (1 - px / entry)
                    r -= cost
                    trips += 1
                if new != 0:
                    entry = px
                    r -= cost
                pos = new
        if pos != 0:
            px = closes[d, -1]
            r += (px / entry - 1) if pos == 1 else (1 - px / entry)
            r -= cost
            trips += 1
        rets[d] = lev * r
    return rets, trips


def report(dates, rets, trips, bench=None):
    r = pd.Series(rets, index=pd.DatetimeIndex(dates))
    r = r.iloc[st.LOOKBACK + 1:]
    eq = (1 + r).cumprod()
    yrs = max((r.index[-1] - r.index[0]).days / 365.25, 1e-9)
    out = {
        "period": f"{r.index[0].date()} to {r.index[-1].date()}",
        "annual return": f"{eq.iloc[-1] ** (1 / yrs) - 1:+.1%}",
        "annual volatility": f"{r.std() * np.sqrt(252):.1%}",
        "sharpe": f"{r.mean() / r.std() * np.sqrt(252):.2f}",
        "max drawdown": f"{(eq / eq.cummax() - 1).min():.1%}",
        "round trips": trips,
        "days with a trade": f"{(r != 0).mean():.0%}",
    }
    yearly = ((1 + r).groupby(r.index.year).prod() - 1).rename("bot")
    if bench is not None:
        b = pd.Series(bench, index=pd.DatetimeIndex(dates)).iloc[st.LOOKBACK + 1:]
        yearly = pd.concat([yearly, ((1 + b).groupby(b.index.year).prod() - 1).rename("SPY buy & hold")], axis=1)
    return out, yearly


def load_alpaca(symbol, start, end, feed):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    key, secret = os.environ["DAYBOT_KEY"], os.environ["DAYBOT_SECRET"]
    paper = os.getenv("DAYBOT_PAPER", "true").lower() != "false"
    data = StockHistoricalDataClient(key, secret)
    trading = TradingClient(key, secret, paper=paper)
    cal = trading.get_calendar(GetCalendarRequest(start=start, end=end))
    full = {c.date for c in cal if c.close.hour == 16 and c.close.minute == 0}
    all_days = [c.date for c in cal]

    frames = []
    cur = start
    while cur <= end:
        nxt = min(end, (pd.Timestamp(cur) + pd.offsets.MonthEnd(0)).date())
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=dt.datetime.combine(cur, dt.time(9, 0), ET),
            end=dt.datetime.combine(nxt, dt.time(16, 30), ET),
            adjustment=Adjustment.SPLIT, feed=DataFeed(feed))
        df = data.get_stock_bars(req).df
        if len(df):
            frames.append(df.reset_index())
        print(f"  fetched {cur:%Y-%m} ({len(df)} bars)", flush=True)
        cur = nxt + dt.timedelta(days=1)
    df = pd.concat(frames, ignore_index=True)
    t = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    df["date"] = t.dt.date
    df["m"] = t.dt.hour * 60 + t.dt.minute - (9 * 60 + 30)
    df = df[(df.m >= 0) & (df.m < st.MINUTES_PER_DAY)]

    dates, opens, closes, vwaps, last_close = [], [], [], [], {}
    for d, g in df.groupby("date"):
        last_close[d] = g.sort_values("m").close.iloc[-1]
        if d not in full or g.m.min() > 5 or len(g) < 300:
            continue
        c = np.full(st.MINUTES_PER_DAY, np.nan)
        c[g.m.values] = g.close.values
        c = pd.Series(c).ffill().bfill().values
        pv = np.zeros(st.MINUTES_PER_DAY)
        vol = np.zeros(st.MINUTES_PER_DAY)
        pv[g.m.values] = g.vwap.values * g.volume.values
        vol[g.m.values] = g.volume.values
        cv = np.cumsum(vol)
        vw = np.where(cv > 0, np.cumsum(pv) / np.where(cv > 0, cv, 1), c)
        dates.append(d)
        opens.append(g.sort_values("m").open.iloc[0])
        closes.append(c)
        vwaps.append(vw)
    # previous session close (the real prior trading day, even if it was a half day)
    prev = []
    for d in dates:
        i = all_days.index(d)
        pd_ = all_days[i - 1] if i > 0 else None
        prev.append(last_close.get(pd_, np.nan))
    prev = np.array(prev)
    closes = np.array(closes)
    prev = np.where(np.isnan(prev), np.r_[np.nan, closes[:-1, -1]], prev)
    return dates, np.array(opens), closes, np.array(vwaps), prev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=os.getenv("DAYBOT_SYMBOL", "SPY"))
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--feed", default="sip")
    ap.add_argument("--cost_bps", type=float, default=0.2, help="cost per side in basis points")
    ap.add_argument("--max_leverage", type=float, default=float(os.getenv("DAYBOT_MAX_LEVERAGE", st.MAX_LEVERAGE)))
    a = ap.parse_args()
    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end) if a.end else (dt.datetime.now(ET).date() - dt.timedelta(days=1))
    print(f"Loading {a.symbol} minute bars {start} -> {end} ...", flush=True)
    dates, opens, closes, vwaps, prev = load_alpaca(a.symbol, start, end, a.feed)
    rets, trips = simulate(dates, opens, closes, vwaps, a.cost_bps, a.max_leverage, prev_closes=prev)
    bench = np.r_[0, closes[1:, -1] / closes[:-1, -1] - 1]
    out, yearly = report(dates, rets, trips, bench)
    lines = [f"## Day bot backtest: {a.symbol}, cost {a.cost_bps} bp/side, max leverage {a.max_leverage}x", ""]
    lines += [f"- **{k}**: {v}" for k, v in out.items()]
    lines += ["", "| year | bot | SPY buy & hold |", "|---|---|---|"]
    lines += [f"| {y} | {row['bot']:+.1%} | {row['SPY buy & hold']:+.1%} |" for y, row in yearly.iterrows()]
    text = "\n".join(lines)
    print(text)
    summ = os.getenv("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
