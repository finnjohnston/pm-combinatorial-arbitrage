import asyncio
import random
import pytest

from book.public_book import PublicMarketBook
from execution.config import ExecutionConfig
from graph.graph import StateGraph
from optimizer.optimizer import Optimizer
from portfolio.ledger import Ledger
from risk.manager import RiskManager
from engine import Engine


class _StubFeed:
    def __init__(self):
        self.subscribed_orderbook: list[list[str]] = []
        self.removed: list[str] = []
        self._hot_events: dict[str, list[str]] = {}
        self._cold_events: dict[str, list[str]] = {}

    async def subscribe_new(self, event_id: str, tickers: list[str], hot: bool = True) -> None:
        if hot:
            self._hot_events[event_id] = list(tickers)
        else:
            self._cold_events[event_id] = list(tickers)

    async def subscribe_orderbook(self, event_id: str, tickers: list[str]) -> None:
        self.subscribed_orderbook.append(list(tickers))
        self._cold_events.pop(event_id, None)
        self._hot_events[event_id] = list(tickers)

    async def remove_event(self, event_id: str, tickers: list[str]) -> None:
        self._hot_events.pop(event_id, None)
        self._cold_events.pop(event_id, None)
        self.removed.extend(tickers)

    async def add_tickers(self, event_id: str, tickers: list[str]) -> None:
        existing = self._hot_events.setdefault(event_id, [])
        existing.extend(t for t in tickers if t not in existing)

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


class _StubRestClient:
    def __init__(self, markets_by_event: dict[str, list[dict]] | None = None):
        self.markets_by_event = markets_by_event or {}

    def get(self, path: str, params=None) -> dict:
        if path == "/markets":
            event_ticker = (params or {}).get("event_ticker")
            return {"markets": self.markets_by_event.get(event_ticker, []), "cursor": None}
        return {"events": [], "cursor": None}


def make_arb_books() -> dict[str, PublicMarketBook]:
    """Two-outcome event where buy arb exists: YES asks sum to < 1000."""
    books = {
        "MKT-A": PublicMarketBook(),
        "MKT-B": PublicMarketBook(),
    }
    books["MKT-A"].no_buys.set_real_qty(545, 10000)
    books["MKT-B"].no_buys.set_real_qty(545, 10000)
    return books


def make_system(
    books: dict[str, PublicMarketBook],
    initial_capital: int = 10_000_000,
    cold_events: dict[str, list[str]] | None = None,
    graph: StateGraph | None = None,
    feed: "_StubFeed | None" = None,
    rest_client: "_StubRestClient | None" = None,
    participation_rate: float = 1.0,
):
    if graph is None:
        graph = StateGraph()
        graph.add_event("EVT-1", list(books.keys()))

    config = ExecutionConfig(min_latency_ms=0, max_latency_ms=0, participation_rate=participation_rate)
    optimizer = Optimizer()
    ledger = Ledger(initial_capital=initial_capital, optimizer=optimizer)
    risk_manager = RiskManager(books=books, config=config, rng=random.Random(42))
    event_queue: asyncio.Queue = asyncio.Queue()
    resolution_queue: asyncio.Queue = asyncio.Queue()
    stub_feed = feed or _StubFeed()

    orch = Engine(
        books=books,
        graph=graph,
        optimizer=optimizer,
        ledger=ledger,
        event_queue=event_queue,
        resolution_queue=resolution_queue,
        config=config,
        risk_manager=risk_manager,
        feed=stub_feed,
        rest_client=rest_client or _StubRestClient(),
        rng=random.Random(42),
        cold_events=cold_events,
        persistence_delay_s=0.0,
    )
    return orch, optimizer, ledger, event_queue, resolution_queue, graph


async def test_arb_loop_detects_and_executes():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")

    # run one iteration then cancel
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # a position should have been opened
    assert ledger._positions.is_open("EVT-1")


async def test_no_arb_no_position():
    books = {
        "MKT-A": PublicMarketBook(),
        "MKT-B": PublicMarketBook(),
    }
    books["MKT-A"].no_buys.set_real_qty(450, 10000)
    books["MKT-B"].no_buys.set_real_qty(450, 10000)

    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)
    event_queue.put_nowait("EVT-1")

    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not ledger._positions.is_open("EVT-1")
    assert ledger.total_capital == 10_000_000


async def test_resolution_loop_closes_position():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")

    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)

    assert ledger._positions.is_open("EVT-1")

    resolution_queue.put_nowait("MKT-A")
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not ledger._positions.is_open("EVT-1")
    assert ledger.total_capital > 10_000_000


async def test_resolution_cleans_up_graph_and_books():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)

    resolution_queue.put_nowait("MKT-A")
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "EVT-1" not in graph.events
    assert "MKT-A" not in graph.ticker_to_event
    assert "MKT-B" not in graph.ticker_to_event
    assert "MKT-A" not in books
    assert "MKT-B" not in books


