# Market Data Backend — Implementation Design

This document is the authoritative implementation reference for the FinAlly market data subsystem. It covers every module in `backend/app/market/`, the unified interface that makes both data sources interchangeable, the GBM simulator, the Massive API client, the SSE streaming endpoint, and how everything wires together at app startup.

All code in this document reflects the actual production implementation in `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Unified Interface — `interface.py`](#5-unified-interface)
6. [Seed Data — `seed_prices.py`](#6-seed-data)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Downstream Consumer Usage](#13-downstream-consumer-usage)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Testing Strategy](#15-testing-strategy)
16. [Configuration Reference](#16-configuration-reference)

---

## 1. Architecture Overview

The market data layer follows a **strategy pattern** with a **shared write-through cache**.

```
Environment variable
  MASSIVE_API_KEY set? ──Yes──▶ MassiveDataSource ──┐
                        │                            │ writes
                        No──▶ SimulatorDataSource ──┤
                                                     ▼
                                               PriceCache (in-memory, thread-safe)
                                                     │
                              ┌──────────────────────┤ reads
                              │                      │
                              ▼                      ▼
                    SSE /api/stream/prices    Trade execution
                    (all connected clients)   Portfolio valuation
```

**Key design decisions:**

- **Push model, not pull**: Data sources write to `PriceCache` on their own schedule. SSE handlers and portfolio routes read from the cache. There is no direct dependency between the data source and its consumers.
- **Single cache instance**: Created once at app startup, passed everywhere. No global variables.
- **Swappable sources**: `SimulatorDataSource` and `MassiveDataSource` implement the same `MarketDataSource` ABC. Every consumer is source-agnostic — changing data sources requires only an environment variable change.
- **Async event loop friendly**: The simulator runs as an `asyncio.Task`. The synchronous Massive client runs via `asyncio.to_thread()`. The cache uses `threading.Lock` so both paths are safe.

---

## 2. File Structure

```
backend/
├── app/
│   └── market/
│       ├── __init__.py          # Re-exports public API
│       ├── models.py            # PriceUpdate dataclass
│       ├── cache.py             # PriceCache — thread-safe in-memory store
│       ├── interface.py         # MarketDataSource ABC
│       ├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
│       ├── simulator.py         # GBMSimulator + SimulatorDataSource
│       ├── massive_client.py    # MassiveDataSource
│       ├── factory.py           # create_market_data_source() factory
│       └── stream.py            # SSE endpoint (FastAPI router factory)
└── tests/
    └── market/
        ├── test_models.py
        ├── test_cache.py
        ├── test_simulator.py
        ├── test_simulator_source.py
        ├── test_factory.py
        └── test_massive.py
```

The `__init__.py` exports exactly five names — everything else is internal:

```python
# backend/app/market/__init__.py
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

Downstream code imports like this:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. It is immutable and carries everything the frontend or portfolio logic needs.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Why these design choices?

| Choice | Reason |
|--------|--------|
| `frozen=True` | Makes `PriceUpdate` a value object — safe to share across async tasks without copying. Prevents accidental mutation. |
| `slots=True` | Minor memory optimization — the simulator creates ~20 updates/second at 10 tickers. |
| Computed properties | `change`, `change_percent`, and `direction` are derived from `price` and `previous_price`. They can never be inconsistent with each other. |
| `to_dict()` | Single serialization point used by both SSE events and REST responses. Format matches what the frontend expects. |

### `to_dict()` output example

```json
{
  "ticker": "AAPL",
  "price": 190.50,
  "previous_price": 190.42,
  "timestamp": 1707580800.123,
  "change": 0.08,
  "change_percent": 0.042,
  "direction": "up"
}
```

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The `PriceCache` is the shared state between producers (data sources) and consumers (SSE, portfolio routes). It uses `threading.Lock` because the Massive client's synchronous API call runs in a real OS thread via `asyncio.to_thread()`.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        If this is the first update for the ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest PriceUpdate for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Version counter for SSE change detection

The SSE streaming loop polls every 500ms. Without a version counter, it would re-serialize and re-send all prices every tick even if nothing changed — for example when the Massive free tier only updates every 15 seconds. The version counter allows skipping sends:

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        prices = price_cache.get_all()
        if prices:
            yield f"data: {json.dumps({t: u.to_dict() for t, u in prices.items()})}\n\n"
    await asyncio.sleep(0.5)
