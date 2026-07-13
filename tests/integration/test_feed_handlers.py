import asyncio
from unittest.mock import AsyncMock
import pytest
from book.public_book import PublicMarketBook
from graph.graph import StateGraph
from feed.feed import KalshiFeed


def make_feed(tickers: list[str]) -> tuple[KalshiFeed, dict[str, PublicMarketBook], asyncio.Queue, asyncio.Queue]:
    books = {ticker: PublicMarketBook() for ticker in tickers}
    graph = StateGraph()
    graph.add_event("EVT-1", tickers)
    event_queue: asyncio.Queue = asyncio.Queue()
    resolution_queue: asyncio.Queue = asyncio.Queue()
    feed = KalshiFeed(
        books=books,
        graph=graph,
        event_queue=event_queue,
        resolution_queue=resolution_queue,
        key_id="test",
        private_key_path="test.pem",
    )
    return feed, books, event_queue, resolution_queue


def test_snapshot_populates_yes_levels():
    feed, books, queue, rqueue = make_feed(["MKT-A", "MKT-B"])
    feed._handle_snapshot({
        "market_ticker": "MKT-A",
        "yes_dollars_fp": [["0.5000", "10.00"], ["0.4800", "20.00"]],
        "no_dollars_fp": [],
    })
    assert books["MKT-A"].yes_buys.real_qty[500] == 1000
    assert books["MKT-A"].yes_buys.real_qty[480] == 2000


def test_snapshot_populates_no_levels():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_snapshot({
        "market_ticker": "MKT-A",
        "yes_dollars_fp": [],
        "no_dollars_fp": [["0.4500", "5.00"]],
    })
    assert books["MKT-A"].no_buys.real_qty[450] == 500


def test_snapshot_notifies_event_queue():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_snapshot({"market_ticker": "MKT-A", "yes_dollars_fp": [["0.5000", "1.00"]], "no_dollars_fp": []})
    assert not queue.empty()
    assert queue.get_nowait() == "EVT-1"


def test_snapshot_unknown_ticker_ignored():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_snapshot({"market_ticker": "MKT-Z", "yes_dollars_fp": [["0.5000", "1.00"]], "no_dollars_fp": []})
    assert queue.empty()


def test_delta_updates_yes_level():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    books["MKT-A"].yes_buys.set_real_qty(500, 1000)
    feed._handle_delta({
        "market_ticker": "MKT-A",
        "side": "yes",
        "price_dollars": "0.5000",
        "delta_fp": "-3.00",
    })
    assert books["MKT-A"].yes_buys.real_qty[500] == 700


def test_delta_updates_no_level():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    books["MKT-A"].no_buys.set_real_qty(600, 800)
    feed._handle_delta({
        "market_ticker": "MKT-A",
        "side": "no",
        "price_dollars": "0.6000",
        "delta_fp": "5.00",
    })
    assert books["MKT-A"].no_buys.real_qty[600] == 1300


def test_delta_notifies_event_queue():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_snapshot({"market_ticker": "MKT-A", "yes_dollars_fp": [], "no_dollars_fp": []})
    queue.get_nowait()  # consume the snapshot notification
    feed._handle_delta({"market_ticker": "MKT-A", "side": "yes", "price_dollars": "0.5000", "delta_fp": "1.00"})
    assert queue.get_nowait() == "EVT-1"


def test_delta_unknown_ticker_ignored():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_delta({"market_ticker": "MKT-Z", "side": "yes", "price_dollars": "0.5000", "delta_fp": "-1.00"})
    assert queue.empty()


def test_delta_decrease_fires_listener():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    books["MKT-A"].yes_buys.set_real_qty(500, 1000)
    received = {}
    books["MKT-A"]._yes_listeners.append(lambda d: received.update(d))
    feed._handle_delta({"market_ticker": "MKT-A", "side": "yes", "price_dollars": "0.5000", "delta_fp": "-2.00"})
    assert received == {500: 200}


def test_notify_puts_correct_event_id():
    tickers = ["MKT-A", "MKT-B", "MKT-C"]
    feed, books, queue, rqueue = make_feed(tickers)
    feed._snapshotted.update(tickers)  # all tickers fresh
    feed._notify("MKT-B")
    assert queue.get_nowait() == "EVT-1"


def test_multiple_snapshots_populate_independent_books():
    feed, books, queue, rqueue = make_feed(["MKT-A", "MKT-B"])
    feed._handle_snapshot({"market_ticker": "MKT-A", "yes_dollars_fp": [["0.5000", "10.00"]], "no_dollars_fp": []})
    feed._handle_snapshot({"market_ticker": "MKT-B", "yes_dollars_fp": [["0.4800", "5.00"]], "no_dollars_fp": []})
    assert books["MKT-A"].yes_buys.real_qty[500] == 1000
    assert books["MKT-B"].yes_buys.real_qty[480] == 500
    assert books["MKT-A"].yes_buys.real_qty[480] == 0


def test_lifecycle_yes_puts_winner_ticker():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_lifecycle({"market_ticker": "MKT-A", "status": "finalized", "result": "yes"})
    assert not rqueue.empty()
    assert rqueue.get_nowait() == "MKT-A"