async def test_resolution_removes_settled_tickers_from_feed():
    books = make_arb_books()
    stub_feed = _StubFeed()
    stub_feed._hot_events["EVT-1"] = ["MKT-A", "MKT-B"]
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, feed=stub_feed)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)

    resolution_queue.put_nowait("MKT-A")
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert stub_feed._hot_events == {}
    assert sorted(stub_feed.removed) == ["MKT-A", "MKT-B"]


async def test_resolution_clears_optimizer_opportunity():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)

    resolution_queue.put_nowait("MKT-A")
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "EVT-1" not in optimizer.open_opportunities


async def test_resolution_credits_profit():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)

    resolution_queue.put_nowait("MKT-A")
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ledger.total_capital > 10_000_000


async def test_duplicate_events_deduped():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    # flood the queue with the same event
    for _ in range(10):
        event_queue.put_nowait("EVT-1")

    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # deduplication means only one position opened, not ten
    assert len(list(ledger._positions.open.values())) == 1


async def test_unknown_resolution_ticker_ignored():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    resolution_queue.put_nowait("MKT-UNKNOWN")

    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ledger.locked_capital == 0


# Cold event handling

def test_cold_event_in_queue_is_skipped():
    """An event whose tickers have no books is silently skipped without raising."""
    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-COLD-A", "MKT-COLD-B"])
    books: dict = {}  # no books for cold tickers
    cold_events = {"EVT-COLD": ["MKT-COLD-A", "MKT-COLD-B"]}
    orch, _, _, event_queue, _, _ = make_system(books, graph=graph, cold_events=cold_events)

    # Should not raise
    orch._evaluate_and_dispatch("EVT-COLD")


async def test_cleanup_removes_settled_cold_event_from_cold_events():
    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-A", "MKT-B"])
    books: dict = {}
    cold_events = {"EVT-COLD": ["MKT-A", "MKT-B"]}
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events)

    await orch._cleanup_event("EVT-COLD")

    assert "EVT-COLD" not in orch._cold_events
    assert "EVT-COLD" not in graph.events


async def test_cleanup_of_hot_event_does_not_affect_cold_events():
    books = make_arb_books()
    cold_events = {"EVT-COLD": ["MKT-X", "MKT-Y"]}
    orch, _, _, _, _, graph = make_system(books, cold_events=cold_events)

    await orch._cleanup_event("EVT-1")  # hot event

    assert "EVT-COLD" in orch._cold_events  # cold event unaffected


async def test_cleanup_removes_cold_event_from_feed():
    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-A", "MKT-B"])
    books: dict = {}
    cold_events = {"EVT-COLD": ["MKT-A", "MKT-B"]}
    stub_feed = _StubFeed()
    stub_feed._cold_events["EVT-COLD"] = ["MKT-A", "MKT-B"]
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events, feed=stub_feed)

    await orch._cleanup_event("EVT-COLD")

    assert stub_feed._cold_events == {}
    assert sorted(stub_feed.removed) == ["MKT-A", "MKT-B"]


async def test_cleanup_removes_hot_event_from_feed():
    books = make_arb_books()
    stub_feed = _StubFeed()
    stub_feed._hot_events["EVT-1"] = ["MKT-A", "MKT-B"]
    orch, _, _, _, _, _ = make_system(books, feed=stub_feed)

    await orch._cleanup_event("EVT-1")  # hot event

    assert stub_feed._hot_events == {}
    assert sorted(stub_feed.removed) == ["MKT-A", "MKT-B"]


async def test_discovery_promotes_cold_event_when_now_liquid():
    from feed.rest_client import DEPTH_MIN, VOLUME_24H_MIN

    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-A", "MKT-B"])
    books: dict = {}
    stub_feed = _StubFeed()
    cold_events = {"EVT-COLD": ["MKT-A", "MKT-B"]}
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events, feed=stub_feed)

    liquid_event = {
        "event_ticker": "EVT-COLD",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "MKT-A", "yes_ask_size_fp": str(DEPTH_MIN), "yes_bid_size_fp": "0", "volume_24h_fp": str(VOLUME_24H_MIN)},
            {"ticker": "MKT-B", "yes_ask_size_fp": str(DEPTH_MIN), "yes_bid_size_fp": "0", "volume_24h_fp": str(VOLUME_24H_MIN)},
        ],
    }

    await orch._process_discovered_events([liquid_event])

    assert "EVT-COLD" not in orch._cold_events
    assert "MKT-A" in books
    assert "MKT-B" in books
    assert stub_feed.subscribed_orderbook == [["MKT-A", "MKT-B"]]