```

### Thread safety rationale

`threading.Lock` (not `asyncio.Lock`) because:
- `MassiveDataSource._fetch_snapshots()` runs in `asyncio.to_thread()` — a real OS thread. `asyncio.Lock` only works within the async event loop.
- `GBMSimulator.step()` is CPU-bound and could be offloaded to a thread executor in the future.
- `threading.Lock` works correctly from both sync threads and the async event loop.

---

## 5. Unified Interface

**File: `backend/app/market/interface.py`**

The abstract base class that both data sources implement. All downstream code (watchlist routes, portfolio routes) depends only on this interface, never on `SimulatorDataSource` or `MassiveDataSource` directly.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Interface contract summary

| Method | Blocking? | Side effects |
|--------|-----------|-------------|
| `start(tickers)` | `async` | Starts background task; seeds cache with initial prices immediately |
| `stop()` | `async` | Cancels background task; safe to call twice |
| `add_ticker(ticker)` | `async` | Adds to tracked set; seeds cache immediately (simulator) or on next poll (Massive) |
| `remove_ticker(ticker)` | `async` | Removes from tracked set; removes from cache immediately |
| `get_tickers()` | sync | Returns current ticker list; no side effects |

---

## 6. Seed Data

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports. These values are used by the simulator for initial prices, GBM parameters, and the correlation matrix. The Massive client also uses these as fallback prices if the API hasn't responded yet.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement per tick)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers added dynamically (not in the table above)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Pairwise correlation coefficients
INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA behaves independently despite being in tech
```

### Tuning the "personality" of each stock

The `sigma` (volatility) and `mu` (drift) values shape what each stock "feels" like in the UI:

| Ticker | Sigma | Personality |
|--------|-------|-------------|
| V | 0.17 | Boring, stable payments stock |
| JPM | 0.18 | Steady bank stock |
| MSFT | 0.20 | Reliable large-cap tech |
| AAPL | 0.22 | Slightly more active than MSFT |
| GOOGL | 0.25 | Active large-cap tech |
| AMZN | 0.28 | Moderately volatile |
| META | 0.30 | More volatile tech |
| NFLX | 0.35 | Volatile streaming stock |
| NVDA | 0.40 | AI chip stock, moves a lot |
| TSLA | 0.50 | Wildcard — moves the most |

### Adding a new default ticker

1. Add its seed price to `SEED_PRICES`
2. Add its `sigma` and `mu` to `TICKER_PARAMS`
3. Add the ticker to `SEED_TICKERS` in `backend/db/seed.py` (so it appears in the default watchlist)
4. If it belongs to a sector group, add it to `CORRELATION_GROUPS`

For tickers added by users at runtime (not in `SEED_PRICES`), the simulator uses `DEFAULT_PARAMS` and assigns a random seed price between $50 and $300.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes with a clear boundary:

- **`GBMSimulator`**: Pure math engine. Stateful (holds current prices). Synchronous. Completely testable without asyncio.
- **`SimulatorDataSource`**: `MarketDataSource` implementation. Wraps `GBMSimulator` in an asyncio loop and writes to `PriceCache`.

### 7.1 The GBM Math

Geometric Brownian Motion is the standard continuous-time model for stock prices. The key property: prices are always positive (the exponential function can never go negative).

```
S(t+dt) = S(t) * exp( (μ - σ²/2)·dt  +  σ·√dt·Z )
           │              │                  │
           │          drift term         diffusion term
           │        (systematic)         (random noise)
     current price
```

Where:
- `μ` (mu) = annualized expected return (drift), e.g. 0.05 = 5%/year
- `σ` (sigma) = annualized volatility, e.g. 0.22 = 22%/year
- `dt` = time step as a fraction of a trading year
- `Z` = standard normal random variable (correlated across tickers)

For a 500ms tick in trading-time:
```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # = 5,896,800
dt = 0.5 / TRADING_SECONDS_PER_YEAR           # ≈ 8.48e-8
```

This tiny `dt` means each 500ms tick produces very small moves (~0.01% for AAPL) that accumulate into realistic-looking price charts over time.

### 7.2 Correlated Moves via Cholesky Decomposition

