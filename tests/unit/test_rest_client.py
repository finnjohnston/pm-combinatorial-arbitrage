import pytest

from feed.rest_client import DEPTH_MIN, VOLUME_24H_MIN, classify_event, build_graph_and_books


def _market(ticker: str, ask: int, bid: int, vol: int) -> dict:
    return {
        "ticker": ticker,
        "yes_ask_size_fp": str(ask),
        "yes_bid_size_fp": str(bid),
        "volume_24h_fp": str(vol),
    }


def _event(event_id: str, markets: list[dict], mx: bool = True) -> dict:
    return {"event_ticker": event_id, "mutually_exclusive": mx, "markets": markets}


# classify_event

def test_classify_hot_when_buy_arb_viable():
    event = _event("E", [
        _market("A", ask=DEPTH_MIN, bid=0, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN, bid=0, vol=VOLUME_24H_MIN),
    ])
    assert classify_event(event) is True


def test_classify_hot_when_sell_arb_viable():
    event = _event("E", [
        _market("A", ask=0, bid=DEPTH_MIN, vol=VOLUME_24H_MIN),
        _market("B", ask=0, bid=DEPTH_MIN, vol=VOLUME_24H_MIN),
    ])
    assert classify_event(event) is True


def test_classify_cold_volume_below_floor():
    # One leg fails volume check
    event = _event("E", [
        _market("A", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN - 1),
    ])
    assert classify_event(event) is False


def test_classify_cold_both_sides_below_depth():
    event = _event("E", [
        _market("A", ask=DEPTH_MIN - 1, bid=DEPTH_MIN - 1, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN - 1, bid=DEPTH_MIN - 1, vol=VOLUME_24H_MIN),
    ])
    assert classify_event(event) is False


def test_classify_cold_bottleneck_leg_fails_depth():
    # Second leg is the bottleneck on both sides
    event = _event("E", [
        _market("A", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN - 1, bid=DEPTH_MIN - 1, vol=VOLUME_24H_MIN),
    ])
    assert classify_event(event) is False


def test_classify_cold_single_market():
    # MX events need at least 2 legs
    event = _event("E", [_market("A", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN)])
    assert classify_event(event) is False


def test_classify_cold_missing_size_fields_treated_as_zero():
    event = {
        "event_ticker": "E",
        "mutually_exclusive": True,
        "markets": [
            {"ticker": "A", "volume_24h_fp": str(VOLUME_24H_MIN)},
            {"ticker": "B", "volume_24h_fp": str(VOLUME_24H_MIN)},
        ],
    }
    assert classify_event(event) is False


def test_classify_accepts_custom_thresholds():
    event = _event("E", [
        _market("A", ask=10, bid=0, vol=5),
        _market("B", ask=10, bid=0, vol=5),
    ])
    assert classify_event(event, depth_min=10, volume_min=5) is True
    assert classify_event(event, depth_min=11, volume_min=5) is False
    assert classify_event(event, depth_min=10, volume_min=6) is False


# build_graph_and_books

def test_build_hot_event_gets_books():
    hot = _event("HOT", [
        _market("A", ask=DEPTH_MIN, bid=0, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN, bid=0, vol=VOLUME_24H_MIN),
    ])
    graph, books, cold_events = build_graph_and_books([hot])
    assert "A" in books and "B" in books
    assert "HOT" not in cold_events


def test_build_cold_event_in_graph_but_not_books():
    cold = _event("COLD", [
        _market("X", ask=0, bid=0, vol=0),
        _market("Y", ask=0, bid=0, vol=0),
    ])
    graph, books, cold_events = build_graph_and_books([cold])
    assert "COLD" in graph.events
    assert "X" not in books and "Y" not in books
    assert "COLD" in cold_events


def test_build_cold_events_dict_contains_correct_tickers():
    cold = _event("COLD", [
        _market("X", ask=0, bid=0, vol=0),
        _market("Y", ask=0, bid=0, vol=0),
    ])
    _, _, cold_events = build_graph_and_books([cold])
    assert sorted(cold_events["COLD"]) == ["X", "Y"]