async def test_discovery_skips_still_cold_event():
    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-A", "MKT-B"])
    books: dict = {}
    cold_events = {"EVT-COLD": ["MKT-A", "MKT-B"]}
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events)

    still_cold = {
        "event_ticker": "EVT-COLD",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "MKT-A", "yes_ask_size_fp": "0", "yes_bid_size_fp": "0", "volume_24h_fp": "0"},
            {"ticker": "MKT-B", "yes_ask_size_fp": "0", "yes_bid_size_fp": "0", "volume_24h_fp": "0"},
        ],
    }

    await orch._process_discovered_events([still_cold])

    assert "EVT-COLD" in orch._cold_events
    assert "MKT-A" not in books


async def test_discovery_adds_new_hot_event():
    from feed.rest_client import DEPTH_MIN, VOLUME_24H_MIN

    books: dict = {}
    stub_feed = _StubFeed()
    orch, _, _, _, _, graph = make_system(books, feed=stub_feed)

    new_hot = {
        "event_ticker": "EVT-NEW",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "MKT-X", "yes_ask_size_fp": str(DEPTH_MIN), "yes_bid_size_fp": "0", "volume_24h_fp": str(VOLUME_24H_MIN)},
            {"ticker": "MKT-Y", "yes_ask_size_fp": str(DEPTH_MIN), "yes_bid_size_fp": "0", "volume_24h_fp": str(VOLUME_24H_MIN)},
        ],
    }

    await orch._process_discovered_events([new_hot])

    assert "EVT-NEW" in graph.events
    assert "MKT-X" in books
    assert "MKT-Y" in books
    assert "EVT-NEW" not in orch._cold_events


async def test_discovery_removes_cold_event_absent_from_response():
    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-A", "MKT-B"])
    books: dict = {}
    cold_events = {"EVT-COLD": ["MKT-A", "MKT-B"]}
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events)

    # Event not returned by REST — it has closed
    await orch._process_discovered_events([])

    assert "EVT-COLD" not in orch._cold_events
    assert "EVT-COLD" not in graph.events


async def test_discovery_removes_only_absent_cold_events():
    graph = StateGraph()
    graph.add_event("EVT-GONE", ["MKT-G1", "MKT-G2"])
    graph.add_event("EVT-ALIVE", ["MKT-A1", "MKT-A2"])
    books: dict = {}
    cold_events = {
        "EVT-GONE": ["MKT-G1", "MKT-G2"],
        "EVT-ALIVE": ["MKT-A1", "MKT-A2"],
    }
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events)

    still_present = {
        "event_ticker": "EVT-ALIVE",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "MKT-A1", "yes_ask_size_fp": "0", "yes_bid_size_fp": "0", "volume_24h_fp": "0"},
            {"ticker": "MKT-A2", "yes_ask_size_fp": "0", "yes_bid_size_fp": "0", "volume_24h_fp": "0"},
        ],
    }

    await orch._process_discovered_events([still_present])

    assert "EVT-GONE" not in orch._cold_events
    assert "EVT-GONE" not in graph.events
    assert "EVT-ALIVE" in orch._cold_events
    assert "EVT-ALIVE" in graph.events


async def test_drift_check_aborts_when_price_moved_too_far():
    from optimizer.opportunity import OpportunityTier
    books = make_arb_books()  # MKT-A and MKT-B at 400t YES ask
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    # populate event state (normally done by _evaluate_and_dispatch)
    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

    # stale leg_prices 200t, current book 400t → drift=200 > max_drift=min(500,100)=100 → abort
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"MKT-A": 200, "MKT-B": 200},
        capital_required=40_000,
        profit=100_000,
    )
    await orch._execute_and_settle("EVT-1", "buy", tier)

    assert not ledger._positions.is_open("EVT-1")


async def test_drift_check_passes_within_tolerance():
    from optimizer.opportunity import OpportunityTier
    books = make_arb_books()  # MKT-A and MKT-B at 400t YES ask
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    # populate event state (normally done by _evaluate_and_dispatch)
    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

    # expected=380, current=400 → drift=20 < max_drift=min(100,100)=100 → proceeds
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"MKT-A": 380, "MKT-B": 380},
        capital_required=38_000,
        profit=20_000,
    )
    await orch._execute_and_settle("EVT-1", "buy", tier)

    assert ledger._positions.is_open("EVT-1")


async def test_discovery_adds_new_cold_event():
    books: dict = {}
    stub_feed = _StubFeed()
    orch, _, _, _, _, graph = make_system(books, feed=stub_feed)

    new_cold = {
        "event_ticker": "EVT-NEW",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "MKT-X", "yes_ask_size_fp": "0", "yes_bid_size_fp": "0", "volume_24h_fp": "0"},
            {"ticker": "MKT-Y", "yes_ask_size_fp": "0", "yes_bid_size_fp": "0", "volume_24h_fp": "0"},
        ],
    }

    await orch._process_discovered_events([new_cold])

    assert "EVT-NEW" in graph.events
    assert "MKT-X" not in books
    assert "EVT-NEW" in orch._cold_events


# Event membership resync (markets added/removed on an existing event)

