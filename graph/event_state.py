class EventState:

    def __init__(self, tickers: frozenset[str]) -> None:
        self.tickers = tickers
        self.prices: dict[str, tuple[int | None, int | None]] = {
            ticker: (None, None) for ticker in tickers
        }
        self.sum_yes_bid: int = 0
        self.sum_yes_ask: int = 0
        self.missing_bid_count: int = len(tickers)
        self.missing_ask_count: int = len(tickers)

    def update_market(self, ticker: str, yes_bid: int | None, yes_ask: int | None) -> None:
        if ticker not in self.tickers:
            raise KeyError(f"{ticker!r} is not a member of this event")

        old_bid, old_ask = self.prices[ticker]

        if old_bid is not None:
            self.sum_yes_bid -= old_bid
        if old_bid is None and yes_bid is not None:
            self.missing_bid_count -= 1
        elif old_bid is not None and yes_bid is None:
            self.missing_bid_count += 1
        if yes_bid is not None:
            self.sum_yes_bid += yes_bid

        if old_ask is not None:
            self.sum_yes_ask -= old_ask
        if old_ask is None and yes_ask is not None:
            self.missing_ask_count -= 1
        elif old_ask is not None and yes_ask is None:
            self.missing_ask_count += 1
        if yes_ask is not None:
            self.sum_yes_ask += yes_ask

        self.prices[ticker] = (yes_bid, yes_ask)

    def total_buy_cost(self) -> int | None:
        if self.missing_ask_count > 0:
            return None
        return self.sum_yes_ask

    def total_sell_proceeds(self) -> int | None:
        if self.missing_bid_count > 0:
            return None
        return self.sum_yes_bid