def test_lifecycle_no_result_ignored():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_lifecycle({"market_ticker": "MKT-A", "status": "finalized", "result": "no"})
    assert rqueue.empty()


def test_lifecycle_non_finalized_ignored():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_lifecycle({"market_ticker": "MKT-A", "status": "open", "result": "yes"})
    assert rqueue.empty()


def test_lifecycle_missing_ticker_ignored():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_lifecycle({"status": "finalized", "result": "yes"})
    assert rqueue.empty()


def test_lifecycle_does_not_touch_event_queue():
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_lifecycle({"market_ticker": "MKT-A", "status": "finalized", "result": "yes"})
    assert queue.empty()


# Event-keyed hot/cold tracking

def test_init_derives_hot_events_from_graph_and_books():
    feed, _, _, _ = make_feed(["MKT-A", "MKT-B"])
    assert feed._hot_events == {"EVT-1": ["MKT-A", "MKT-B"]}


def test_init_uses_provided_cold_events():
    graph = StateGraph()
    graph.add_event("EVT-COLD", ["MKT-X", "MKT-Y"])
    feed = KalshiFeed(
        books={},
        graph=graph,
        event_queue=asyncio.Queue(),
        resolution_queue=asyncio.Queue(),
        key_id="test",
        private_key_path="test.pem",
        cold_events={"EVT-COLD": ["MKT-X", "MKT-Y"]},
    )
    assert feed._cold_events == {"EVT-COLD": ["MKT-X", "MKT-Y"]}
    assert feed._hot_events == {}


def test_hot_and_cold_count_properties():
    feed, _, _, _ = make_feed(["MKT-A", "MKT-B"])
    assert feed.hot_event_count == 1
    assert feed.hot_ticker_count == 2
    assert feed.cold_event_count == 0
    assert feed.cold_ticker_count == 0


# subscribe_new cold path — no WebSocket call

async def test_subscribe_new_cold_appends_to_cold_events():
    feed, _, _, _ = make_feed(["MKT-A"])
    await feed.subscribe_new("EVT-COLD", ["MKT-X", "MKT-Y"], hot=False)
    assert feed._cold_events["EVT-COLD"] == ["MKT-X", "MKT-Y"]


async def test_subscribe_new_cold_does_not_call_client():
    from unittest.mock import AsyncMock
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.subscribe = AsyncMock()

    await feed.subscribe_new("EVT-COLD", ["MKT-X", "MKT-Y"], hot=False)

    feed._client.subscribe.assert_not_called()


async def test_subscribe_new_cold_does_not_affect_hot_events():
    feed, _, _, _ = make_feed(["MKT-A"])
    await feed.subscribe_new("EVT-COLD", ["MKT-X"], hot=False)
    assert "EVT-COLD" not in feed._hot_events


async def test_subscribe_orderbook_subscribes_both_channels():
    from unittest.mock import AsyncMock
    import json
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client._ws = AsyncMock()
    feed._client._ws.send = AsyncMock()

    feed._cold_events["EVT-X"] = ["MKT-X"]
    await feed.subscribe_orderbook("EVT-X", ["MKT-X"])

    sent_channels = {
        json.loads(call.args[0])["params"]["channels"][0]
        for call in feed._client._ws.send.call_args_list
    }
    assert "orderbook_delta" in sent_channels
    assert "market_lifecycle_v2" in sent_channels
    assert "EVT-X" not in feed._cold_events
    assert feed._hot_events["EVT-X"] == ["MKT-X"]


async def test_subscribe_new_cold_is_idempotent():
    feed, _, _, _ = make_feed(["MKT-A"])
    await feed.subscribe_new("EVT-COLD", ["MKT-X"], hot=False)
    await feed.subscribe_new("EVT-COLD", ["MKT-X"], hot=False)
    assert feed._cold_events["EVT-COLD"] == ["MKT-X"]


# Book freshness gate — evaluation blocked until all tickers snapshotted

def test_partial_snapshot_does_not_notify():
    # Two-ticker event: first snapshot should NOT queue until the second arrives
    feed, books, queue, rqueue = make_feed(["MKT-A", "MKT-B"])
    feed._handle_snapshot({"market_ticker": "MKT-A", "yes_dollars_fp": [], "no_dollars_fp": []})
    assert queue.empty()


def test_all_tickers_snapshotted_notifies():
    # Both tickers snapshotted: second snapshot fires the queue
    feed, books, queue, rqueue = make_feed(["MKT-A", "MKT-B"])
    feed._handle_snapshot({"market_ticker": "MKT-A", "yes_dollars_fp": [], "no_dollars_fp": []})
    feed._handle_snapshot({"market_ticker": "MKT-B", "yes_dollars_fp": [], "no_dollars_fp": []})
    assert not queue.empty()
    assert queue.get_nowait() == "EVT-1"