def _mx_event(event_id: str, tickers: list[str], liquid: bool = False) -> dict:
    from feed.rest_client import DEPTH_MIN, VOLUME_24H_MIN
    depth = str(DEPTH_MIN) if liquid else "0"
    vol = str(VOLUME_24H_MIN) if liquid else "0"
    return {
        "event_ticker": event_id,
        "mutually_exclusive": True,
        "markets": [
            {"ticker": t, "yes_ask_size_fp": depth, "yes_bid_size_fp": "0", "volume_24h_fp": vol}
            for t in tickers
        ],
    }


async def test_membership_change_resyncs_cold_event():
    graph = StateGraph()
    graph.add_event("EVT-1", ["MKT-A", "MKT-B"])
    books: dict = {}
    cold_events = {"EVT-1": ["MKT-A", "MKT-B"]}
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events)

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B", "MKT-C"])])

    assert set(graph.events["EVT-1"].tickers) == {"MKT-A", "MKT-B", "MKT-C"}
    assert orch._cold_events["EVT-1"] == ["MKT-A", "MKT-B", "MKT-C"]


async def test_membership_change_resyncs_hot_event():
    books = make_arb_books()
    stub_feed = _StubFeed()
    stub_feed._hot_events["EVT-1"] = ["MKT-A", "MKT-B"]
    orch, _, _, _, _, graph = make_system(books, feed=stub_feed)

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B", "MKT-C"], liquid=True)])

    assert set(graph.events["EVT-1"].tickers) == {"MKT-A", "MKT-B", "MKT-C"}
    assert "MKT-C" in books


async def test_membership_mixed_change_with_open_position_keeps_stale_set():
    """A removed market can't be auto-repaired: the stale set is kept."""
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ledger.has_open_position("EVT-1")

    # MKT-B removed and MKT-D added: mixed change is not auto-repairable
    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-D"], liquid=True)])

    assert set(graph.events["EVT-1"].tickers) == {"MKT-A", "MKT-B"}
    assert "MKT-D" not in books
    assert "EVT-1" not in orch._pending_repairs


async def test_membership_unchanged_no_resync():
    graph = StateGraph()
    graph.add_event("EVT-1", ["MKT-A", "MKT-B"])
    books: dict = {}
    cold_events = {"EVT-1": ["MKT-A", "MKT-B"]}
    orch, _, _, _, _, _ = make_system(books, graph=graph, cold_events=cold_events)
    state_before = graph.events["EVT-1"]

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B"])])

    assert graph.events["EVT-1"] is state_before


# Settlement reconciliation for hot events absent from discovery

async def test_reconcile_missing_hot_event_with_winner_queues_resolution():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "yes"},
        {"ticker": "MKT-B", "status": "settled", "result": "no"},
    ]})
    orch, _, _, _, resolution_queue, graph = make_system(books, rest_client=rest)

    await orch._process_discovered_events([])

    assert resolution_queue.get_nowait() == "MKT-A"


async def test_reconcile_voided_event_cleaned_up():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "void"},
        {"ticker": "MKT-B", "status": "settled", "result": "void"},
    ]})
    orch, _, _, _, _, graph = make_system(books, rest_client=rest)

    await orch._process_discovered_events([])

    assert "EVT-1" not in graph.events


async def test_reconcile_unsettled_missing_event_left_alone():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "closed", "result": ""},
        {"ticker": "MKT-B", "status": "closed", "result": ""},
    ]})
    orch, _, _, _, resolution_queue, graph = make_system(books, rest_client=rest)

    await orch._process_discovered_events([])

    assert "EVT-1" in graph.events
    assert resolution_queue.empty()


async def test_reconcile_empty_markets_response_left_alone():
    books = make_arb_books()
    orch, _, _, _, _, graph = make_system(books)  # stub returns no markets

    await orch._process_discovered_events([])

    assert "EVT-1" in graph.events


async def test_reconcile_voided_event_with_position_kept():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "void"},
        {"ticker": "MKT-B", "status": "settled", "result": "void"},
    ]})
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, rest_client=rest)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ledger.has_open_position("EVT-1")

    await orch._process_discovered_events([])

    assert "EVT-1" in graph.events  # kept for manual attention


# Execution accounting: no silent discards, zero-fill release

async def test_zero_fill_execution_releases_commitment():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(
        books, participation_rate=0.0
    )

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert not ledger.has_open_position("EVT-1")
    assert optimizer.committed == {}
    assert ledger.total_capital == 10_000_000


async def test_trade_recorded_even_when_cost_exceeds_available():
    from optimizer.opportunity import OpportunityTier
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, initial_capital=1_000_000)

    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

    tier = OpportunityTier(
        quantity=10000,
        leg_prices={"MKT-A": 400, "MKT-B": 400},
        capital_required=8_000_000,
        profit=2_000_000,
    )
    await orch._execute_and_settle("EVT-1", "buy", tier)

    # previously this trade was silently discarded; now it must be recorded
    assert ledger.has_open_position("EVT-1")
    assert ledger.available_capital < 0
    assert ledger.locked_capital > 0


