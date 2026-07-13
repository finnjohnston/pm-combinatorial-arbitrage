from dataclasses import dataclass


@dataclass(frozen=True)
class LegFill:
    ticker: str
    side: str
    requested_qty: int
    filled_qty: int
    unfilled_qty: int
    avg_price: float | None
    expected_price: int
    fee: int
    latency_ms: float
    timestamp_ms: float

    @property
    def slippage_ticks(self) -> float:
        if self.avg_price is None:
            return 0.0
        if self.side == "buy":
            return self.avg_price - self.expected_price
        return self.expected_price - self.avg_price


@dataclass
class ExecutionResult:
    event_id: str
    side: str
    target_qty: int
    legs: dict[str, LegFill]
    total_capital: int = 0
    estimated_profit: int = 0
    realized_profit: int = 0
    wall_clock_ms: float = 0.0
