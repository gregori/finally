# FinAlly — AI Trading Workstation

An AI-powered trading workstation with live-streaming market data, a simulated portfolio, and an LLM assistant that can analyze positions and execute trades on your behalf. Bloomberg terminal aesthetic, built entirely by coding agents as a capstone for an agentic AI course.

## Features

- **Live price streaming** — 10 default tickers with green/red flash animations and sparkline mini-charts
- **Simulated portfolio** — $10,000 in virtual cash, instant market orders, no fees
- **Portfolio visualizations** — treemap heatmap (sized by weight, colored by P&L), P&L chart, positions table
- **AI chat assistant** — ask questions, get analysis, and have the AI execute trades and manage your watchlist via natural language
- **Real market data** — optional Polygon.io integration via Massive API; simulator used by default

## Quick Start

```bash
cp .env.example .env
# Add your OPENCODE_API_KEY to .env
./scripts/start_mac.sh   # or start_windows.ps1 on Windows
```

Open [http://localhost:8000](http://localhost:8000).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENCODE_API_KEY` | Yes | OpenCode API key for LLM chat. Get one at [opencode.ai](https://opencode.ai) |
| `MASSIVE_API_KEY` | No | Real market data via Polygon.io wrapper. Omit to use the built-in simulator |
| `LLM_MOCK` | No | Set `true` for deterministic mock LLM responses (testing/CI) |

## Architecture

Single Docker container on port 8000:

- **Frontend**: Next.js (TypeScript), static export served by FastAPI
- **Backend**: FastAPI (Python/uv), SQLite database
- **Real-time**: Server-Sent Events for price streaming
- **AI**: LiteLLM → OpenCode → Cerebras (`opencode/deepseek-v4-flash-free`)

## Development

```bash
# Build and run
docker build -t finally .
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally

# Stop
./scripts/stop_mac.sh
```

## Testing

E2E tests use Playwright against a fresh ephemeral container (no API key needed — runs with `LLM_MOCK=true`):

```bash
cd test && docker compose -f docker-compose.test.yml up --abort-on-container-exit
```
