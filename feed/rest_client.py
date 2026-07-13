import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from book.public_book import PublicMarketBook
from graph.graph import StateGraph

logger = logging.getLogger("feed")

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

DEPTH_MIN = 250
VOLUME_24H_MIN = 500
# maximum time-to-settlement: bounds capital lockup and exposure to Kalshi
# adding markets to an event while we hold positions in it
MAX_TENOR_DAYS = 14

_RETRY_TOTAL = 3
_RETRY_BACKOFF = 0.5
_REQUEST_TIMEOUT_S = 10
# pause between paginated requests so bursts stay under Kalshi's rate limit
_PAGE_PAUSE_S = 0.15


class _LoggingRetry(Retry):
    """Retry policy that reports attempts in the engine's log format instead of
    urllib3's internal repr."""

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        new_retry = super().increment(method=method, url=url, response=response, error=error,
                                      _pool=_pool, _stacktrace=_stacktrace)
        attempt = _RETRY_TOTAL - (new_retry.total if new_retry.total is not None else 0)
        if error is not None:
            reason = repr(error)
        elif response is not None:
            reason = f"status={response.status}"
        else:
            reason = "unknown"
        path = (url or "?").split("?")[0]
        logger.warning("RETRY  source=rest  attempt=%d/%d  reason=%s  path=%s",
                       attempt, _RETRY_TOTAL, reason, path)
        return new_retry


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = _LoggingRetry(
        total=_RETRY_TOTAL,
        connect=_RETRY_TOTAL,
        read=_RETRY_TOTAL,
        backoff_factor=_RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class KalshiRestClient:

    def __init__(self, base_url: str = BASE_URL, session: requests.Session | None = None) -> None:
        self.base_url = base_url
        self.session = session or _build_session()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=_REQUEST_TIMEOUT_S)
        response.raise_for_status()
        return response.json()


def fetch_mx_events(client: KalshiRestClient, cursor: str | None = None) -> dict:
    params: dict[str, Any] = {
        "status": "open",
        "with_nested_markets": True,
        "mutually_exclusive": True,
        "limit": 200,
    }
    if cursor:
        params["cursor"] = cursor
    return client.get("/events", params=params)


def fetch_all_mx_events(client: KalshiRestClient) -> list[dict]:
    events: list[dict] = []
    cursor: str | None = None
    while True:
        page = fetch_mx_events(client, cursor)
        events.extend(page.get("events", []))
        cursor = page.get("cursor")
        if not cursor:
            break
        time.sleep(_PAGE_PAUSE_S)
    return events


def fetch_event_markets(client: KalshiRestClient, event_ticker: str) -> list[dict]:
    """Fetch all markets of one event regardless of status (used to reconcile
    settlements the websocket missed)."""
    markets: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"event_ticker": event_ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/markets", params=params)
        markets.extend(page.get("markets", []))
        cursor = page.get("cursor")
        if not cursor:
            break
        time.sleep(_PAGE_PAUSE_S)
    return markets


def _fp_to_qty(fp_str: str | None) -> int:
    try:
        return int(float(fp_str or 0))
    except (ValueError, TypeError):
        return 0


def _market_close_time(market: dict) -> datetime | None:
    close_str = market.get("close_time")
    if not close_str:
        return None
    try:
        return datetime.fromisoformat(close_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def classify_event(event: dict, depth_min: int = DEPTH_MIN, volume_min: int = VOLUME_24H_MIN,
                   now: datetime | None = None) -> bool:
    markets = event.get("markets", [])
    if len(markets) < 2:
        return False

    # A market past its close time is a settlement-pending zombie: whatever the
    # book displays is stale, and one dead leg kills the whole arb anyway.
    # A market closing beyond the tenor limit locks capital for too long.
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=MAX_TENOR_DAYS)
    for market in markets:
        close = _market_close_time(market)
        if close is None:
            continue
        if close <= now or close > horizon:
            return False

    ask_sizes: list[int] = []
    bid_sizes: list[int] = []
    volumes: list[int] = []

    for m in markets:
        ask_sizes.append(_fp_to_qty(m.get("yes_ask_size_fp")))
        bid_sizes.append(_fp_to_qty(m.get("yes_bid_size_fp")))
        volumes.append(_fp_to_qty(m.get("volume_24h_fp")))

    if min(volumes) < volume_min:
        return False

    buy_arb_viable = min(ask_sizes) >= depth_min
    sell_arb_viable = min(bid_sizes) >= depth_min
    return buy_arb_viable or sell_arb_viable


def build_graph_and_books(
    events: list[dict],
    depth_min: int = DEPTH_MIN,
    volume_min: int = VOLUME_24H_MIN,
) -> tuple[StateGraph, dict[str, PublicMarketBook], dict[str, list[str]]]:
    """Build the state graph and hot books.

    Returns:
        graph: all MX events (hot + cold) for settlement tracking
        books: books for hot tickers only (subscribed via websocket)
        cold_events: mapping of cold event_id -> tickers; cold tickers get no
            websocket subscription — they are tracked via the discovery poll
            (and its REST reconciliation) until promoted
    """
    graph = StateGraph()
    books: dict[str, PublicMarketBook] = {}
    cold_events: dict[str, list[str]] = {}

    for event in events:
        if not event.get("mutually_exclusive"):
            continue
        event_id = event["event_ticker"]
        tickers = [m["ticker"] for m in event.get("markets", [])]
        if len(tickers) < 2:
            continue
        graph.add_event(event_id, tickers)
        if classify_event(event, depth_min, volume_min):
            for ticker in tickers:
                books[ticker] = PublicMarketBook()
        else:
            cold_events[event_id] = tickers

    return graph, books, cold_events