async def test_reconcile_untracked_winner_settles_by_event_id():
    """Membership changed after entry and the new (untracked) market won: the
    position must still close and the event must still be cleaned up."""
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "no"},
        {"ticker": "MKT-B", "status": "settled", "result": "no"},
        {"ticker": "MKT-C", "status": "settled", "result": "yes"},  # never tracked
    ]})
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, rest_client=rest)

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ledger.has_open_position("EVT-1")

    await orch._process_discovered_events([])

    assert not ledger.has_open_position("EVT-1")
    assert "EVT-1" not in graph.events
    assert resolution_queue.empty()  # settled directly, not via the ticker queue


async def test_reconcile_untracked_winner_without_position_cleans_up():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "no"},
        {"ticker": "MKT-B", "status": "settled", "result": "no"},
        {"ticker": "MKT-C", "status": "settled", "result": "yes"},
    ]})
    orch, _, ledger, _, resolution_queue, graph = make_system(books, rest_client=rest)

    await orch._process_discovered_events([])

    assert "EVT-1" not in graph.events
    assert resolution_queue.empty()


# Membership repair: buy position + added market → complete or unwind

async def _open_buy_position(orch):
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_added_market_with_buy_position_queues_repair():
    books = make_arb_books()
    stub_feed = _StubFeed()
    stub_feed._hot_events["EVT-1"] = ["MKT-A", "MKT-B"]
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, feed=stub_feed)

    event_queue.put_nowait("EVT-1")
    await _open_buy_position(orch)
    assert ledger.has_open_position("EVT-1")

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B", "MKT-C"], liquid=True)])

    assert "EVT-1" in orch._pending_repairs
    assert set(graph.events["EVT-1"].tickers) == {"MKT-A", "MKT-B", "MKT-C"}
    assert "MKT-C" in books
    assert "MKT-C" in stub_feed._hot_events["EVT-1"]


async def test_repair_completes_when_new_leg_is_cheap():
    books = make_arb_books()
    # capital large enough that the 10%-capped entry still clears the completion floor
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, initial_capital=10_000_000)

    event_queue.put_nowait("EVT-1")
    await _open_buy_position(orch)
    hedge_before = ledger.get_position("EVT-1").hedge_qty
    locked_before = ledger.locked_capital

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B", "MKT-C"], liquid=True)])
    # cheap YES asks on the new leg → completion should win
    books["MKT-C"].no_buys.set_real_qty(900, 100_000)  # YES ask 100
    # the partial-tier entry consumed all available capital; top up so the
    # completion buy is affordable
    ledger.available_capital += 200_000

    orch._pending_repairs.discard("EVT-1")
    await orch._repair_position("EVT-1")

    record = ledger.get_position("EVT-1")
    assert record is not None
    assert record.hedge_qty == hedge_before
    assert set(record.result.legs) == {"MKT-A", "MKT-B", "MKT-C"}
    assert ledger.locked_capital > locked_before  # paid for the new leg


async def test_repair_unwinds_when_new_leg_unbuyable():
    books = make_arb_books()
    # give the old legs bids so an unwind can actually fill (tight spread so
    # the spread-floor gate still admits the entry)
    books["MKT-A"].yes_buys.set_real_qty(450, 100_000)
    books["MKT-B"].yes_buys.set_real_qty(450, 100_000)
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books, initial_capital=1_000_000)

    event_queue.put_nowait("EVT-1")
    await _open_buy_position(orch)
    assert ledger.has_open_position("EVT-1")

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B", "MKT-C"], liquid=True)])
    # MKT-C book stays empty: completion impossible → unwind

    orch._pending_repairs.discard("EVT-1")
    await orch._repair_position("EVT-1")

    assert not ledger.has_open_position("EVT-1")
    assert ledger.locked_capital == 0


async def test_dispatch_routes_pending_repair():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)
    orch._pending_repairs.add("EVT-1")

    orch._evaluate_and_dispatch("EVT-1")
    assert "EVT-1" not in orch._pending_repairs
    await asyncio.sleep(0.05)  # let the repair task run (no position → no-op)



async def test_repair_impossible_keeps_position_and_requeues():
    """No cash to complete, no bids to unwind: the position must NOT be closed."""
    books = make_arb_books()  # old legs have no bids
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")
    await _open_buy_position(orch)
    assert ledger.has_open_position("EVT-1")
    locked_before = ledger.locked_capital

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B", "MKT-C"], liquid=True)])
    # MKT-C book empty and available capital ~0 → neither action possible

    orch._pending_repairs.discard("EVT-1")
    await orch._repair_position("EVT-1")

    assert ledger.has_open_position("EVT-1")
    assert ledger.locked_capital == locked_before
    assert "EVT-1" in orch._pending_repairs  # retried on the next book event


# Settlement counters feeding the DISCOVERY log

