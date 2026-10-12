"""
Noise-area intraday momentum rules for SPY (Zarattini, Aziz & Barbon, 2024,
"Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF").

This file is shared by the live bot and the backtest so both use the exact
same rules.

Day layout: minute bars 9:30 ... 15:59 (390 bars). Bar i starts at 9:30 + i min.
The price "at" a check time HH:MM is the close of the bar that ends at HH:MM,
i.e. bar index (HH:MM - 9:30) - 1.

Rules
  1. Noise area. For each check time, sigma = average absolute move from the
     9:30 open to that time over the last 14 full trading days.
       upper = max(today's open, yesterday's close) * (1 + sigma)
       lower = min(today's open, yesterday's close) * (1 - sigma)
  2. Checks every 30 min, 10:00 through 15:30.
       flat  -> long  if price > max(upper, VWAP)
       flat  -> short if price < min(lower, VWAP)
       long  -> exit  if price < max(upper, VWAP)   (trailing stop)
       short -> exit  if price > min(lower, VWAP)
     An exit and an entry the other way can happen at the same check.
  3. Always flat at the close.
  4. Size: equity at yesterday's close * min(MAX_LEVERAGE, 2% / stdev of the
     last 14 daily returns) / today's open.
"""
import math

import numpy as np

LOOKBACK = 14
VOL_TARGET = 0.02
MAX_LEVERAGE = 4.0
MINUTES_PER_DAY = 390
CHECK_OFFSETS = [30 + 30 * k for k in range(12)]  # minutes after 9:30: 10:00 ... 15:30


def bar_index(offset_minutes):
    """Index of the bar whose close is the price at `offset_minutes` after 9:30."""
    return offset_minutes - 1


def sigma_for_checks(hist_open, hist_close):
    """
    hist_open:  array (days,) of 9:30 opens for the last LOOKBACK full days
    hist_close: array (days, 390) of minute closes for the same days
    returns {offset: sigma}
    """
    hist_open = np.asarray(hist_open, dtype=float)
    hist_close = np.asarray(hist_close, dtype=float)
    out = {}
    for off in CHECK_OFFSETS:
        moves = np.abs(hist_close[:, bar_index(off)] / hist_open - 1)
        out[off] = float(np.mean(moves))
    return out


def bands(today_open, prev_close, sigma, vm=1.0):
    upper = max(today_open, prev_close) * (1 + vm * sigma)
    lower = min(today_open, prev_close) * (1 - vm * sigma)
    return upper, lower


def leverage(daily_closes, max_leverage=MAX_LEVERAGE, vol_target=VOL_TARGET):
    """daily_closes: last LOOKBACK+1 daily closes (oldest first)."""
    c = np.asarray(daily_closes, dtype=float)
    if len(c) < LOOKBACK + 1:
        return 0.0
    r = c[1:] / c[:-1] - 1
    sd = float(np.std(r[-LOOKBACK:], ddof=1))
    if not math.isfinite(sd) or sd <= 0:
        return 0.0
    return min(max_leverage, vol_target / sd)


def decide(pos, price, upper, lower, vwap):
    """Return the new position (-1, 0, +1) at a check."""
    long_line = max(upper, vwap)
    short_line = min(lower, vwap)
    if pos == 1 and price < long_line:
        pos = 0
    elif pos == -1 and price > short_line:
        pos = 0
    if pos == 0:
        if price > long_line:
            return 1
        if price < short_line:
            return -1
    return pos