Stocks in the same sector tend to move together. Tech stocks rise and fall together on macro news. The simulator replicates this via a correlation matrix and its Cholesky decomposition.

**Step 1: Build the correlation matrix**

```
           AAPL  GOOGL  MSFT  TSLA  JPM    V
AAPL   [  1.0   0.6    0.6   0.3   0.3   0.3  ]
GOOGL  [  0.6   1.0    0.6   0.3   0.3   0.3  ]
MSFT   [  0.6   0.6    1.0   0.3   0.3   0.3  ]
TSLA   [  0.3   0.3    0.3   1.0   0.3   0.3  ]
JPM    [  0.3   0.3    0.3   0.3   1.0   0.5  ]
V      [  0.3   0.3    0.3   0.3   0.5   1.0  ]
```

**Step 2: Cholesky decomposition**

```python
L = np.linalg.cholesky(corr)   # Lower triangular matrix, L @ L.T = corr
```

**Step 3: Generate correlated noise**

```python
z_independent = np.random.standard_normal(n)  # n = number of tickers
z_correlated  = L @ z_independent             # Each z_correlated[i] correlates with the others
```

Each element of `z_correlated` is a standard normal variable that is correlated with all others according to the correlation matrix. Multiplying the price update by `exp(... * z_correlated[i])` applies a correlated random shock.

### 7.3 GBMSimulator — Full Implementation

```python
import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS,
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES, TICKER_PARAMS, TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}.

        Hot path — called every 500ms. Vectorized noise generation via numpy.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws, then correlate them
        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            # GBM formula: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift     = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec → expect ~1 event every 50 seconds
            if random.random() < self._event_prob:
                magnitude = random.uniform(0.02, 0.05)
                sign      = random.choice([-1, 1])
                self._prices[ticker] *= 1 + magnitude * sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker, magnitude * 100, "up" if sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild correlation matrix Cholesky decomposition. O(n^2), n < 50."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        tech    = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### 7.4 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator.

    Runs a background asyncio task that calls step() every update_interval
    seconds and writes results to PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache   = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately — SSE clients get data on their first poll
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### Key behaviors

- **Immediate cache seeding**: `start()` populates the cache with seed prices *before* the background loop starts. The SSE endpoint has data to send on its very first poll — no blank-screen delay.
- **Ticker-level event isolation**: exceptions in `_run_loop` are caught per-step, not per-ticker. A bad tick doesn't kill the feed.
- **Graceful stop**: `stop()` cancels the task and awaits it, catching `CancelledError`. Clean shutdown on FastAPI lifespan teardown.
- **Dynamic watchlist**: `add_ticker()` and `remove_ticker()` both trigger a Cholesky rebuild inside `GBMSimulator`. This is O(n²) but n stays small (<50 tickers).

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Massive (formerly Polygon.io, rebranded October 2025) provides real stock market data via a REST API. This client polls the `/v2/snapshot/locale/us/markets/stocks/tickers` endpoint at a configurable interval and writes results to `PriceCache`.

The Massive `RESTClient` is synchronous. It runs via `asyncio.to_thread()` to avoid blocking the event loop.

```python
from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call per cycle.

    Rate limits:
      - Free tier:  5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key  = api_key
        self._cache    = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client  = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        # Immediate first poll — cache has data before the periodic loop starts
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task   = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price     = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — loop retries on next interval

    def _fetch_snapshots(self) -> list:
        """Synchronous Massive API call. Must run in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Massive API snapshot response structure

The `get_snapshot_all()` call returns a list of snapshot objects. The key fields used:

```python
snap.ticker                    # "AAPL"
snap.last_trade.price          # 190.73  (most recent trade price)
snap.last_trade.timestamp      # 1718304000000  (Unix milliseconds)
snap.day.close                 # 190.73  (fallback if last_trade is None)
snap.prev_day.close            # 189.50  (previous session close)
snap.todays_change_perc        # 0.65    (% change from prev close)
```

### Price extraction fallback

During pre/post-market hours, `last_trade` may be `None`. The `_poll_once` method handles this:

```python
try:
    price = snap.last_trade.price   # Most current during market hours
    timestamp = snap.last_trade.timestamp / 1000.0
except (AttributeError, TypeError):
    # Fallback: day close (pre/post market, or no recent trade)
    logger.warning("Skipping snapshot for %s: no last_trade", snap.ticker)
    # Skip this ticker — cache retains last known price
```

### Error handling philosophy

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as `error`. Poller keeps running — fix the key and restart. |
| 429 Rate Limited | Logged as `error`. Retries after `poll_interval` seconds. |
| Network timeout | Logged as `error`. Retries automatically on next cycle. |
| Malformed snapshot | Individual ticker skipped with `warning`. Others still processed. |
| All tickers fail | Cache retains last-known prices. SSE streams stale data (better than nothing). |

### Rate limits

| Tier | Limit | Recommended poll interval |
|------|-------|--------------------------|
| Free | 5 req/min | 15s (default) |
| Starter | Unlimited | 5-10s |
| Advanced | Unlimited | 2-5s |

All 10 watched tickers are fetched in a **single API call** per poll cycle — not one call per ticker. This keeps the request count at 1 per poll interval regardless of watchlist size.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

The factory is the single decision point for which data source is used. No other code checks `MASSIVE_API_KEY`.

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select and create the appropriate market data source.

    Decision:
      - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
      - Otherwise                         → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. The caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage at startup

```python
# In backend/app/main.py:
price_cache = PriceCache()
source = create_market_data_source(price_cache)     # reads MASSIVE_API_KEY
initial_tickers = await load_watchlist_from_db()    # e.g. ["AAPL", "GOOGL", ...]
await source.start(initial_tickers)
```

### Provider selection summary

| `MASSIVE_API_KEY` value | Provider selected |
|------------------------|------------------|
| Unset or empty string | `SimulatorDataSource` |
| Any non-empty string | `MassiveDataSource` |

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

The SSE endpoint is a long-lived HTTP connection that pushes price updates to connected browser clients. The browser uses the native `EventSource` API.

```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with an injected PriceCache reference."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint: GET /api/stream/prices

        Streams all tracked ticker prices to the client. Events are sent
        whenever the PriceCache version changes (i.e., any price updates).

        Client connects with:
            const es = new EventSource('/api/stream/prices');
            es.onmessage = (e) => {
                const prices = JSON.parse(e.data);
                // prices: { "AAPL": { ticker, price, previous_price, ... }, ... }
            };
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted price events.

    Uses version-based change detection: events are only sent when
    the cache has been updated since the last send.
    """
    yield "retry: 1000\n\n"  # Browser retries after 1s if connection drops

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE wire format