def test_build_non_mx_event_excluded():
    non_mx = _event("NMX", [
        _market("A", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN),
    ], mx=False)
    graph, books, cold_events = build_graph_and_books([non_mx])
    assert len(graph.events) == 0
    assert len(books) == 0
    assert len(cold_events) == 0


def test_build_single_market_event_excluded():
    one_leg = _event("ONE", [_market("A", ask=DEPTH_MIN, bid=DEPTH_MIN, vol=VOLUME_24H_MIN)])
    graph, books, cold_events = build_graph_and_books([one_leg])
    assert len(graph.events) == 0


def test_build_mixed_events_split_correctly():
    hot = _event("HOT", [
        _market("A", ask=DEPTH_MIN, bid=0, vol=VOLUME_24H_MIN),
        _market("B", ask=DEPTH_MIN, bid=0, vol=VOLUME_24H_MIN),
    ])
    cold = _event("COLD", [
        _market("X", ask=0, bid=0, vol=0),
        _market("Y", ask=0, bid=0, vol=0),
    ])
    graph, books, cold_events = build_graph_and_books([hot, cold])
    assert len(graph.events) == 2
    assert set(books) == {"A", "B"}
    assert "COLD" in cold_events
    assert "HOT" not in cold_events


def test_build_graph_contains_ticker_to_event_for_cold():
    cold = _event("COLD", [
        _market("X", ask=0, bid=0, vol=0),
        _market("Y", ask=0, bid=0, vol=0),
    ])
    graph, _, _ = build_graph_and_books([cold])
    assert graph.ticker_to_event.get("X") == "COLD"
    assert graph.ticker_to_event.get("Y") == "COLD"


class _FakePagingClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.pages.pop(0)


def test_fetch_event_markets_paginates():
    from unittest.mock import patch
    from feed.rest_client import fetch_event_markets
    pages = [
        {"markets": [{"ticker": "M1"}], "cursor": "c1"},
        {"markets": [{"ticker": "M2"}], "cursor": None},
    ]
    client = _FakePagingClient(pages)
    with patch("feed.rest_client.time.sleep") as mock_sleep:
        markets = fetch_event_markets(client, "EVT-X")
    assert [m["ticker"] for m in markets] == ["M1", "M2"]
    assert client.calls[0][1]["event_ticker"] == "EVT-X"
    assert client.calls[1][1]["cursor"] == "c1"


def test_fetch_event_markets_empty_response():
    from feed.rest_client import fetch_event_markets
    client = _FakePagingClient([{"markets": [], "cursor": None}])
    assert fetch_event_markets(client, "EVT-X") == []


def test_logging_retry_emits_structured_warning(caplog):
    import logging
    from feed.rest_client import _LoggingRetry, _RETRY_TOTAL

    retry = _LoggingRetry(total=_RETRY_TOTAL, connect=_RETRY_TOTAL, read=_RETRY_TOTAL, backoff_factor=0)
    with caplog.at_level(logging.WARNING, logger="feed"):
        retry.increment(
            method="GET",
            url="/trade-api/v2/events?status=open&limit=200",
            error=ConnectionResetError(54, "Connection reset by peer"),
        )

    assert "RETRY  source=rest  attempt=1/3" in caplog.text
    assert "path=/trade-api/v2/events" in caplog.text
    assert "status=open" not in caplog.text  # query string stripped


def test_logging_retry_used_by_default_session():
    from feed.rest_client import KalshiRestClient, _LoggingRetry
    client = KalshiRestClient()
    adapter = client.session.get_adapter("https://api.elections.kalshi.com")
    assert isinstance(adapter.max_retries, _LoggingRetry)


# Expiry filter: events past close_time never classify hot

def _liquid_market(ticker, close_time=None):
    from feed.rest_client import DEPTH_MIN, VOLUME_24H_MIN
    m = {
        "ticker": ticker,
        "yes_ask_size_fp": str(DEPTH_MIN),
        "yes_bid_size_fp": "0",
        "volume_24h_fp": str(VOLUME_24H_MIN),
    }
    if close_time is not None:
        m["close_time"] = close_time
    return m


def _mx(markets):
    return {"event_ticker": "E", "mutually_exclusive": True, "markets": markets}


