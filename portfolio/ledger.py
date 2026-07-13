from execution.models import ExecutionResult
from optimizer.optimizer import Optimizer
from risk.models import ResolutionResult

from .db import TradeDB
from .positions import PositionTracker


def _position_holdings(result: ExecutionResult, resolution: ResolutionResult) -> dict[str, int]:
    if resolution.final_holdings:
        return dict(resolution.final_holdings)
    # fallback for resolutions that predate holdings tracking: assume hedged
    return {ticker: resolution.final_hedge_qty for ticker in result.legs}


def _fees_total(result: ExecutionResult, resolution: ResolutionResult) -> int:
    return sum(leg.fee for leg in result.legs.values()) + resolution.resolution_fees


class Ledger:

    def __init__(self, initial_capital: int, optimizer: Optimizer, db: TradeDB | None = None) -> None:
        self._optimizer = optimizer
        self._positions = PositionTracker()
        self._db = db
        self.available_capital = initial_capital
        self.locked_capital = 0

    @property
    def total_capital(self) -> int:
        return self.available_capital + self.locked_capital

    def has_open_position(self, event_id: str) -> bool:
        return self._positions.is_open(event_id)

    def open_event_ids(self) -> set[str]:
        return set(self._positions.open)

    def get_position(self, event_id: str):
        return self._positions.open.get(event_id)

    def record_unwound(self, event_id: str, result: ExecutionResult, resolution: ResolutionResult) -> None:
        self._optimizer.release(event_id)
        self.available_capital += resolution.total_realized_profit
        if self._db is not None:
            self._db.record_unwound(event_id, result, resolution)
            self._db.snapshot_capital(self.total_capital, self.locked_capital, self.available_capital)

    def record_open(self, event_id: str, result: ExecutionResult, resolution: ResolutionResult) -> None:
        holdings = _position_holdings(result, resolution)
        fees_total = _fees_total(result, resolution)
        self._positions.open_position(event_id, result, resolution, holdings=holdings, fees_total=fees_total)
        record = self._positions.open[event_id]
        cost = record.cost_basis
        self.available_capital -= cost
        self.locked_capital += cost
        self._optimizer.release(event_id)

        if self._db is not None:
            self._db.record_open(event_id, result, resolution, capital_deployed=cost,
                                 naked_qty=record.naked_qty)
            self._db.snapshot_capital(self.total_capital, self.locked_capital, self.available_capital)

    def record_close(self, event_id: str, winner_ticker: str) -> int:
        if not self._positions.is_open(event_id):
            return 0

        record = self._positions.open[event_id]
        cost = record.cost_basis
        exec_profit = record.result.realized_profit
        payout = record.settlement_payout(winner_ticker)
        total_profit = payout - cost - record.fees_total

        self._positions.close_position(event_id, winner_ticker)
        self.locked_capital -= cost
        self.available_capital += cost + total_profit
        self._optimizer.release(event_id)

        if self._db is not None:
            self._db.record_close(event_id, winner_ticker, exec_profit, total_profit)
            self._db.snapshot_capital(self.total_capital, self.locked_capital, self.available_capital)

        return total_profit

    def record_repair(self, event_id: str, result: ExecutionResult, resolution: ResolutionResult) -> int:
        if not self._positions.is_open(event_id):
            return 0
        old_cost = self._positions.open[event_id].cost_basis

        holdings = _position_holdings(result, resolution)
        flat = all(q == 0 for q in holdings.values()) if holdings else True

        if flat:
            # fully liquidated: cash P&L is realized now, no settlement dependence
            self._positions.close_position(event_id, "")
            self.locked_capital -= old_cost
            self.available_capital += old_cost + resolution.total_realized_profit
            self._optimizer.release(event_id)
            if self._db is not None:
                self._db.record_close(event_id, "membership-unwound", resolution.total_realized_profit, resolution.total_realized_profit)
                self._db.snapshot_capital(self.total_capital, self.locked_capital, self.available_capital)
        else:
            # completed or partially unwound: replace the record, adjust locked cost
            fees_total = _fees_total(result, resolution)
            self._positions.open_position(event_id, result, resolution, holdings=holdings, fees_total=fees_total)
            new_cost = self._positions.open[event_id].cost_basis
            delta = new_cost - old_cost
            self.available_capital -= delta
            self.locked_capital += delta
            if self._db is not None:
                self._db.snapshot_capital(self.total_capital, self.locked_capital, self.available_capital)

        return resolution.total_realized_profit

    def force_settle_all(self) -> int:
        total_profit = 0
        for event_id, record in list(self._positions.open.items()):
            cost = record.cost_basis
            profit = record.worst_case_payout() - cost - record.fees_total

            self._positions.close_position(event_id, "")
            self.locked_capital -= cost
            self.available_capital += cost + profit
            self._optimizer.release(event_id)

            if self._db is not None:
                self._db.force_settle(event_id, profit)

            total_profit += profit

        return total_profit
