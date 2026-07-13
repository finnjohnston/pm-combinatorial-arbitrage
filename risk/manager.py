import asyncio
import random
from typing import Callable

from book.fees import taker_fee_order
from book.public_book import PublicMarketBook
from execution.config import ExecutionConfig
from execution.executor import execute_leg, monotonic_ms
from execution.models import ExecutionResult, LegFill

from .models import ResolutionResult

# Below 10 contracts, Kalshi's per-order cent-ceiled fee dominates any possible
# arb edge, so completing a hedge can never pay for itself.
_MIN_COMPLETION_QTY = 1000


class RiskManager:

    def __init__(self, books: dict[str, PublicMarketBook], config: ExecutionConfig, rng: random.Random | None = None,
                 capital_provider: Callable[[], int] | None = None) -> None:
        self._books = books
        self._config = config
        self._participation_rate = config.participation_rate
        self._rng = rng or random.Random()
        self._capital_provider = capital_provider

    def _completion_budget(self, result: ExecutionResult, budget: int | None) -> int | None:
        """Cash available for completion buys. On the real exchange an order that
        exceeds the balance is rejected, so completion spending must fit in what
        is left after the base trade's own outflow."""
        if budget is not None:
            return budget
        if self._capital_provider is None:
            return None
        return max(0, self._capital_provider() - max(result.total_capital, 0))

    async def handle(self, result: ExecutionResult, budget: int | None = None) -> ResolutionResult:
        holdings: dict[str, int] = {t: lf.filled_qty for t, lf in result.legs.items()}
        resolution_legs: list[LegFill] = []
        resolution_capital = 0
        resolution_fees = 0
        resolution_net_fill = 0
        did_complete = False
        did_unwind = False
        completion_budget = self._completion_budget(result, budget)

        while True:
            hedge_qty, unhedged = self._compute_unhedged(holdings)
            if not unhedged:
                break

            completion_pnl = self._eval_completion_pnl(result.side, holdings, hedge_qty)
            unwind_pnl = self._eval_unwind_pnl(result.side, holdings, hedge_qty)

            remaining_budget = None
            if completion_budget is not None:
                remaining_budget = max(0, completion_budget - max(resolution_capital, 0))

            # complete only when it locks a positive P&L on a position big enough
            # to clear the fee floor; otherwise unwind rather than add exposure
            can_complete = (
                max(holdings.values()) >= _MIN_COMPLETION_QTY
                and completion_pnl > 0
                and completion_pnl >= unwind_pnl
            )

            if can_complete:
                new_legs = await self._execute_completion(result.side, holdings, remaining_budget)
                if not any(leg.filled_qty > 0 for leg in new_legs):
                    unwind_legs = await self._execute_unwind(result.side, holdings, hedge_qty)
                    resolution_legs.extend(unwind_legs)
                    resolution_capital, resolution_fees, net_fill = self._apply_legs(
                        result.side, "unwind", unwind_legs,
                        holdings, resolution_capital, resolution_fees,
                    )
                    resolution_net_fill += net_fill
                    did_unwind = True
                    break

                resolution_capital, resolution_fees, net_fill = self._apply_legs(
                    result.side, "complete", new_legs,
                    holdings, resolution_capital, resolution_fees,
                )
                resolution_net_fill += net_fill
                resolution_legs.extend(new_legs)
                did_complete = True
            else:
                new_legs = await self._execute_unwind(result.side, holdings, hedge_qty)
                resolution_capital, resolution_fees, net_fill = self._apply_legs(
                    result.side, "unwind", new_legs,
                    holdings, resolution_capital, resolution_fees,
                )
                resolution_net_fill += net_fill
                resolution_legs.extend(new_legs)
                did_unwind = True
                break

        if did_complete and did_unwind:
            action = "partial"
        elif did_complete:
            action = "completed"
        elif did_unwind:
            action = "unwound"
        else:
            action = "none"

        final_hedge_qty = min(holdings.values())
        original_hedge_qty = min(lf.filled_qty for lf in result.legs.values())
        delta_hedge = final_hedge_qty - original_hedge_qty

        if result.side == "buy":
            resolution_profit = delta_hedge * 1000 - resolution_capital - resolution_fees
        else:
            # resolution_capital is collateral outflow (see _apply_legs); recover
            # cash proceeds via the net filled quantity to price the hedge change.
            resolution_profit = (resolution_net_fill - delta_hedge) * 1000 - resolution_capital - resolution_fees

        return ResolutionResult(
            original_result=result,
            action=action,
            resolution_legs=resolution_legs,
            final_hedge_qty=final_hedge_qty,
            resolution_capital=resolution_capital,
            resolution_fees=resolution_fees,
            total_realized_profit=result.realized_profit + resolution_profit,
            final_holdings=dict(holdings),
        )

    @staticmethod
    def _compute_unhedged(holdings: dict[str, int]) -> tuple[int, dict[str, int]]:
        hedge_qty = min(holdings.values())
        unhedged = {t: q - hedge_qty for t, q in holdings.items() if q > hedge_qty}
        return hedge_qty, unhedged

    def _eval_completion_pnl(self, side: str, holdings: dict[str, int], hedge_qty: int) -> int:
        max_qty = max(holdings.values())
        short_legs = {t: max_qty - q for t, q in holdings.items() if q < max_qty}
        if not short_legs:
            return 0

        sim_results = {
            ticker: self._books[ticker].simulate_taker(side, gap, self._participation_rate)
            for ticker, gap in short_legs.items()
        }

        new_holdings = dict(holdings)
        for ticker, sim in sim_results.items():
            new_holdings[ticker] += sim.filled_qty

        additional_sets = min(new_holdings.values()) - hedge_qty
        cost = sum(self._fills_cost(sim.fills) for sim in sim_results.values())
        fees = sum(self._fills_fees(sim.fills) for sim in sim_results.values())

        if side == "buy":
            return additional_sets * 1000 - cost - fees
        else:
            return cost - additional_sets * 1000 - fees

    def _eval_unwind_pnl(self, side: str, holdings: dict[str, int], hedge_qty: int) -> int:
        unwind_side = "sell" if side == "buy" else "buy"
        total = 0
        for ticker, qty in holdings.items():
            excess = qty - hedge_qty
            if excess <= 0:
                continue
            sim = self._books[ticker].simulate_taker(unwind_side, excess, self._participation_rate)
            cost = self._fills_cost(sim.fills)
            fees = self._fills_fees(sim.fills)
            if side == "buy":
                total += cost - fees
            else:
                total -= cost + fees
        return total

    def _cap_gaps_to_budget(self, side: str, short_legs: dict[str, int], budget: int) -> dict[str, int]:
        est_outflow = 0
        for ticker, gap in short_legs.items():
            sim = self._books[ticker].simulate_taker(side, gap, self._participation_rate)
            cost = self._fills_cost(sim.fills)
            if side == "buy":
                est_outflow += cost
            else:
                est_outflow += sim.filled_qty * 1000 - cost
        if est_outflow <= budget:
            return short_legs
        if est_outflow == 0:
            return short_legs
        capped = {t: gap * budget // est_outflow for t, gap in short_legs.items()}
        return {t: gap for t, gap in capped.items() if gap > 0}

    def _completion_limit(self, side: str, ticker: str, gap: int) -> int | None:
        sim = self._books[ticker].simulate_taker(side, gap, self._participation_rate)
        if not sim.fills:
            return None
        prices = [f.price for f in sim.fills]
        return max(prices) if side == "buy" else min(prices)

    async def _execute_completion(self, side: str, holdings: dict[str, int], budget: int | None = None) -> list[LegFill]:
        max_qty = max(holdings.values())
        short_legs = {t: max_qty - q for t, q in holdings.items() if q < max_qty}
        if budget is not None:
            short_legs = self._cap_gaps_to_budget(side, short_legs, budget)
            if not short_legs:
                return []
        limits = {t: self._completion_limit(side, t, gap) for t, gap in short_legs.items()}
        start = monotonic_ms()
        leg_fills = await asyncio.gather(*(
            execute_leg(
                ticker=ticker,
                side=side,
                qty=gap,
                expected_price=self._expected_price(ticker, side),
                book=self._books[ticker],
                config=self._config,
                rng=self._rng,
                start=start,
                limit_price=limits[ticker],
            )
            for ticker, gap in short_legs.items()
        ))
        return list(leg_fills)

    async def _execute_unwind(self, side: str, holdings: dict[str, int], hedge_qty: int) -> list[LegFill]:
        unwind_side = "sell" if side == "buy" else "buy"
        over_legs = {t: q - hedge_qty for t, q in holdings.items() if q > hedge_qty}
        start = monotonic_ms()
        leg_fills = await asyncio.gather(*(
            execute_leg(
                ticker=ticker,
                side=unwind_side,
                qty=excess,
                expected_price=self._expected_price(ticker, unwind_side),
                book=self._books[ticker],
                config=self._config,
                rng=self._rng,
                start=start,
            )
            for ticker, excess in over_legs.items()
        ))
        return list(leg_fills)

    def _expected_price(self, ticker: str, side: str) -> int:
        book = self._books[ticker]
        price = book.best_yes_ask() if side == "buy" else book.best_yes_bid()
        return price or 0

    @staticmethod
    def _apply_legs(side: str, action: str, legs: list[LegFill], holdings: dict[str, int], capital: int, fees: int) -> tuple[int, int, int]:
        # capital is a positive cash outflow for both sides: buys cost the fill
        # price, sells cost the NO-side collateral (1000 - price) per unit.
        net_fill = 0
        for leg in legs:
            leg_cost = round(leg.filled_qty * (leg.avg_price or 0))
            if action == "complete":
                holdings[leg.ticker] += leg.filled_qty
                net_fill += leg.filled_qty
                capital += leg_cost if side == "buy" else leg.filled_qty * 1000 - leg_cost
            else:
                holdings[leg.ticker] -= leg.filled_qty
                net_fill -= leg.filled_qty
                capital += -leg_cost if side == "buy" else leg_cost - leg.filled_qty * 1000
            fees += leg.fee
        return capital, fees, net_fill

    @staticmethod
    def _fills_cost(fills) -> int:
        return sum(f.price * f.qty for f in fills)

    @staticmethod
    def _fills_fees(fills) -> int:
        return taker_fee_order(fills)