Each event the client receives is a single `data:` line followed by a blank line:

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...},...}

data: {"AAPL":{"ticker":"AAPL","price":190.48,...},...}

```

### Frontend consumption

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": PriceUpdate, "GOOGL": PriceUpdate, ... }
    for (const [ticker, update] of Object.entries(prices)) {
        updateWatchlistRow(ticker, update);
        appendSparklinePoint(ticker, update.price);
        if (ticker === selectedTicker) {
            appendMainChartPoint(update);
        }
    }
};

eventSource.onerror = (e) => {
    // EventSource auto-reconnects — the retry: 1000 directive tells it
    // to wait 1 second before retrying. No manual reconnection needed.
    setConnectionStatus('reconnecting');
};
```

### Push cadence vs. poll interval

The SSE generator wakes every 500ms. It sends an event only when `price_cache.version` has changed since the last send:

| Data source | Cache update rate | SSE event rate |
|-------------|------------------|----------------|
| Simulator | Every 500ms | ~500ms (every wake) |
| Massive (free) | Every 15s | ~15s (skips 29 wakes) |
| Massive (paid) | Every 2-5s | ~2-5s |

This means the 500ms SSE wake interval is cheap — it's just an integer comparison and a `asyncio.sleep`. The expensive part (JSON serialization, socket write) only happens when data actually changed.

---

## 11. FastAPI Lifecycle Integration

The market data system starts and stops with the FastAPI app via `lifespan`.

```python
# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---

    # Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # Create and start the market data source (reads MASSIVE_API_KEY)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers_from_db()
    await source.start(initial_tickers)

    # Register the SSE router
    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# FastAPI dependency injectors for route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Using market data in route handlers

```python
from fastapi import APIRouter, Depends, HTTPException
from app.market import PriceCache, MarketDataSource
from app.main import get_price_cache, get_market_source

router = APIRouter(prefix="/api")

