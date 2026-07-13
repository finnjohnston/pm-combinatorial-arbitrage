from .types import PRICE_MAX, Fill, FillResult

class BuyQueue:

    def __init__(self) -> None:
        self.real_qty: list[int] = [0] * (PRICE_MAX + 1)
        self.occupied_mask: int = 0

    def _iter_occupied_desc(self):
        mask = self.occupied_mask
        while mask:
            price = mask.bit_length() - 1
            yield price
            mask &= (1 << price) - 1

    def best_price(self) -> int | None:
        if self.occupied_mask == 0:
            return None
        return self.occupied_mask.bit_length() - 1

    def set_real_qty(self, price: int, qty: int) -> None:
        self.real_qty[price] = qty
        if qty > 0:
            self.occupied_mask |= 1 << price
        else:
            self.occupied_mask &= ~(1 << price)

    def apply_real_delta(self, price: int, signed_qty: int) -> None:
        self.set_real_qty(price, self.real_qty[price] + signed_qty)

    def occupied_prices(self) -> list[int]:
        return list(self._iter_occupied_desc())

    def top_levels(self, n: int) -> list[tuple[int, int]]:
        levels = []
        for price in self._iter_occupied_desc():
            if len(levels) >= n:
                break
            levels.append((price, self.real_qty[price]))
        return levels
    
    def simulate_fill(self, qty: int, participation_rate: float = 1.0, min_price: int | None = None) -> FillResult:
        fills = []
        remaining = qty
        for price in self._iter_occupied_desc():
            if remaining <= 0:
                break
            if min_price is not None and price < min_price:
                break  # levels are walked best-first; everything below is worse
            available = int(self.real_qty[price] * participation_rate)
            take = min(available, remaining)
            if take > 0:
                fills.append(Fill(price=price, qty=take))
                remaining -= take
        return FillResult(fills=fills, filled_qty=qty - remaining, unfilled_qty=remaining)