async def test_settlement_increments_settled_counters():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    resolution_queue.put_nowait("MKT-A")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert orch._settled_events == 1
    assert orch._settled_tickers == 2


async def test_discovery_log_resets_settled_counters():
    books = make_arb_books()
    orch, _, _, _, _, graph = make_system(books)
    orch._settled_events = 3
    orch._settled_tickers = 6

    await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B"])])

    assert orch._settled_events == 0
    assert orch._settled_tickers == 0


async def test_reconcile_returns_winner_count():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "yes"},
        {"ticker": "MKT-B", "status": "settled", "result": "no"},
    ]})
    orch, _, _, _, _, _ = make_system(books, rest_client=rest)

    reconciled = await orch._reconcile_missing_hot_events(set())

    assert reconciled == 1


async def test_reconcile_void_counts_as_settled():
    books = make_arb_books()
    rest = _StubRestClient({"EVT-1": [
        {"ticker": "MKT-A", "status": "settled", "result": "void"},
        {"ticker": "MKT-B", "status": "settled", "result": "void"},
    ]})
    orch, _, _, _, _, graph = make_system(books, rest_client=rest)

    reconciled = await orch._reconcile_missing_hot_events(set())

    assert reconciled == 1
    assert orch._settled_events == 1
    assert orch._settled_tickers == 2
    assert "EVT-1" not in graph.events


# Demotion with consecutive-cycle hysteresis

async def test_demotion_after_consecutive_failing_cycles():
    books = make_arb_books()
    stub_feed = _StubFeed()
    stub_feed._hot_events["EVT-1"] = ["MKT-A", "MKT-B"]
    orch, optimizer, ledger, _, _, graph = make_system(books, feed=stub_feed)
    optimizer.update("EVT-1", None)  # nothing cached

    illiquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=False)
    await orch._process_discovered_events([illiquid])
    await orch._process_discovered_events([illiquid])
    assert "EVT-1" not in orch._cold_events  # 2 strikes: still hot

    await orch._process_discovered_events([illiquid])

    assert orch._cold_events["EVT-1"] == ["MKT-A", "MKT-B"]
    assert "MKT-A" not in books
    assert "MKT-B" not in books
    assert "EVT-1" in stub_feed._cold_events
    assert "EVT-1" not in stub_feed._hot_events
    assert sorted(stub_feed.removed) == ["MKT-A", "MKT-B"]
    assert "EVT-1" in graph.events  # graph keeps cold events


async def test_demotion_clears_cached_opportunity():
    from optimizer.opportunity import Opportunity, OpportunityTier
    books = make_arb_books()
    orch, optimizer, _, _, _, _ = make_system(books)
    tier = OpportunityTier(quantity=100, leg_prices={"MKT-A": 400, "MKT-B": 400},
                           capital_required=80_000, profit=20_000)
    optimizer.update("EVT-1", Opportunity(event_id="EVT-1", side="buy", tiers=[tier]))

    illiquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=False)
    for _ in range(3):
        await orch._process_discovered_events([illiquid])

    assert "EVT-1" not in optimizer.open_opportunities


async def test_demotion_strikes_reset_on_liquid_cycle():
    books = make_arb_books()
    orch, _, _, _, _, _ = make_system(books)

    illiquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=False)
    liquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=True)

    await orch._process_discovered_events([illiquid])
    await orch._process_discovered_events([illiquid])
    await orch._process_discovered_events([liquid])    # resets strikes
    await orch._process_discovered_events([illiquid])
    await orch._process_discovered_events([illiquid])

    assert "EVT-1" not in orch._cold_events  # never hit 3 consecutive


async def test_no_demotion_with_open_position():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, _, _ = make_system(books)
    event_queue.put_nowait("EVT-1")
    await _open_buy_position(orch)
    assert ledger.has_open_position("EVT-1")

    illiquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=False)
    for _ in range(4):
        await orch._process_discovered_events([illiquid])

    assert "EVT-1" not in orch._cold_events
    assert "MKT-A" in books


async def test_no_demotion_while_committed():
    books = make_arb_books()
    orch, optimizer, _, _, _, _ = make_system(books)
    optimizer.commit("EVT-1", 1000)

    illiquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=False)
    for _ in range(4):
        await orch._process_discovered_events([illiquid])

    assert "EVT-1" not in orch._cold_events


async def test_demoted_event_can_be_repromoted():
    books = make_arb_books()
    stub_feed = _StubFeed()
    stub_feed._hot_events["EVT-1"] = ["MKT-A", "MKT-B"]
    orch, _, _, _, _, graph = make_system(books, feed=stub_feed)

    illiquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=False)
    for _ in range(3):
        await orch._process_discovered_events([illiquid])
    assert "EVT-1" in orch._cold_events

    liquid = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=True)
    await orch._process_discovered_events([liquid])

    assert "EVT-1" not in orch._cold_events
    assert "MKT-A" in books
    assert "EVT-1" in stub_feed._hot_events
    assert stub_feed.subscribed_orderbook[-1] == ["MKT-A", "MKT-B"]


