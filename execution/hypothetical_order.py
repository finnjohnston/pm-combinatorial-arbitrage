from book.public_book import PublicMarketBook
from book.types import Fill, FillResult


class HypotheticalOrderError(RuntimeError):
    """Raised on invalid lifecycle usage."""


class HypotheticalTakerOrder:

    def __init__(self, book: PublicMarketBook, side: str, qty: int, participation_rate: float = 1.0,
                 limit_price: int | None = None) -> None:
        self._book = book
        self._side = side
        self._requested_qty = qty

        walk = book.simulate_taker(side, qty, participation_rate, limit_price)
        self._alloc: dict[int, int] = {f.price: f.qty for f in walk.fills}
        self._erosion: dict[int, int] = {price: 0 for price in self._alloc}
        self._resolved = False

        if self._alloc:
            self._book.add_taker_listener(self._side, self._on_decreases)

    def _on_decreases(self, decreases: dict[int, int]) -> None:
        for price, qty_decrease in decreases.items():
            if price in self._erosion:
                self._erosion[price] += qty_decrease

    def cancel(self) -> None:
        """Detach from the book without producing fills (order abandoned, e.g.
        the executing task was cancelled)."""
        if self._resolved:
            return
        self._resolved = True
        if self._alloc:
            self._book.remove_taker_listener(self._side, self._on_decreases)

    def resolve(self) -> FillResult:
        if self._resolved:
            raise HypotheticalOrderError("resolve() called twice")
        self._resolved = True
        if self._alloc:
            self._book.remove_taker_listener(self._side, self._on_decreases)

        fills = []
        filled_qty = 0
        for price, allocated in self._alloc.items():
            realized = max(0, allocated - self._erosion[price])
            if realized > 0:
                fills.append(Fill(price=price, qty=realized))
                filled_qty += realized

        return FillResult(fills=fills, filled_qty=filled_qty, unfilled_qty=self._requested_qty - filled_qty)
