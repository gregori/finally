# Market Data Interface

This document defines the unified Python interface that all market data providers must implement. Both the Massive API client and the built-in simulator conform to this interface. All downstream code (SSE streaming, portfolio snapshots, watchlist management) depends only on this interface — never on a concrete provider.

---

## Design Goals

- **Swap-in transparency**: switching from simulator to Massive requires only changing the provider selected at startup; zero application code changes.
- **Async-first**: the provider runs a background task; the cache is read synchronously by SSE handlers.
- **No polling inside SSE**: SSE handlers read from a shared in-memory cache that the background task updates. Handlers never call the provider directly.
- **Ticker lifecycle**: tickers can be added or removed at runtime without restarting the provider.

---

## Core Data Types

```python
# backend/market/types.py

from dataclasses import dataclass, field
import time

@dataclass
class PriceUpdate:
    ticker: str
    price: float
    prev_price: float           # previous tick price (simulator) or previous session close (Massive)
    timestamp: float            # Unix timestamp (seconds)
    change: float               # absolute change from prev_price
    change_pct: float           # percentage change from prev_price

    @classmethod
    def from_prices(cls, ticker: str, price: float, prev_price: float) -> "PriceUpdate":
        change = price - prev_price
        change_pct = (change / prev_price * 100) if prev_price else 0.0
        return cls(
            ticker=ticker,
            price=price,
            prev_price=prev_price,
            timestamp=time.time(),
            change=change,
            change_pct=change_pct,
        )
```

---

## Abstract Provider Interface

```python
# backend/market/base.py

from abc import ABC, abstractmethod
from typing import Optional
from .types import PriceUpdate

class MarketDataProvider(ABC):
    """Abstract base class for all market data providers."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """
        Start the provider with an initial set of tickers.
        Called once at application startup. Must not block — launches
        a background task internally.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources. Called on shutdown."""

    @abstractmethod
    def get_price(self, ticker: str) -> Optional[PriceUpdate]:
        """
        Return the latest price update for a ticker, or None if not yet available.
        Synchronous — safe to call from SSE handlers.
        """

    @abstractmethod
    def get_all_prices(self) -> dict[str, PriceUpdate]:
        """
        Return the latest price update for every tracked ticker.
        Synchronous — safe to call from SSE handlers.
        Returns a snapshot copy of the cache (not a live reference).
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """
        Add a ticker to the tracked set. The ticker appears in get_all_prices()
        on the next poll/tick cycle after this call.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the tracked set."""

    @abstractmethod
    async def validate_ticker(self, ticker: str) -> bool:
        """
        Return True if the ticker is a known, tradeable symbol.
        Used by the watchlist POST endpoint before persisting to the database.
        Simulator: checks against its known ticker set.
        Massive: calls /v3/reference/tickers or uses snapshot presence.
        """
```

---

## Shared Price Cache

Both providers use the same `PriceCache` class. The background task writes to it; SSE handlers and API routes read from it. The lock ensures writes are atomic across threads/coroutines.

```python
# backend/market/cache.py

import asyncio
from typing import Optional
from .types import PriceUpdate

class PriceCache:
    """Thread-safe in-memory cache of latest prices per ticker."""

    def __init__(self) -> None:
        self._data: dict[str, PriceUpdate] = {}
        self._lock = asyncio.Lock()

    async def set(self, update: PriceUpdate) -> None:
        async with self._lock:
            self._data[update.ticker] = update

    async def set_many(self, updates: list[PriceUpdate]) -> None:
        async with self._lock:
            for update in updates:
                self._data[update.ticker] = update

    def get(self, ticker: str) -> Optional[PriceUpdate]:
        return self._data.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        return dict(self._data)  # snapshot copy

    async def remove(self, ticker: str) -> None:
        async with self._lock:
            self._data.pop(ticker, None)
```

---

## Provider Factory

The factory reads `MASSIVE_API_KEY` from the environment and selects the appropriate provider. All application code calls `get_provider()` — it never instantiates a provider directly.

```python
# backend/market/factory.py

import os
from .base import MarketDataProvider
from .simulator import MarketSimulator
from .massive import MassiveMarketClient

def get_provider() -> MarketDataProvider:
    """
    Return the active market data provider based on environment variables.
    Simulator is used when MASSIVE_API_KEY is absent or empty.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveMarketClient(api_key=api_key)
    return MarketSimulator()
```

---

## Application Lifecycle Integration

The provider is created once at startup and stored as application state. FastAPI's lifespan context manager manages the lifecycle.

```python
# backend/main.py  (abbreviated)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from .market.factory import get_provider
from .db import load_watchlist

provider = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global provider
    tickers = await load_watchlist()          # reads initial tickers from SQLite
    provider = get_provider()
    await provider.start(tickers)
    yield
    await provider.stop()

app = FastAPI(lifespan=lifespan)
```

---

## SSE Streaming Integration

The SSE endpoint reads from the provider's cache every 500ms (simulator cadence) or every poll interval (Massive). It never calls the provider's fetch methods directly.

```python
# backend/routes/stream.py  (abbreviated)

import asyncio
import json
from fastapi import Request
from fastapi.responses import StreamingResponse
from ..main import provider

async def price_stream(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            prices = provider.get_all_prices()
            for ticker, update in prices.items():
                payload = {
                    "ticker": update.ticker,
                    "price": update.price,
                    "prev_price": update.prev_price,
                    "change": update.change,
                    "change_pct": update.change_pct,
                    "timestamp": update.timestamp,
                }
                yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.5)      # push cadence; Massive clients may sleep longer

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

---

## Watchlist Route Integration

When the user adds a ticker via `POST /api/watchlist`, the route validates it through the provider, persists it to the database, then notifies the provider to begin tracking it.

```python
# backend/routes/watchlist.py  (abbreviated)

from fastapi import HTTPException
from ..main import provider
from ..db import add_to_watchlist, ticker_in_watchlist

async def add_ticker(body: AddTickerRequest):
    ticker = body.ticker.upper().strip()

    # Duplicate check (fast, no external call)
    if await ticker_in_watchlist(ticker):
        raise HTTPException(409, detail={"error": "Ticker already in watchlist", "code": "DUPLICATE_TICKER"})

    # Symbol validation (may call Massive API or check simulator set)
    if not await provider.validate_ticker(ticker):
        raise HTTPException(400, detail={"error": "Unknown ticker symbol", "code": "UNKNOWN_TICKER"})

    await add_to_watchlist(ticker)
    await provider.add_ticker(ticker)       # starts tracking in next tick/poll
    return {"success": True, "data": {"ticker": ticker}}
```

---

## Provider Selection Summary

| Condition | Provider used |
|---|---|
| `MASSIVE_API_KEY` unset or empty | `MarketSimulator` |
| `MASSIVE_API_KEY` set and non-empty | `MassiveMarketClient` |

No other code should check `MASSIVE_API_KEY`. The factory is the single decision point.

---

## Module Layout

```
backend/
└── market/
    ├── __init__.py
    ├── types.py          # PriceUpdate dataclass
    ├── cache.py          # PriceCache
    ├── base.py           # MarketDataProvider ABC
    ├── factory.py        # get_provider()
    ├── simulator.py      # MarketSimulator (see MARKET_SIMULATOR.md)
    └── massive.py        # MassiveMarketClient (see MASSIVE_API.md)
```
