from .event_state import EventState


class StateGraph:

    def __init__(self) -> None:
        self.events: dict[str, EventState] = {}
        self.ticker_to_event: dict[str, str] = {}

    def add_event(self, event_id: str, tickers: list[str]) -> None:
        for ticker in tickers:
            existing = self.ticker_to_event.get(ticker)
            if existing is not None and existing != event_id:
                raise ValueError(
                    f"{ticker!r} is already registered to event {existing!r}"
                )
        self.events[event_id] = EventState(frozenset(tickers))
        for ticker in tickers:
            self.ticker_to_event[ticker] = event_id

    def update_market(self, ticker: str, yes_bid: int | None, yes_ask: int | None) -> None:
        event_id = self.ticker_to_event[ticker]
        self.events[event_id].update_market(ticker, yes_bid, yes_ask)
