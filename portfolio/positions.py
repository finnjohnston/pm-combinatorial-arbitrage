from dataclasses import dataclass, field

from execution.models import ExecutionResult
from risk.models import ResolutionResult


@dataclass
class PositionRecord:
    event_id: str
    result: ExecutionResult
    resolution: ResolutionResult
    # net per-leg quantity actually held (may be ragged after failed unwinds)
    holdings: dict[str, int] = field(default_factory=dict)
    # all fees already incurred (execution + resolution)
    fees_total: int = 0

    @property
    def side(self) -> str:
        return self.result.side

    @property
    def hedge_qty(self) -> int:
        return self.resolution.final_hedge_qty

    @property
    def cost_basis(self) -> int:
        return self.result.total_capital + self.resolution.resolution_capital

    @property
    def naked_qty(self) -> int:
        if not self.holdings:
            return 0
        hedge = min(self.holdings.values())
        return sum(q - hedge for q in self.holdings.values())

    def settlement_payout(self, winner_ticker: str) -> int:
        if self.side == "buy":
            return self.holdings.get(winner_ticker, 0) * 1000
        # sell side holds NO on each leg: every losing leg pays out
        total = sum(self.holdings.values())
        return (total - self.holdings.get(winner_ticker, 0)) * 1000

    def worst_case_payout(self) -> int:
        if not self.holdings:
            return 0
        if self.side == "buy":
            return min(self.holdings.values()) * 1000
        total = sum(self.holdings.values())
        return (total - max(self.holdings.values())) * 1000


class PositionTracker:

    def __init__(self) -> None:
        self._open: dict[str, PositionRecord] = {}

    def open_position(self, event_id: str, result: ExecutionResult, resolution: ResolutionResult,
                      holdings: dict[str, int] | None = None, fees_total: int = 0) -> None:
        self._open[event_id] = PositionRecord(
            event_id=event_id,
            result=result,
            resolution=resolution,
            holdings=dict(holdings or {}),
            fees_total=fees_total,
        )

    def close_position(self, event_id: str, winner_ticker: str) -> int:
        record = self._open.pop(event_id)
        return record.resolution.total_realized_profit

    @property
    def open(self) -> dict[str, PositionRecord]:
        return dict(self._open)

    def is_open(self, event_id: str) -> bool:
        return event_id in self._open