# Trade execution: read price from cache
@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": "Price not yet available. Please wait a moment and try again.",
                "code": "PRICE_UNAVAILABLE",
            }
        )
    # ... execute trade at current_price ...

# Watchlist management: notify source of ticker changes
@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = payload.ticker.upper().strip()
    # ... validate ticker, check duplicates, persist to DB ...
    await source.add_ticker(ticker)   # Starts tracking immediately
    price = price_cache.get_price(ticker)
    return {"success": True, "data": {"ticker": ticker, "price": price}}

@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper()
    # ... remove from DB ...
    # Only stop tracking if no open position (see Section 12)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    return {"success": True, "data": {"ticker": ticker}}
```

---

## 12. Watchlist Coordination

When the watchlist changes — via REST API or LLM chat — the market data source must be kept in sync.

### Adding a ticker

```
User/LLM → POST /api/watchlist { "ticker": "PYPL" }
  1. Validate ticker (not a known error code, etc.)
  2. Check for duplicate (UNIQUE constraint in DB → 409 DUPLICATE_TICKER)
  3. Insert into watchlist table (SQLite)
  4. await source.add_ticker("PYPL")
       Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
       Massive:   appends to poll list, new price appears on next poll cycle
  5. Return { "success": true, "data": { "ticker": "PYPL", "price": 65.20 } }
```

### Removing a ticker

```
User/LLM → DELETE /api/watchlist/PYPL
  1. Delete from watchlist table (SQLite)
  2. Check if user holds shares (positions table)
     → If position exists AND quantity > 0: do NOT remove from source
       (still need live price for portfolio valuation)
     → If no position or quantity == 0: await source.remove_ticker("PYPL")
  3. Return { "success": true, "data": { "ticker": "PYPL" } }
```

### Edge case: removing a ticker with an open position

This edge case matters. The user may want to remove TSLA from their watchlist but still hold 10 shares. Stopping price updates for TSLA would break portfolio valuation.

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper()
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    # If they hold shares, the source keeps tracking — ticker stays in cache.
    # The frontend watchlist UI will hide it; the portfolio panel still uses it.
    return {"success": True, "data": {"ticker": ticker}}
```

---

## 13. Downstream Consumer Usage

### Reading a single ticker price

```python
price = price_cache.get_price("AAPL")   # float or None
if price is None:
    # Ticker not yet tracked, or data source hasn't produced first update
    raise HTTPException(400, ...)
```

### Reading all current prices

```python
prices: dict[str, PriceUpdate] = price_cache.get_all()
for ticker, update in prices.items():
    print(f"{ticker}: ${update.price:.2f} ({update.direction})")
```

### Portfolio snapshot valuation

```python
async def take_portfolio_snapshot(user_id: str = "default") -> float:
    """Compute total portfolio value and record a snapshot."""
    positions = await db.get_positions(user_id)
    cash = await db.get_cash_balance(user_id)

    total = cash
    for pos in positions:
        current_price = price_cache.get_price(pos.ticker)
        if current_price:
            total += pos.quantity * current_price

    await db.insert_portfolio_snapshot(user_id=user_id, total_value=total)
    return total
```

### Portfolio positions with P&L

```python
async def get_portfolio(user_id: str = "default") -> dict:
    positions = await db.get_positions(user_id)
    cash = await db.get_cash_balance(user_id)

    enriched = []
    for pos in positions:
        update = price_cache.get(pos.ticker)
        current_price = update.price if update else None
        unrealized_pnl = (
            (current_price - pos.avg_cost) * pos.quantity
            if current_price else None
        )
        enriched.append({
            "ticker":        pos.ticker,
            "quantity":      pos.quantity,
            "avg_cost":      pos.avg_cost,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "change_percent": update.change_percent if update else None,
        })

    total_value = cash + sum(
        (p["current_price"] or 0) * p["quantity"] for p in enriched
    )

    return {
        "success": True,
        "data": {
            "cash": cash,
            "total_value": total_value,
            "positions": enriched,
        }
    }
```

### Watchlist with live prices

```python
async def get_watchlist(user_id: str = "default") -> dict:
    tickers = await db.get_watchlist(user_id)
    result = []
    for ticker in tickers:
        update = price_cache.get(ticker)
        result.append({
            "ticker":         ticker,
            "price":          update.price if update else None,
            "change_percent": update.change_percent if update else None,
            "direction":      update.direction if update else None,
        })
    return {"success": True, "data": result}
```

