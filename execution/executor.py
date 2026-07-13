import asyncio
import random
import time

from book.fees import taker_fee_order
from book.public_book import PublicMarketBook
from optimizer.opportunity import OpportunityTier

from .config import ExecutionConfig
from .hypothetical_order import HypotheticalTakerOrder
from .models import ExecutionResult, LegFill


def monotonic_ms() -> float:
    return time.monotonic() * 1000


async def execute_leg(ticker: str, side: str, qty: int, expected_price: int, book: PublicMarketBook, config: ExecutionConfig, rng: random.Random, start: float,
                      limit_price: int | None = None) -> LegFill:
    t0_rel = monotonic_ms() - start
    latency = rng.uniform(config.min_latency_ms, config.max_latency_ms)

    order = HypotheticalTakerOrder(book, side, qty, config.participation_rate, limit_price)
    try:
        await asyncio.sleep(latency / 1000)
    except BaseException:
        order.cancel()  # detach the book listener if this task is cancelled
        raise
    result = order.resolve()

    fee = taker_fee_order(result.fills)

    return LegFill(
        ticker=ticker, 
        side=side,
        requested_qty=qty, 
        filled_qty=result.filled_qty, 
        unfilled_qty=result.unfilled_qty,
        avg_price=result.avg_price, 
        expected_price=expected_price, 
        fee=fee,
        latency_ms=latency, timestamp_ms=t0_rel + latency,
    )


async def execute_tier(event_id: str, side: str, tier: OpportunityTier, books: dict[str, PublicMarketBook], config: ExecutionConfig, rng: random.Random | None = None) -> ExecutionResult:
    rng = rng or random.Random()
    start = monotonic_ms()

    leg_fills = await asyncio.gather(*(
        execute_leg(ticker, side, tier.quantity, price, books[ticker], config, rng, start)
        for ticker, price in tier.leg_prices.items()
    ))

    legs = {lf.ticker: lf for lf in leg_fills}
    return _build_result(event_id, side, tier, legs, start)


def _build_result(event_id: str, side: str, tier: OpportunityTier, legs: dict[str, LegFill], start: float) -> ExecutionResult:
    gross_cash = int(round(sum(leg.filled_qty * (leg.avg_price or 0) for leg in legs.values())))
    total_fees = sum(leg.fee for leg in legs.values())
    hedge_qty = min((leg.filled_qty for leg in legs.values()), default=0)

    if side == "buy":
        total_capital = gross_cash
        realized_profit = hedge_qty * 1000 - gross_cash - total_fees
    else:
        # Selling YES == buying NO: capital consumed is the NO-side collateral
        # (1000 - price per filled unit), so total_capital is a positive outflow
        # for both sides.
        total_filled = sum(leg.filled_qty for leg in legs.values())
        total_capital = total_filled * 1000 - gross_cash
        realized_profit = gross_cash - hedge_qty * 1000 - total_fees

    return ExecutionResult(
        event_id=event_id,
        side=side,
        target_qty=tier.quantity,
        legs=legs,
        total_capital=total_capital,
        estimated_profit=tier.profit,
        realized_profit=realized_profit,
        wall_clock_ms=monotonic_ms() - start,
    )
