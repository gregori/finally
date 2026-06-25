# Massive API Reference (formerly Polygon.io)

Massive.com rebranded from Polygon.io on October 30, 2025. All existing `api.polygon.io` integrations remain compatible — the base URL has not changed. This document covers only the endpoints needed by FinAlly: fetching current prices for a set of watched tickers.

---

## Authentication

Every request requires an API key. Two options:

**Query parameter (simplest for direct HTTP calls):**
```
GET https://api.polygon.io/v2/snapshot/...?apiKey=YOUR_KEY
```

**Authorization header (preferred for production):**
```
Authorization: Bearer YOUR_KEY
```

The Python client handles this automatically via the constructor.

---

## Python Client

Massive ships an official Python client (renamed from `polygon-api-client`).

```bash
pip install -U massive
```

Requires Python 3.9+.

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_KEY")
```

The client handles pagination, retries, and deserialization. All returned objects support attribute access (e.g., `result.ticker`, `result.day.c`).

---

## Endpoints

### 1. Full Market Snapshot — multiple tickers (recommended for FinAlly)

Fetches current snapshot data for a comma-separated list of tickers in one request. This is the primary endpoint for polling the watched tickers list.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `tickers` | string | No | Comma-separated ticker list (e.g. `AAPL,GOOGL,MSFT`). Omit to get all ~10,000+ active symbols. |
| `include_otc` | boolean | No | Include OTC securities. Default: `false`. |
| `apiKey` | string | Yes (or header) | Your API key. |

**Response structure:**
```json
{
  "status": "OK",
  "count": 3,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.23,
      "todaysChangePerc": 0.65,
      "updated": 1718304000000000000,
      "day": {
        "o": 189.50,
        "h": 191.20,
        "l": 188.90,
        "c": 190.73,
        "v": 52431000,
        "vw": 190.12
      },
      "lastTrade": {
        "p": 190.73,
        "s": 100,
        "t": 1718304000000000000
      },
      "lastQuote": {
        "P": 190.74,
        "S": 2,
        "p": 190.73,
        "s": 8
      },
      "min": {
        "o": 190.60,
        "h": 190.80,
        "l": 190.55,
        "c": 190.73,
        "v": 12300,
        "vw": 190.68
      },
      "prevDay": {
        "o": 188.20,
        "h": 189.70,
        "l": 187.50,
        "c": 189.50,
        "v": 48200000
      }
    }
  ]
}
```

**Key fields per ticker:**

| Field | Description |
|---|---|
| `ticker` | Symbol |
| `day.c` | Current day's closing/latest price |
| `day.o` / `day.h` / `day.l` | Day open, high, low |
| `day.v` | Day volume |
| `prevDay.c` | Previous day close (for % change calculation) |
| `lastTrade.p` | Most recent trade price (most current) |
| `todaysChange` | Absolute change from previous close |
| `todaysChangePerc` | Percentage change from previous close |
| `updated` | Nanosecond timestamp of last update |

**Python example using the `massive` client:**
```python
from massive import RESTClient

client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])

tickers = ["AAPL", "GOOGL", "MSFT", "TSLA"]
snapshots = client.get_snapshot_all("us", tickers)

for snap in snapshots:
    price = snap.last_trade.price if snap.last_trade else snap.day.close
    prev_close = snap.prev_day.close
    print(f"{snap.ticker}: ${price:.2f}  ({snap.todays_change_perc:+.2f}%)")
```

**Python example using raw `httpx`/`requests` (no client library):**
```python
import httpx

BASE_URL = "https://api.polygon.io"

def fetch_snapshots(api_key: str, tickers: list[str]) -> dict:
    url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {
        "tickers": ",".join(tickers),
        "apiKey": api_key,
    }
    resp = httpx.get(url, params=params, timeout=10.0)
    resp.raise_for_status()
    return resp.json()

data = fetch_snapshots(os.environ["MASSIVE_API_KEY"], ["AAPL", "GOOGL"])
for snap in data["tickers"]:
    last_price = snap["lastTrade"]["p"] if snap.get("lastTrade") else snap["day"]["c"]
    print(f"{snap['ticker']}: ${last_price}")
```

---

### 2. Single Ticker Snapshot

Useful for fetching one ticker on demand (e.g., when the user adds a new ticker to their watchlist).

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}
```

**Path parameters:**

| Parameter | Description |
|---|---|
| `stocksTicker` | Case-sensitive ticker symbol (e.g., `AAPL`) |