---

## 14. Error Handling & Edge Cases

### Empty watchlist at startup

If the database has no watchlist entries, `start([])` is called. Both data sources handle this:
- Simulator produces no prices; loop still runs
- Massive skips the API call; loop still runs

When the user adds the first ticker, the source starts tracking it normally.

### Price cache miss during trade execution

New tickers in Massive mode may not have a cached price yet (API hasn't polled since `add_ticker()`). The Massive `start()` does an immediate first poll, but `add_ticker()` does not — the new ticker appears on the next scheduled poll. During that window, `get_price()` returns `None`.

Handle it in the trade route:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail={
            "success": False,
            "error": f"Price for {ticker} not yet available. Please try again in a moment.",
            "code": "PRICE_UNAVAILABLE",
        }
    )
```

The simulator avoids this gap by seeding the cache in `add_ticker()` before the next loop tick.

### Invalid Massive API key

401 from the API → logged as an error, poller continues retrying. The SSE stream sends empty events (no prices). The frontend connection status indicator shows "connected" (SSE is working) but prices are all `null`. Fix: correct the key and restart the container.

### Rate limit exceeded (Massive 429)

Logged as an error. The poller waits `poll_interval` seconds and retries. Cache retains last-known prices — users see stale prices rather than no prices.

### Correlation matrix rebuild on dynamic tickers

When a user adds a non-default ticker (e.g., "COIN") to their watchlist, the simulator calls `_rebuild_cholesky()`. This rebuilds an (n+1)×(n+1) matrix. For n < 50 tickers this is fast (microseconds). The new ticker uses `DEFAULT_PARAMS` (σ=0.25, μ=0.05) and a random seed price between $50 and $300.

### Thread safety under concurrent SSE clients

Multiple browser tabs connect to `/api/stream/prices`. Each SSE handler runs in its own async coroutine. All of them call `price_cache.get_all()`, which acquires `threading.Lock` briefly. With 10 tickers the critical section is a single dict copy — negligible contention even with dozens of simultaneous clients.

---

## 15. Testing Strategy

All tests live in `backend/tests/market/`. Run with:

```bash
cd backend
uv run --extra dev pytest tests/market/ -v
uv run --extra dev pytest tests/market/ --cov=app/market
```

### 15.1 Test: GBMSimulator core math

```python
# backend/tests/market/test_simulator.py
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert set(sim.step().keys()) == {"AAPL", "GOOGL"}

    def test_prices_always_positive(self):
        sim = GBMSimulator(tickers=["AAPL", "TSLA"])
        for _ in range(10_000):
            prices = sim.step()
            assert all(p > 0 for p in prices.values())

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_unknown_ticker_gets_random_seed(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert 50.0 <= price <= 300.0

    def test_add_ticker_appears_in_next_step(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        assert "TSLA" in sim.step()

    def test_remove_ticker_disappears_from_next_step(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        assert "GOOGL" not in sim.step()

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim._tickers) == 1

    def test_cholesky_none_for_single_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_exists_for_multiple_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None

    def test_empty_step(self):
        sim = GBMSimulator(tickers=[])
        assert sim.step() == {}
```

### 15.2 Test: PriceCache

```python
# backend/tests/market/test_cache.py
from app.market.cache import PriceCache


class TestPriceCache:

    def test_update_and_get(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") is update

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up_and_change(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.0

    def test_direction_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_get_all_returns_snapshot(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"AAPL", "GOOGL"}

    def test_version_increments_on_update(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_version_does_not_increment_on_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        v = cache.version
        cache.remove("AAPL")
        assert cache.version == v  # remove doesn't bump version

    def test_get_price_convenience(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None
```

### 15.3 Integration Test: SimulatorDataSource

```python
# backend/tests/market/test_simulator_source.py
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_populates_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=100.0)
        await source.start(["AAPL", "GOOGL"])
        # Cache has data before any loop tick (seeded from GBMSimulator)
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])
        v_start = cache.version
        await asyncio.sleep(0.3)   # 6 ticks
        assert cache.version > v_start
        await source.stop()

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=100.0)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None   # Seeded immediately

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()

    async def test_double_stop_is_safe(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()   # Should not raise
```

### 15.4 Unit Test: MassiveDataSource (with mocks)

```python
# backend/tests/market/test_massive.py
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def make_snapshot(ticker: str, price: float, timestamp_ms: int = 1707580800000) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=999.0)
        source._client = MagicMock()   # Skip real RESTClient construction

        snapshots = [
            make_snapshot("AAPL", 190.50),
            make_snapshot("GOOGL", 175.25),
        ]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            source._tickers = ["AAPL", "GOOGL"]
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_is_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=999.0)
        source._client = MagicMock()
        source._tickers = ["AAPL", "BAD"]

        bad_snap = MagicMock()
        bad_snap.ticker = "BAD"
        bad_snap.last_trade = None   # Causes AttributeError in _poll_once

        snapshots = [make_snapshot("AAPL", 190.50), bad_snap]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()   # Must not raise

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash_poller(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=999.0)
        source._client = MagicMock()
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()   # Must not raise

    async def test_remove_ticker_clears_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=999.0)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None
        assert cache.get("GOOGL") is not None   # Unaffected
