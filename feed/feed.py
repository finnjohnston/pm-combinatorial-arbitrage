import asyncio
import logging
import time

from book.public_book import PublicMarketBook
from graph.graph import StateGraph

from .client import FeedClient
from .normaliser import price_to_ticks, qty_to_units

logger = logging.getLogger("feed")

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_STABLE_S = 60.0


class KalshiFeed:

    def __init__(self, books: dict[str, PublicMarketBook], graph: StateGraph, event_queue: asyncio.Queue, resolution_queue: asyncio.Queue, key_id: str, private_key_path: str,
                 cold_events: dict[str, list[str]] | None = None) -> None:
        self._books = books
        self._graph = graph
        self._event_queue = event_queue
        self._resolution_queue = resolution_queue
        self._client = FeedClient(key_id, private_key_path)
        self._hot_events: dict[str, list[str]] = {
            event_id: sorted(event_state.tickers)
            for event_id, event_state in graph.events.items()
            if event_state.tickers and all(t in books for t in event_state.tickers)
        }
        self._cold_events: dict[str, list[str]] = {eid: list(t) for eid, t in (cold_events or {}).items()}
        self._snapshotted: set[str] = set()

    @property
    def hot_event_count(self) -> int:
        return len(self._hot_events)

    @property
    def hot_ticker_count(self) -> int:
        return sum(len(tickers) for tickers in self._hot_events.values())

    @property
    def cold_event_count(self) -> int:
        return len(self._cold_events)

    @property
    def cold_ticker_count(self) -> int:
        return sum(len(tickers) for tickers in self._cold_events.values())

    async def run(self) -> None:
        backoff = _BACKOFF_BASE
        while True:
            receive_task = None
            connected_at = None
            try:
                await self._client.connect()
                connected_at = time.monotonic()
                self._snapshotted.clear()
                logger.info(
                    "CONNECT  hot=%d(%d tickers)  cold=%d(%d tickers)",
                    self.hot_event_count, self.hot_ticker_count,
                    self.cold_event_count, self.cold_ticker_count,
                )
                receive_task = asyncio.create_task(self._receive_loop())
                hot_tickers = [t for tickers in self._hot_events.values() for t in tickers]
                if hot_tickers:
                    await self._client.subscribe(hot_tickers)
                logger.info("READY")
                await receive_task
            except asyncio.CancelledError:
                if receive_task is not None and not receive_task.done():
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                raise
            except Exception as exc:
                if receive_task is not None and not receive_task.done():
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                # only treat the connection as healthy (resetting backoff) if it
                # survived for a while — a flapping link keeps backing off
                if connected_at is not None and time.monotonic() - connected_at >= _BACKOFF_STABLE_S:
                    backoff = _BACKOFF_BASE
                logger.warning("DISCONNECT  reason=%s  retry=%.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _receive_loop(self) -> None:
        while True:
            msg = await self._client.receive()
            msg_type = msg.get("type")
            if msg_type == "orderbook_snapshot":
                self._handle_snapshot(msg["msg"])
            elif msg_type == "orderbook_delta":
                self._handle_delta(msg["msg"])
            elif msg_type == "market_lifecycle_v2":
                self._handle_lifecycle(msg["msg"])
            elif msg_type == "subscribed":
                await self._handle_subscribed(msg)
            elif msg_type == "error":
                self._client.record_error(msg)
                logger.error("ERROR  msg=%s", msg.get("msg"))

    def _handle_snapshot(self, msg: dict) -> None:
        ticker = msg["market_ticker"]
        book = self._books.get(ticker)
        if book is None:
            return

        yes_levels = {price_to_ticks(p): qty_to_units(q) for p, q in msg.get("yes_dollars_fp", [])}
        no_levels  = {price_to_ticks(p): qty_to_units(q) for p, q in msg.get("no_dollars_fp",  [])}
        book.load_snapshot(yes_levels, no_levels)
        self._snapshotted.add(ticker)
        self._notify(ticker)

    def _handle_delta(self, msg: dict) -> None:
        ticker = msg["market_ticker"]
        book = self._books.get(ticker)
        if book is None:
            return
        if "price_dollars" not in msg:
            logger.debug("Skipping non-standard delta for %s: %s", ticker, msg)
            return

        price = price_to_ticks(msg["price_dollars"])
        delta = qty_to_units(msg["delta_fp"])
        side  = msg["side"]
        book.apply_delta(side, price, delta)
        self._notify(ticker)

    def _handle_lifecycle(self, msg: dict) -> None:
        if msg.get("status") != "finalized":
            return
        if msg.get("result") != "yes":
            return
        ticker = msg.get("market_ticker")
        if ticker:
            self._resolution_queue.put_nowait(ticker)

    async def _handle_subscribed(self, msg: dict) -> None:
        await self._client.record_subscribed(msg)

    async def remove_event(self, event_id: str, tickers: list[str]) -> None:
        was_hot = self._hot_events.pop(event_id, None) is not None
        self._cold_events.pop(event_id, None)
        for ticker in tickers:
            self._snapshotted.discard(ticker)
        if was_hot:
            await self._client.unsubscribe(tickers)

    async def subscribe_new(self, event_id: str, tickers: list[str], hot: bool = True) -> None:
        if hot:
            self._hot_events[event_id] = list(tickers)
            try:
                await self._client.subscribe(tickers)
            except Exception:
                pass
        else:
            self._cold_events[event_id] = list(tickers)

    async def subscribe_orderbook(self, event_id: str, tickers: list[str]) -> None:
        self._cold_events.pop(event_id, None)
        self._hot_events[event_id] = list(tickers)
        try:
            await self._client.subscribe(tickers)
        except Exception:
            pass

    async def add_tickers(self, event_id: str, tickers: list[str]) -> None:
        """Subscribe additional tickers on an already-hot event (markets Kalshi
        added to the event after we first saw it)."""
        existing = self._hot_events.setdefault(event_id, [])
        new = [t for t in tickers if t not in existing]
        if not new:
            return
        existing.extend(new)
        try:
            await self._client.subscribe(new)
        except Exception:
            pass

    def _notify(self, ticker: str) -> None:
        event_id = self._graph.ticker_to_event.get(ticker)
        if event_id is None:
            return
        event_state = self._graph.events.get(event_id)
        if event_state is None:
            return
        if not all(t in self._snapshotted for t in event_state.tickers):
            return
        self._event_queue.put_nowait(event_id)