def test_classify_rejects_event_past_close():
    from feed.rest_client import classify_event
    event = _mx([
        _liquid_market("A", "2020-01-01T00:00:00Z"),
        _liquid_market("B", "2020-01-01T00:00:00Z"),
    ])
    assert classify_event(event) is False


def _close_in(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_classify_rejects_if_any_leg_past_close():
    from feed.rest_client import classify_event
    event = _mx([
        _liquid_market("A", _close_in(2)),
        _liquid_market("B", "2020-01-01T00:00:00Z"),
    ])
    assert classify_event(event) is False


def test_classify_accepts_near_future_close():
    from feed.rest_client import classify_event
    event = _mx([
        _liquid_market("A", _close_in(2)),
        _liquid_market("B", _close_in(2)),
    ])
    assert classify_event(event) is True


def test_classify_missing_close_time_not_excluded():
    from feed.rest_client import classify_event
    event = _mx([_liquid_market("A"), _liquid_market("B")])
    assert classify_event(event) is True


def test_classify_unparseable_close_time_not_excluded():
    from feed.rest_client import classify_event
    event = _mx([
        _liquid_market("A", "not-a-timestamp"),
        _liquid_market("B", "also-garbage"),
    ])
    assert classify_event(event) is True


# Pagination pacing: bursts stay under the rate limit

def test_fetch_all_mx_events_pauses_between_pages():
    from unittest.mock import call, patch
    from feed.rest_client import fetch_all_mx_events, _PAGE_PAUSE_S
    pages = [
        {"events": [{"event_ticker": "E1"}], "cursor": "c1"},
        {"events": [{"event_ticker": "E2"}], "cursor": "c2"},
        {"events": [{"event_ticker": "E3"}], "cursor": None},
    ]
    client = _FakePagingClient(pages)
    with patch("feed.rest_client.time.sleep") as mock_sleep:
        events = fetch_all_mx_events(client)
    assert len(events) == 3
    assert mock_sleep.call_args_list == [call(_PAGE_PAUSE_S)] * 2  # 3 pages → 2 pauses


def test_fetch_all_mx_events_single_page_no_pause():
    from unittest.mock import patch
    from feed.rest_client import fetch_all_mx_events
    client = _FakePagingClient([{"events": [], "cursor": None}])
    with patch("feed.rest_client.time.sleep") as mock_sleep:
        fetch_all_mx_events(client)
    mock_sleep.assert_not_called()


def test_fetch_event_markets_pauses_between_pages():
    from unittest.mock import call, patch
    from feed.rest_client import fetch_event_markets, _PAGE_PAUSE_S
    pages = [
        {"markets": [{"ticker": "M1"}], "cursor": "c1"},
        {"markets": [{"ticker": "M2"}], "cursor": None},
    ]
    client = _FakePagingClient(pages)
    with patch("feed.rest_client.time.sleep") as mock_sleep:
        fetch_event_markets(client, "EVT-X")
    assert mock_sleep.call_args_list == [call(_PAGE_PAUSE_S)]


# Tenor filter: no capital locked beyond MAX_TENOR_DAYS

def test_classify_rejects_event_beyond_tenor():
    from feed.rest_client import classify_event
    event = _mx([
        _liquid_market("A", _close_in(30)),
        _liquid_market("B", _close_in(30)),
    ])
    assert classify_event(event) is False


def test_classify_rejects_if_any_leg_beyond_tenor():
    from feed.rest_client import classify_event
    event = _mx([
        _liquid_market("A", _close_in(2)),
        _liquid_market("B", _close_in(30)),
    ])
    assert classify_event(event) is False


def test_classify_accepts_just_inside_tenor():
    from feed.rest_client import classify_event, MAX_TENOR_DAYS
    event = _mx([
        _liquid_market("A", _close_in(MAX_TENOR_DAYS - 0.1)),
        _liquid_market("B", _close_in(MAX_TENOR_DAYS - 0.1)),
    ])
    assert classify_event(event) is True


def test_classify_rejects_just_beyond_tenor():
    from feed.rest_client import classify_event, MAX_TENOR_DAYS
    event = _mx([
        _liquid_market("A", _close_in(MAX_TENOR_DAYS + 0.1)),
        _liquid_market("B", _close_in(MAX_TENOR_DAYS + 0.1)),
    ])
    assert classify_event(event) is False
