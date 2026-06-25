# Market Simulator

The simulator generates realistic-looking price movements without any external API. It is the default provider when `MASSIVE_API_KEY` is not set.

---

## Price Model: Geometric Brownian Motion (GBM)

Each tick uses GBM, the standard model for continuous stock prices. The formula ensures prices are always positive and exhibit realistic log-normal distribution:

```
price_t = price_{t-1} * exp((μ - 0.5σ²)·Δt + σ·√Δt·Z)
```

Where:
- `μ` (drift) — annualized expected return, per-ticker
- `σ` (volatility) — annualized price volatility, per-ticker
- `Δt` — time step in years (`0.5 / (252 * 6.5 * 3600)` for a 500ms tick in trading-time)
- `Z` — standard normal random variable, correlated with a market factor

---

## Correlated Market Factor

To simulate realistic co-movement (tech stocks move together on macro news), each tick generates a **shared market factor** mixed with per-ticker idiosyncratic noise:

```
market_factor ~ N(0, 1)          # one per tick, shared across all tickers
noise_i ~ N(0, 1)                # independent per ticker

Z_i = β_i · market_factor + √(1 - β_i²) · noise_i
```

`β` (beta) controls market sensitivity:
- Tech stocks (AAPL, GOOGL, MSFT, NVDA, META, AMZN, TSLA, NFLX): `β ≈ 0.7`
- Defensive / financial (JPM, V): `β ≈ 0.3`

Tickers added dynamically by the user (not in the default set) get `β = 0.5`.

---

## Occasional Random Events

Every ~30 seconds on average, a random ticker experiences a sudden 2–5% price move to simulate news/earnings surprises. This keeps the UI visually interesting.

```python
# On each tick, with small probability per ticker:
if random.random() < EVENT_PROBABILITY:
    direction = random.choice([-1, 1])
    magnitude = random.uniform(0.02, 0.05)
    price *= (1 + direction * magnitude)
```

`EVENT_PROBABILITY` is set so that across all tickers and tick frequency, one event fires roughly once per 30 seconds.

---

## Seed Prices and Ticker Parameters

```python
# backend/market/simulator.py

SEED_PRICES: dict[str, float] = {
    "AAPL":  190.0,
    "GOOGL": 175.0,
    "MSFT":  415.0,
    "AMZN":  185.0,
    "TSLA":  250.0,
    "NVDA":  875.0,
    "META":  500.0,
    "JPM":   200.0,
    "V":     285.0,
    "NFLX":  650.0,
}

# Per-ticker GBM parameters
# drift: annualized expected return (small positive = trending up slightly)
# vol: annualized volatility (typical stocks: 0.15–0.40)
# beta: market factor sensitivity
TICKER_PARAMS: dict[str, dict] = {
    "AAPL":  {"drift": 0.08, "vol": 0.22, "beta": 0.70},
    "GOOGL": {"drift": 0.07, "vol": 0.24, "beta": 0.70},
    "MSFT":  {"drift": 0.09, "vol": 0.20, "beta": 0.70},
    "AMZN":  {"drift": 0.08, "vol": 0.28, "beta": 0.70},
    "TSLA":  {"drift": 0.05, "vol": 0.55, "beta": 0.70},
    "NVDA":  {"drift": 0.12, "vol": 0.45, "beta": 0.70},
    "META":  {"drift": 0.08, "vol": 0.30, "beta": 0.70},
    "JPM":   {"drift": 0.06, "vol": 0.18, "beta": 0.30},
    "V":     {"drift": 0.07, "vol": 0.17, "beta": 0.30},
    "NFLX":  {"drift": 0.06, "vol": 0.35, "beta": 0.70},
}

# Defaults for tickers not in the above table
DEFAULT_PARAMS = {"drift": 0.07, "vol": 0.25, "beta": 0.50}
```

---

## Tick Timing

| Constant | Value |
|---|---|
| `TICK_INTERVAL_S` | `0.5` — background task sleeps 500ms between ticks |
| `TRADING_DAYS_PER_YEAR` | `252` |
| `TRADING_HOURS_PER_DAY` | `6.5` |
| `DT` | `0.5 / (252 * 6.5 * 3600)` — fraction of a trading year per 500ms tick |
| `EVENT_PROBABILITY` | `0.5 / (252 * 6.5 * 3600 / 30)` — ~1 event per 30 real seconds |

---

## Full Implementation Sketch

