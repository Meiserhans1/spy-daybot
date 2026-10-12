# SPY Day Bot

An automatic SPY day trading bot for Alpaca. It runs on GitHub's computers on
its own schedule, so your PC can stay off, and it is completely separate from
your TQQQ bot.

## What it does

The bot follows the "noise area" intraday momentum strategy from Zarattini,
Aziz & Barbon (2024), *Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)*.

1. **Normal range.** Every morning it works out how far SPY normally moves
   from the 9:30 open by each time of day, using the last 14 days. That
   gives an upper and a lower line.
2. **Checks every 30 minutes**, from 10:00 to 15:30:
   - Above the upper line and above VWAP → it goes **long**.
   - Below the lower line and below VWAP → it goes **short**.
   - In a trade, it gets out as soon as price falls back inside, using VWAP
     or the line (whichever is tighter) as a trailing stop.
3. **Always flat at 15:59.** Nothing is held overnight.
4. **Size.** Position size shrinks when the market is wild and grows when it
   is calm. It aims for about 2% daily swings and never uses more than 4x
   leverage.

## How it did in testing

I tested it on S&P 500 one-minute data from May 2007 to May 2020, charging
0.2 bp per side for costs:

| | Bot | S&P 500 buy & hold |
|---|---|---|
| Yearly return | about +16% | about +5% (price only) |
| Worst drawdown | about −19% | about −57% |
| Losing years | 2 of 14 (2016 −12%, 2017 −1%); 2010 about flat | |
| Big crash years | 2008 +62%, 2020 (to May) +8% | 2008 −38% |

Some other things the testing showed:

- **Costs matter.** With costs set 2.5 times higher, the yearly return
  drops to about 12%.
- **Timing matters a little.** Running the checks a few minutes off the
  exact :00 and :30 times still worked at every offset I tried (Sharpe 0.7
  to 1.2). The exact times were the best.
- **Expect normal ups and downs.** It wins on only about 41% of days. The
  money comes from the big trend days, so losing weeks are normal.

None of this guarantees future results. Paper trade it first.

## Why it can't interfere with your other bot

- **Separate Alpaca account.** It trades with its own keys
  (`DAYBOT_KEY` / `DAYBOT_SECRET`), so it never shares money or positions
  with the TQQQ bot.
- **Separate repo and schedule.** It has its own workflow and
  `concurrency` group, so neither bot ever waits on or cancels the other.
- **Only touches its own orders.** It only trades SPY. Every order is
  tagged `daybot-`, it only cancels tagged orders, and it closes positions
  with orders for exactly its own share count.
- **No shared GitHub minutes, as long as this repo is public.** The bot runs
  about 6.5 hours every trading day. GitHub's free private-repo minutes
  (2,000 a month) would run out in about a week, and when they do, *every*
  workflow on your account stops, including the TQQQ bot. Public repos get
  unlimited free minutes.
  - Your API keys stay hidden in Secrets.
  - The logs show percentages, never dollar balances.

## Setup (about 10 minutes)

1. **Get an Alpaca paper account for this bot.** Your old ~$100k paper
   account works, or you can make a new one. Copy its API key and secret.
2. **Add the keys to this repo.** Go to **Settings → Secrets and variables
   → Actions → New repository secret** and add:
   - `DAYBOT_KEY`
   - `DAYBOT_SECRET`
3. **Turn on Actions.** Open the **Actions** tab and enable workflows if
   GitHub asks.
4. **Optional: check recent years.** Run **Day bot backtest (recent data)**.
   It tests the exact same rules on real SPY data from 2016 to yesterday,
   and the results show on the run page.
5. **Optional: dry run.** Run **Day bot (SPY intraday)** with "Dry run"
   ticked. It logs what it would do without sending orders.

After that, it runs by itself every weekday. It skips holidays and half days.

## Checking on it

- **Weekly:** look at the Alpaca paper dashboard.
- **Per day:** each day's log is in **Actions → Day bot (SPY intraday)**.
  You'll see one line per check: price, the two lines, VWAP, and the
  position.
- **To pause:** Actions → Day bot → **⋯ → Disable workflow**.

## Settings (repository variables, optional)

| Variable | Default | Meaning |
|---|---|---|
| `DAYBOT_PAPER` | `true` | Set to `false` only when you decide to trade real money |
| `DAYBOT_MAX_LEVERAGE` | `4` | Lower = smaller positions and smaller swings |

## Files

- `daybot/strategy.py`: the rules, shared by the bot and the backtest.
- `daybot/bot.py`: the live bot.
- `daybot/backtest_recent.py`: backtest on Alpaca data.
- `.github/workflows/daybot.yml`: the weekday schedule, run as a morning
  job and an afternoon job.
- `.github/workflows/daybot-backtest.yml`: the manual backtest.
