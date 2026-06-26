"""Tests for the SSE streaming endpoint (stream.py)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import APIRouter

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _make_request(disconnects_after: int = 0) -> MagicMock:
    """Return a mock Starlette Request that disconnects after N is_disconnected() calls."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"

    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > disconnects_after

    request.is_disconnected = is_disconnected
    return request


class TestCreateStreamRouter:
    """Tests for the create_stream_router factory function."""

    def test_returns_api_router(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        assert isinstance(router, APIRouter)

    def test_router_has_correct_prefix(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        assert router.prefix == "/api/stream"

    def test_router_has_prices_route(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        paths = [route.path for route in router.routes]
        assert "/prices" in paths

    def test_each_call_returns_independent_router(self):
        """Each call must produce a distinct router — no shared global state."""
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)

        assert router1 is not router2

    def test_each_router_has_exactly_one_route(self):
        """Router must not accumulate duplicate routes across factory calls."""
        cache = PriceCache()
        for _ in range(3):
            router = create_stream_router(cache)

        assert len(router.routes) == 1

    def test_prices_route_responds_to_get(self):
        """The /prices route must be registered as a GET endpoint."""
        cache = PriceCache()
        router = create_stream_router(cache)

        route = next(r for r in router.routes if r.path == "/prices")
        assert "GET" in route.methods


class TestGenerateEventsRetryDirective:
    """Tests for the retry directive yielded at stream start."""

    @pytest.mark.asyncio
    async def test_first_event_is_retry_directive(self):
        """First yielded value must be the SSE retry directive."""
        cache = PriceCache()
        request = _make_request(disconnects_after=0)  # Disconnect immediately

        gen = _generate_events(cache, request)
        first = await gen.__anext__()
        assert first == "retry: 1000\n\n"

    @pytest.mark.asyncio
    async def test_retry_directive_before_disconnect(self):
        """Retry directive is sent even when client disconnects right away."""
        cache = PriceCache()
        request = _make_request(disconnects_after=0)

        events = []
        async for event in _generate_events(cache, request):
            events.append(event)

        assert events == ["retry: 1000\n\n"]


class TestGenerateEventsDataPayload:
    """Tests for price data events yielded by _generate_events."""

    @pytest.mark.asyncio
    async def test_yields_price_data_when_cache_populated(self):
        """A data event is emitted when the cache contains prices."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)

        request = _make_request(disconnects_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        assert len(events) == 2
        assert events[0] == "retry: 1000\n\n"
        assert events[1].startswith("data: ")

    @pytest.mark.asyncio
    async def test_payload_contains_correct_price(self):
        """Data event payload has the correct price for each ticker."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)

        request = _make_request(disconnects_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_raw = events[1][len("data: "):]
        payload = json.loads(data_raw.strip())

        assert "AAPL" in payload
        assert payload["AAPL"]["price"] == 190.5
        assert payload["AAPL"]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_payload_has_all_required_fields(self):
        """Each ticker entry contains all fields required by the SSE protocol contract."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = _make_request(disconnects_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_event = next(e for e in events if e.startswith("data: "))
        payload = json.loads(data_event[len("data: "):].strip())
        aapl = payload["AAPL"]

        for field in ("ticker", "price", "previous_price", "timestamp", "change", "change_percent", "direction"):
            assert field in aapl, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_all_cache_tickers_in_payload(self):
        """All tickers present in the cache appear in the data event."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("MSFT", 420.00)

        request = _make_request(disconnects_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_event = next(e for e in events if e.startswith("data: "))
        payload = json.loads(data_event[len("data: "):].strip())

        assert set(payload.keys()) == {"AAPL", "GOOGL", "MSFT"}

    @pytest.mark.asyncio
    async def test_no_data_event_when_cache_empty(self):
        """No data event is emitted when the cache is empty."""
        cache = PriceCache()

        request = _make_request(disconnects_after=2)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        assert events == ["retry: 1000\n\n"]

    @pytest.mark.asyncio
    async def test_data_events_are_sse_formatted(self):
        """Data events follow the SSE wire format: 'data: ...\\n\\n'."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = _make_request(disconnects_after=1)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_event = next(e for e in events if e.startswith("data: "))
        assert data_event.endswith("\n\n")


class TestGenerateEventsVersionTracking:
    """Tests for version-based change detection (no duplicate sends)."""

    @pytest.mark.asyncio
    async def test_no_duplicate_events_when_version_unchanged(self):
        """If the cache version does not change, no additional data event is sent."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        # 3 disconnect checks → 2 loop iterations after the first data send
        request = _make_request(disconnects_after=3)

        events = []
        async for event in _generate_events(cache, request, interval=0.01):
            events.append(event)

        data_events = [e for e in events if e.startswith("data: ")]
        assert len(data_events) == 1

    @pytest.mark.asyncio
    async def test_new_event_sent_after_cache_update(self):
        """A second data event is emitted when the cache is updated mid-stream."""
        cache = PriceCache()

        injected = False
        call_count = 0

        async def is_disconnected() -> bool:
            nonlocal call_count, injected
            call_count += 1
            if call_count == 2 and not injected:
                cache.update("AAPL", 200.00)
                injected = True
            return call_count > 4

        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.is_disconnected = is_disconnected

        events = []
        async for event in _generate_events(cache, request, interval=0.001):
            events.append(event)

        data_events = [e for e in events if e.startswith("data: ")]
        assert len(data_events) >= 1

        last_payload = json.loads(data_events[-1][len("data: "):].strip())
        assert "AAPL" in last_payload
        assert last_payload["AAPL"]["price"] == 200.0


class TestGenerateEventsDisconnect:
    """Tests for client disconnection handling."""

    @pytest.mark.asyncio
    async def test_stops_immediately_on_disconnect(self):
        """Generator terminates as soon as is_disconnected() returns True."""
        cache = PriceCache()
        request = _make_request(disconnects_after=0)

        events = []
        async for event in _generate_events(cache, request):
            events.append(event)

        # Only the retry directive before disconnect check triggers
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_handles_missing_client_attribute(self):
        """Generator runs correctly when request.client is None (Unix socket etc.)."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)

        request = MagicMock()
        request.client = None
        request.is_disconnected = AsyncMock(return_value=True)

        gen = _generate_events(cache, request)
        first = await gen.__anext__()
        assert first == "retry: 1000\n\n"

        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    @pytest.mark.asyncio
    async def test_generator_is_async_iterable(self):
        """_generate_events must be usable in an async for loop."""
        cache = PriceCache()
        request = _make_request(disconnects_after=0)

        count = 0
        async for _ in _generate_events(cache, request):
            count += 1

        assert count >= 0  # Just verify no exception during iteration
