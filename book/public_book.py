from typing import Callable

from .buy_queue import BuyQueue
from .types import Fill, FillResult

class PublicMarketBook:

    def __init__(self) -> None:
        self.yes_buys = BuyQueue()
        self.no_buys = BuyQueue()
        self._yes_listeners: list[Callable[[dict[int, int]], None]] = []
        self._no_listeners: list[Callable[[dict[int, int]], None]] = []

    def load_snapshot(self, yes_levels: dict[int, int], no_levels: dict[int, int]) -> dict[str, dict[int, int]]:
        yes_decreases = self._load_side(self.yes_buys, yes_levels)
        no_decreases = self._load_side(self.no_buys, no_levels)

        if yes_decreases and self._yes_listeners:
            for callback in list(self._yes_listeners):
                callback(yes_decreases)
        if no_decreases and self._no_listeners:
            converted = {1000 - price: qty for price, qty in no_decreases.items()}
            for callback in list(self._no_listeners):
                callback(converted)

        return {"yes": yes_decreases, "no": no_decreases}

    def _resolve_taker_side(self, side: str) -> tuple[BuyQueue, bool]:
        if side == "buy":
            return self.no_buys, True
        if side == "sell":
            return self.yes_buys, False
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    def simulate_taker(self, side: str, qty: int, participation_rate: float = 1.0,
                       limit_price: int | None = None) -> FillResult:
        """limit_price is in YES terms: max acceptable price for buys, min
        acceptable price for sells."""
        queue, convert = self._resolve_taker_side(side)
        min_price = None
        if limit_price is not None:
            min_price = 1000 - limit_price if convert else limit_price
        result = queue.simulate_fill(qty, participation_rate, min_price)
        if not convert:
            return result
        return FillResult(
            fills=[Fill(price=1000 - f.price, qty=f.qty) for f in result.fills],
            filled_qty=result.filled_qty, unfilled_qty=result.unfilled_qty,
        )

    def add_taker_listener(self, side: str, callback: Callable[[dict[int, int]], None]) -> None:
        self._listeners_for(side).append(callback)

    def remove_taker_listener(self, side: str, callback: Callable[[dict[int, int]], None]) -> None:
        self._listeners_for(side).remove(callback)

    def listener_count(self, side: str) -> int:
        return len(self._listeners_for(side))

    def _listeners_for(self, side: str) -> list[Callable[[dict[int, int]], None]]:
        if side == "buy":
            return self._no_listeners
        if side == "sell":
            return self._yes_listeners
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    @staticmethod
    def _load_side(queue: BuyQueue, new_levels: dict[int, int]) -> dict[int, int]:
        decreases = {}
        touched = set(new_levels) | set(queue.occupied_prices())
        for price in touched:
            old = queue.real_qty[price]
            new = new_levels.get(price, 0)
            if new < old:
                decreases[price] = old - new
            if new != old:
                queue.set_real_qty(price, new)
        return decreases

    def apply_delta(self, side: str, price: int, delta: int) -> None:
        if side == "yes":
            self.yes_buys.apply_real_delta(price, delta)
            if delta < 0 and self._yes_listeners:
                for callback in list(self._yes_listeners):
                    callback({price: abs(delta)})
        elif side == "no":
            self.no_buys.apply_real_delta(price, delta)
            if delta < 0 and self._no_listeners:
                for callback in list(self._no_listeners):
                    callback({1000 - price: abs(delta)})
        else:
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    def best_yes_bid(self) -> int | None:
        return self.yes_buys.best_price()

    def best_no_bid(self) -> int | None:
        return self.no_buys.best_price()

    def best_yes_ask(self) -> int | None:
        price = self.no_buys.best_price()
        return None if price is None else 1000 - price

    def best_no_ask(self) -> int | None:
        price = self.yes_buys.best_price()
        return None if price is None else 1000 - price

    def mid_price_yes(self) -> float | None:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def mid_price_no(self) -> float | None:
        bid, ask = self.best_no_bid(), self.best_no_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2
    
    def spread(self) -> int | None:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def snapshot(self, depth: int = 10) -> dict[str, list[tuple[int, int]]]:
        return {
            "yes_bids": self.yes_buys.top_levels(depth),
            "yes_asks": [(1000 - price, qty) for price, qty in self.no_buys.top_levels(depth)],
            "no_bids": self.no_buys.top_levels(depth),
            "no_asks": [(1000 - price, qty) for price, qty in self.yes_buys.top_levels(depth)],
        }