```

### 15.5 Test: Factory selection

```python
# backend/tests/market/test_factory.py
import os
from unittest.mock import patch
import pytest
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource
from app.market.massive_client import MassiveDataSource


class TestFactory:

    def test_no_api_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": ""}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_api_key_returns_massive(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "abc123"}):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)

    def test_whitespace_only_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "   "}):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)
```

---

## 16. Configuration Reference

All parameters and where to change them:

| Parameter | File | Default | Notes |
|-----------|------|---------|-------|
| `MASSIVE_API_KEY` | `.env` | `""` | Empty = use simulator; any non-empty = use Massive |
| Simulator tick interval | `simulator.py` `SimulatorDataSource.__init__` | `0.5s` | Time between GBM steps |
| Massive poll interval | `massive_client.py` `MassiveDataSource.__init__` | `15.0s` | Safe for free tier (5 req/min) |
| GBM `dt` | `simulator.py` `GBMSimulator.DEFAULT_DT` | `~8.48e-8` | 500ms as fraction of trading year |
| Shock event probability | `simulator.py` `GBMSimulator.__init__` | `0.001` | ~1 event/50s across 10 tickers |
| SSE push interval | `stream.py` `_generate_events` | `0.5s` | Wake interval (sends only on cache change) |
| SSE retry directive | `stream.py` `_generate_events` | `1000ms` | Browser reconnection delay |
| Seed prices | `seed_prices.py` `SEED_PRICES` | Per ticker | Starting prices for simulator |
| Volatility params | `seed_prices.py` `TICKER_PARAMS` | Per ticker | `sigma`, `mu` per stock |
| Correlation groups | `seed_prices.py` `CORRELATION_GROUPS` | Sector-based | `tech`, `finance` groups |

### Timing constants summary

| Constant | Value | Derived from |
|----------|-------|-------------|
| `TRADING_SECONDS_PER_YEAR` | 5,896,800 | 252 days × 6.5 h × 3600 s |
| `DEFAULT_DT` | ~8.48e-8 | 0.5 / 5,896,800 |
| Simulator tick | 500ms | `update_interval` default |
| Massive free tier poll | 15s | `poll_interval` default |
| Portfolio snapshot | 30s | Background task in portfolio module |
| SSE retry | 1000ms | `retry:` SSE directive |

---

## Quick Start for Downstream Agents

To use the market data subsystem in a new route or module:

```python
# 1. Import only from the top-level package
from app.market import PriceCache, MarketDataSource

# 2. Inject via FastAPI dependencies (defined in main.py)
from app.main import get_price_cache, get_market_source

# 3. Read prices (sync, always safe from any coroutine)
price    = price_cache.get_price("AAPL")     # float | None
update   = price_cache.get("AAPL")           # PriceUpdate | None
all_px   = price_cache.get_all()             # dict[str, PriceUpdate]

# 4. Track new tickers (async)
await source.add_ticker("PYPL")
await source.remove_ticker("GOOGL")

# 5. Check what's tracked
tickers = source.get_tickers()               # list[str]

# 6. Serialize for API response (uses PriceUpdate.to_dict())
data = update.to_dict()   # dict with ticker, price, previous_price, etc.
```