```python
# backend/market/simulator.py

import asyncio
import math
import random
import time
from typing import Optional

from .base import MarketDataProvider
from .cache import PriceCache
from .types import PriceUpdate

TICK_INTERVAL_S = 0.5
TRADING_DAYS_PER_YEAR = 252
TRADING_HOURS_PER_DAY = 6.5
DT = TICK_INTERVAL_S / (TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 3600)
# probability that a given ticker fires a random event on any single tick
EVENT_PROBABILITY = TICK_INTERVAL_S / (TRADING_DAYS_PER_YEAR * TRADING_HOURS_PER_DAY * 3600 / 30)

SEED_PRICES = { ... }   # as above
TICKER_PARAMS = { ... } # as above
DEFAULT_PARAMS = {"drift": 0.07, "vol": 0.25, "beta": 0.50}

# Known valid tickers (for validate_ticker)
KNOWN_TICKERS: set[str] = set(SEED_PRICES.keys())


class MarketSimulator(MarketDataProvider):
    def __init__(self) -> None:
        self._cache = PriceCache()
        self._prices: dict[str, float] = {}   # current price per ticker
        self._tickers: set[str] = set()
        self._task: Optional[asyncio.Task] = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = set(t.upper() for t in tickers)
        # Seed initial prices
        for ticker in self._tickers:
            self._prices[ticker] = SEED_PRICES.get(ticker, 100.0)
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def get_price(self, ticker: str) -> Optional[PriceUpdate]:
        return self._cache.get(ticker.upper())

    def get_all_prices(self) -> dict[str, PriceUpdate]:
        return self._cache.get_all()

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker not in self._tickers:
            self._tickers.add(ticker)
            self._prices[ticker] = SEED_PRICES.get(ticker, 100.0)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        self._tickers.discard(ticker)
        self._prices.pop(ticker, None)
        await self._cache.remove(ticker)

    async def validate_ticker(self, ticker: str) -> bool:
        # Accept any uppercase alphabetic symbol 1–5 chars
        t = ticker.upper().strip()
        return bool(t) and t.isalpha() and 1 <= len(t) <= 5

    async def _tick_loop(self) -> None:
        while True:
            await self._do_tick()
            await asyncio.sleep(TICK_INTERVAL_S)

    async def _do_tick(self) -> None:
        if not self._tickers:
            return

        market_factor = random.gauss(0, 1)
        updates: list[PriceUpdate] = []

        for ticker in list(self._tickers):
            params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
            drift = params["drift"]
            vol = params["vol"]
            beta = params["beta"]

            prev_price = self._prices[ticker]
            noise = random.gauss(0, 1)
            z = beta * market_factor + math.sqrt(1 - beta**2) * noise
            new_price = prev_price * math.exp(
                (drift - 0.5 * vol**2) * DT + vol * math.sqrt(DT) * z
            )

            # Occasional random event
            if random.random() < EVENT_PROBABILITY:
                direction = random.choice([-1, 1])
                magnitude = random.uniform(0.02, 0.05)
                new_price *= (1 + direction * magnitude)

            new_price = max(new_price, 0.01)   # price floor
            self._prices[ticker] = new_price
            updates.append(PriceUpdate.from_prices(ticker, new_price, prev_price))

        await self._cache.set_many(updates)
```

---

## Ticker Validation Policy

The simulator's `validate_ticker` uses a loose rule: any 1–5 character alphabetic string is accepted. This lets the user add any ticker symbol to their watchlist without rejecting unknown ones, which is appropriate when operating without real market data.

If you want stricter validation in simulator mode, you can expand `KNOWN_TICKERS` with a broader list of real NYSE/NASDAQ symbols and check membership instead.

---

## Why GBM?

GBM is the simplest continuous-time model that satisfies three requirements:
1. Prices are always positive (log-normal distribution)
2. Price changes are proportional to current price (percentage moves, not absolute)
3. Future prices are independent of price history (memoryless — Markov property)

It produces visually realistic price charts and is the foundation of Black-Scholes option pricing. The per-ticker drift and volatility parameters allow tuning the "personality" of each stock (TSLA is wilder than V; NVDA trends faster than JPM).

---

## Adding New Default Tickers

To add a new default ticker to the simulator:
1. Add its seed price to `SEED_PRICES`
2. Add its parameters to `TICKER_PARAMS`
3. Add it to `SEED_TICKERS` in `backend/db/seed.py` (so it appears in the default watchlist)

The simulator dynamically handles tickers added at runtime (user adds to watchlist) using `DEFAULT_PARAMS` and a seed price of $100.00, so no code change is needed for user-added tickers.