def test_delta_suppressed_before_snapshot():
    # Delta arrives before snapshot: event must not be queued
    feed, books, queue, rqueue = make_feed(["MKT-A", "MKT-B"])
    feed._handle_delta({"market_ticker": "MKT-A", "side": "yes", "price_dollars": "0.5000", "delta_fp": "1.00"})
    assert queue.empty()


def test_freshness_cleared_between_connections():
    # Simulates reconnect: _snapshotted is cleared, delta no longer fires
    feed, books, queue, rqueue = make_feed(["MKT-A"])
    feed._handle_snapshot({"market_ticker": "MKT-A", "yes_dollars_fp": [], "no_dollars_fp": []})
    queue.get_nowait()  # consume initial notification
    assert "MKT-A" in feed._snapshotted
    feed._snapshotted.clear()  # reconnect clears freshness
    feed._handle_delta({"market_ticker": "MKT-A", "side": "yes", "price_dollars": "0.5000", "delta_fp": "1.00"})
    assert queue.empty()


# subscribed acks + ticker removal / unsubscribe

async def test_handle_subscribed_forwards_to_client():
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.record_subscribed = AsyncMock()

    msg = {"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 5}}
    await feed._handle_subscribed(msg)

    feed._client.record_subscribed.assert_awaited_once_with(msg)


async def test_remove_event_drops_from_hot_events():
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.unsubscribe = AsyncMock()

    await feed.remove_event("EVT-1", ["MKT-A"])

    assert "EVT-1" not in feed._hot_events


async def test_remove_event_drops_from_cold_events():
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.unsubscribe = AsyncMock()
    feed._cold_events["EVT-X"] = ["MKT-X"]

    await feed.remove_event("EVT-X", ["MKT-X"])

    assert "EVT-X" not in feed._cold_events


async def test_remove_event_does_not_unsubscribe_cold_only_event():
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.unsubscribe = AsyncMock()
    feed._cold_events["EVT-X"] = ["MKT-X"]

    await feed.remove_event("EVT-X", ["MKT-X"])

    feed._client.unsubscribe.assert_not_called()


async def test_remove_event_unsubscribes_hot_event():
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.unsubscribe = AsyncMock()

    await feed.remove_event("EVT-1", ["MKT-A"])

    feed._client.unsubscribe.assert_called_once_with(["MKT-A"])


async def test_remove_event_ignores_unknown_event():
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.unsubscribe = AsyncMock()

    await feed.remove_event("EVT-UNKNOWN", ["MKT-UNKNOWN"])

    feed._client.unsubscribe.assert_not_called()


async def test_add_tickers_appends_and_subscribes_only_new():
    from unittest.mock import AsyncMock
    feed, _, _, _ = make_feed(["MKT-A", "MKT-B"])
    feed._client.subscribe = AsyncMock()

    await feed.add_tickers("EVT-1", ["MKT-B", "MKT-C"])

    assert sorted(feed._hot_events["EVT-1"]) == ["MKT-A", "MKT-B", "MKT-C"]
    feed._client.subscribe.assert_called_once_with(["MKT-C"])


async def test_add_tickers_noop_when_all_known():
    from unittest.mock import AsyncMock
    feed, _, _, _ = make_feed(["MKT-A", "MKT-B"])
    feed._client.subscribe = AsyncMock()

    await feed.add_tickers("EVT-1", ["MKT-A"])

    feed._client.subscribe.assert_not_called()


# Reconnect backoff policy

async def test_backoff_grows_when_connection_flaps():
    from unittest.mock import patch
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.connect = AsyncMock()
    feed._client.subscribe = AsyncMock()
    feed._client.receive = AsyncMock(side_effect=RuntimeError("boom"))

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    with patch("feed.feed.asyncio.sleep", new=fake_sleep):
        try:
            await feed.run()
        except asyncio.CancelledError:
            pass

    # instant flapping never reaches the 60s stability window: backoff doubles
    assert sleeps == [1.0, 2.0, 4.0]


async def test_backoff_resets_after_stable_connection():
    from unittest.mock import patch
    feed, _, _, _ = make_feed(["MKT-A"])
    feed._client.connect = AsyncMock()
    feed._client.subscribe = AsyncMock()
    feed._client.receive = AsyncMock(side_effect=RuntimeError("boom"))

    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    fake_now = iter(float(i * 100) for i in range(20))  # 100s between calls: stable

    with patch("feed.feed.asyncio.sleep", new=fake_sleep), \
         patch("feed.feed.time.monotonic", side_effect=lambda: next(fake_now)):
        try:
            await feed.run()
        except asyncio.CancelledError:
            pass

    # every connection "lasted" 100s > stability window: backoff resets each time
    assert sleeps == [1.0, 1.0, 1.0]


async def test_remove_event_clears_snapshot_freshness():
    feed, _, _, _ = make_feed(["MKT-A", "MKT-B"])
    feed._client.unsubscribe = AsyncMock()
    feed._snapshotted.update(["MKT-A", "MKT-B"])

    await feed.remove_event("EVT-1", ["MKT-A", "MKT-B"])

    assert "MKT-A" not in feed._snapshotted
    assert "MKT-B" not in feed._snapshotted