async def test_settlement_clears_demotion_strikes():
    books = make_arb_books()
    orch, _, _, _, resolution_queue, _ = make_system(books)
    orch._demotion_strikes["EVT-1"] = 2

    await orch._cleanup_event("EVT-1")

    assert "EVT-1" not in orch._demotion_strikes


async def test_expired_but_liquid_event_gets_demoted():
    """An event past close_time fails classification even with displayed depth,
    so a hot zombie market is demoted after the hysteresis window."""
    books = make_arb_books()
    orch, _, _, _, _, _ = make_system(books)

    zombie = _mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=True)
    for m in zombie["markets"]:
        m["close_time"] = "2020-01-01T00:00:00Z"

    for _ in range(3):
        await orch._process_discovered_events([zombie])

    assert "EVT-1" in orch._cold_events
    assert "MKT-A" not in books


async def test_reconcile_paces_between_multiple_events():
    """Multiple missing hot events must not burst the /markets endpoint."""
    from unittest.mock import AsyncMock, call, patch
    import engine as engine_mod

    books = {
        "MKT-A": PublicMarketBook(), "MKT-B": PublicMarketBook(),
        "MKT-X": PublicMarketBook(), "MKT-Y": PublicMarketBook(),
    }
    graph = StateGraph()
    graph.add_event("EVT-1", ["MKT-A", "MKT-B"])
    graph.add_event("EVT-2", ["MKT-X", "MKT-Y"])
    orch, _, _, _, _, _ = make_system(books, graph=graph)

    with patch.object(engine_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
        await orch._reconcile_missing_hot_events(set())

    pauses = [c for c in mock_sleep.call_args_list if c == call(engine_mod._RECONCILE_PAUSE_S)]
    assert len(pauses) == 1  # 2 missing events → 1 pause between them


# Ragged resolutions are tracked to settlement, not booked as cash

async def test_ragged_resolution_records_open_position():
    """A failed unwind leaving naked exposure must open a tracked position,
    not book the proceeds as realized profit (the CHCBAL bug)."""
    from optimizer.opportunity import OpportunityTier
    from risk.models import ResolutionResult
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

    async def ragged_handle(result, budget=None):
        return ResolutionResult(
            original_result=result, action="unwound",
            final_hedge_qty=0, resolution_capital=0, resolution_fees=0,
            total_realized_profit=224_250,  # the optimistic number the old code banked
            final_holdings={"MKT-A": 345, "MKT-B": 0},
        )
    orch._risk_manager.handle = ragged_handle

    tier = OpportunityTier(quantity=2000, leg_prices={"MKT-A": 400, "MKT-B": 400},
                           capital_required=1_600_000, profit=400_000)
    await orch._execute_and_settle("EVT-1", "buy", tier)

    assert ledger.has_open_position("EVT-1")
    record = ledger.get_position("EVT-1")
    assert record.holdings == {"MKT-A": 345, "MKT-B": 0}
    assert record.naked_qty == 345


async def test_flat_resolution_still_books_unwound():
    from optimizer.opportunity import OpportunityTier
    from risk.models import ResolutionResult
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

    async def flat_handle(result, budget=None):
        return ResolutionResult(
            original_result=result, action="unwound",
            final_hedge_qty=0, resolution_capital=0, resolution_fees=0,
            total_realized_profit=-500,
            final_holdings={"MKT-A": 0, "MKT-B": 0},
        )
    orch._risk_manager.handle = flat_handle

    tier = OpportunityTier(quantity=2000, leg_prices={"MKT-A": 400, "MKT-B": 400},
                           capital_required=1_600_000, profit=400_000)
    initial = ledger.total_capital
    await orch._execute_and_settle("EVT-1", "buy", tier)

    assert not ledger.has_open_position("EVT-1")
    assert ledger.total_capital == initial - 500  # cash P&L realized immediately


async def test_trade_size_capped_at_ten_percent_of_capital():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    event_queue.put_nowait("EVT-1")
    await _open_buy_position(orch)

    assert ledger.has_open_position("EVT-1")
    # 10% of 10_000_000 total capital = 1_000_000 max deployed on one event
    assert ledger.locked_capital <= 1_000_000


# ---------------------------------------------------------------------------
# Evaluation funnel telemetry
# ---------------------------------------------------------------------------

async def test_dispatch_feeds_eval_stats():
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, resolution_queue, graph = make_system(books)

    orch._evaluate_and_dispatch("EVT-1")

    assert orch._eval_stats.get("evaluated") == 1
    assert orch._eval_stats.get("opportunities") == 1


async def test_discovery_emits_and_resets_eval_stats(caplog):
    import logging
    books = make_arb_books()
    orch, _, _, _, _, _ = make_system(books)
    orch._eval_stats = {"evaluated": 42, "no_cross": 40, "return_rate": 2}

    with caplog.at_level(logging.INFO, logger="engine"):
        await orch._process_discovered_events([_mx_event("EVT-1", ["MKT-A", "MKT-B"], liquid=True)])

    assert "EVAL  evaluated=42  no_cross=40" in caplog.text
    assert "return_rate=2" in caplog.text
    assert "best_miss=none" in caplog.text
    assert orch._eval_stats == {}


async def test_log_eval_stats_emits_and_resets(caplog):
    import logging
    books = make_arb_books()
    orch, _, _, _, _, _ = make_system(books)
    orch._eval_stats = {"evaluated": 300, "no_cross": 300}

    with caplog.at_level(logging.INFO, logger="engine"):
        orch._log_eval_stats()

    assert "EVAL  evaluated=300  no_cross=300" in caplog.text
    assert orch._eval_stats == {}


async def test_log_eval_stats_formats_best_miss(caplog):
    import logging
    books = make_arb_books()
    orch, _, _, _, _, _ = make_system(books)
    orch._eval_stats = {
        "evaluated": 10, "return_rate": 3,
        "best_miss_return": 0.0231, "best_miss_event": "KXMLBGAME-26JUL11NYYBOS", "best_miss_joint": 955,
    }

    with caplog.at_level(logging.INFO, logger="engine"):
        orch._log_eval_stats()

    assert "best_miss=KXMLBGAME-26JUL11NYYBOS@2.31% joint=955" in caplog.text


# Dispatch-abort telemetry

async def test_abort_logs_reeval_when_opportunity_gone(caplog):
    import logging
    from optimizer.opportunity import OpportunityTier
    books = {"MKT-A": PublicMarketBook(), "MKT-B": PublicMarketBook()}  # no arb
    orch, optimizer, ledger, _, _, graph = make_system(books)
    optimizer.commit("EVT-1", 1000)

    tier = OpportunityTier(quantity=1000, leg_prices={"MKT-A": 455, "MKT-B": 455},
                           capital_required=910_000, profit=50_000)
    with caplog.at_level(logging.INFO, logger="engine"):
        await orch._execute_and_settle("EVT-1", "buy", tier)

    assert "ABORT  EVT-1  reason=reeval" in caplog.text
    assert "EVT-1" not in optimizer.committed  # commitment released


async def test_abort_logs_drift_when_price_moved(caplog):
    import logging
    from optimizer.opportunity import OpportunityTier
    books = make_arb_books()
    orch, optimizer, ledger, _, _, graph = make_system(books)
    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

    # stale leg prices 200 vs current 455 → drift 255 > cap
    tier = OpportunityTier(quantity=1000, leg_prices={"MKT-A": 200, "MKT-B": 200},
                           capital_required=400_000, profit=100_000)
    with caplog.at_level(logging.INFO, logger="engine"):
        await orch._execute_and_settle("EVT-1", "buy", tier)

    assert "ABORT  EVT-1  reason=drift" in caplog.text
    assert "expected=200" in caplog.text


async def test_abort_logs_zero_fill(caplog):
    import logging
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, _, _ = make_system(books, participation_rate=0.0)

    event_queue.put_nowait("EVT-1")
    with caplog.at_level(logging.INFO, logger="engine"):
        await _open_buy_position(orch)

    assert "ABORT  EVT-1  reason=zero_fill" in caplog.text
    assert not ledger.has_open_position("EVT-1")


# Persistence gate: quotes must survive the delay to trade

async def test_vanished_quote_aborts_after_persistence_delay(caplog):
    """A book that empties during the persistence window must abort as reeval,
    never execute (the phantom-quote/flicker case)."""
    import logging
    from optimizer.opportunity import OpportunityTier
    books = make_arb_books()
    orch, optimizer, ledger, _, _, graph = make_system(books)
    orch._persistence_delay_s = 0.05
    for ticker, book in books.items():
        graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())
    optimizer.commit("EVT-1", 910_000)

    tier = OpportunityTier(quantity=1000, leg_prices={"MKT-A": 455, "MKT-B": 455},
                           capital_required=910_000, profit=50_000)

    with caplog.at_level(logging.INFO, logger="engine"):
        task = asyncio.create_task(orch._execute_and_settle("EVT-1", "buy", tier))
        await asyncio.sleep(0.01)
        # the quote gets pulled mid-window, as cancel-on-touch flicker would
        books["MKT-A"].no_buys.set_real_qty(545, 0)
        await task

    assert "ABORT  EVT-1  reason=reeval" in caplog.text
    assert not ledger.has_open_position("EVT-1")
    assert "EVT-1" not in optimizer.committed


async def test_persistent_quote_trades_after_delay():
    """A book that survives the persistence window trades normally."""
    books = make_arb_books()
    orch, optimizer, ledger, event_queue, _, _ = make_system(books)
    orch._persistence_delay_s = 0.05

    event_queue.put_nowait("EVT-1")
    task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ledger.has_open_position("EVT-1")