**Sample response:**
```json
{
  "status": "OK",
  "request_id": "657e430f1ae768891f018e08e03598d8",
  "ticker": {
    "ticker": "AAPL",
    "day": { "c": 190.73, "h": 191.20, "l": 188.90, "o": 189.50, "v": 52431000, "vw": 190.12 },
    "min": { "c": 190.73, "h": 190.80, "l": 190.55, "o": 190.60 },
    "prevDay": { "c": 189.50, "h": 189.70, "l": 187.50, "o": 188.20 },
    "lastTrade": { "p": 190.73, "s": 100, "t": 1718304000000000000 },
    "todaysChange": 1.23,
    "todaysChangePerc": 0.65,
    "updated": 1718304000000000000
  }
}
```

**Python example:**
```python
def fetch_single_snapshot(api_key: str, ticker: str) -> dict | None:
    url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
    resp = httpx.get(url, params={"apiKey": api_key}, timeout=10.0)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    return data.get("ticker")
```

---

### 3. Unified Snapshot (v3) — alternative for up to 250 tickers

A newer endpoint that supports multiple asset classes in one call. Accepts up to 250 symbols via `ticker.any_of`.

```
GET /v3/snapshot
```

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `ticker.any_of` | string | Comma-separated list, max 250 symbols |
| `type` | string | Filter by asset class (`stocks`, `options`, `fx`, `crypto`, `indices`) |
| `limit` | integer | Max results per page (max 250, default 10) |

**Plan requirement:** Stocks Starter and above (no free tier access).

**Note:** For FinAlly we default to the v2 endpoint (`/v2/snapshot/locale/us/markets/stocks/tickers`) because it is available on the free tier and supports our 10-ticker watchlist easily.

---

## Ticker Validation

To check whether a ticker symbol exists before adding it to the watchlist:

```
GET /v3/reference/tickers/{ticker}
```

```python
def is_valid_ticker(api_key: str, ticker: str) -> bool:
    url = f"{BASE_URL}/v3/reference/tickers/{ticker}"
    resp = httpx.get(url, params={"apiKey": api_key}, timeout=10.0)
    if resp.status_code == 200:
        return resp.json().get("status") == "OK"
    return False
```

Alternatively, use the snapshot endpoint: a 200 response with `ticker` data confirms the symbol exists; a `"NOT_FOUND"` status or absent `ticker` key means it does not.

---

## Rate Limits and Tiers

| Plan | Rate limit | Data recency |
|---|---|---|
| Free | 5 requests/minute | 15-minute delayed |
| Starter / Developer | Unlimited | 15-minute delayed |
| Advanced / Business | Unlimited (< 100 req/s recommended) | Real-time |

**FinAlly polling strategy:**
- Free tier: poll every 15 seconds (4 req/min — safely under the 5 req/min cap)
- Paid tiers: poll every 2–15 seconds depending on user preference
- A single poll call fetches all watched tickers in one request (the v2 endpoint accepts an arbitrary comma-separated list)

---

## Price Extraction Logic

The `lastTrade.p` field is the most current price during market hours. During pre/post-market or when trades are absent, fall back to `day.c`. The previous close is always in `prevDay.c`.

```python
def extract_price(snap: dict) -> tuple[float, float]:
    """Returns (current_price, prev_close)."""
    last_trade = snap.get("lastTrade") or {}
    current = last_trade.get("p") or snap["day"]["c"]
    prev_close = snap["prevDay"]["c"]
    return float(current), float(prev_close)
```

---

## Error Handling

| HTTP Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad request (malformed parameters) |
| 403 | Invalid or missing API key |
| 404 | Ticker not found (single-ticker endpoint) |
| 429 | Rate limit exceeded |
| 500 | Massive server error |

```python
import httpx

try:
    resp = httpx.get(url, params=params, timeout=10.0)
    resp.raise_for_status()
except httpx.HTTPStatusError as e:
    if e.response.status_code == 429:
        # back off and retry
        pass
    elif e.response.status_code == 403:
        raise RuntimeError("Invalid MASSIVE_API_KEY") from e
    else:
        raise
except httpx.TimeoutException:
    # log and skip this poll cycle
    pass
```

---

## Market Hours

The snapshot data is cleared at **3:30 AM EST** and begins repopulating from **4:00 AM EST** as exchanges open pre-market. During closed hours, `day.*` fields reflect the most recent session; `prevDay.*` holds the last completed session.

FinAlly does not restrict trading to market hours — the simulator and the displayed prices work 24/7, but real Massive data will show stale prices outside market hours. This is acceptable for the course project scope.
