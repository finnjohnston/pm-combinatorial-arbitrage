import random
import pytest
from book.public_book import PublicMarketBook
from book.fees import taker_fee
from execution.config import ExecutionConfig
from execution.executor import execute_tier
from optimizer.opportunity import OpportunityTier


def make_buy_book(yes_ask: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.no_buys.set_real_qty(1000 - yes_ask, qty)
    return b


def make_sell_book(yes_bid: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.yes_buys.set_real_qty(yes_bid, qty)
    return b


def zero_config():
    return ExecutionConfig(min_latency_ms=0, max_latency_ms=0, participation_rate=1.0)


# Basic structure

async def test_execute_tier_returns_execution_result():
    books = {
        "A": make_buy_book(480, 100),
        "B": make_buy_book(480, 100),
    }
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 480, "B": 480},
        capital_required=96000,
        profit=500,
    )
    result = await execute_tier("E", "buy", tier, books, zero_config(), random.Random(1))
    assert result.event_id == "E"
    assert result.side == "buy"
    assert set(result.legs.keys()) == {"A", "B"}


async def test_execute_tier_fills_match_book_depth():
    books = {
        "A": make_buy_book(480, 100),
        "B": make_buy_book(480, 100),
    }
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 480, "B": 480},
        capital_required=96000,
        profit=500,
    )
    result = await execute_tier("E", "buy", tier, books, zero_config(), random.Random(1))
    assert result.legs["A"].filled_qty == 100
    assert result.legs["B"].filled_qty == 100


async def test_hedge_qty_is_min_of_filled():
    """hedge_qty = min(filled across legs), reflected in realized_profit."""
    books = {
        "A": make_buy_book(480, 50),   # only 50 available
        "B": make_buy_book(480, 100),
    }
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 480, "B": 480},
        capital_required=96000,
        profit=500,
    )
    result = await execute_tier("E", "buy", tier, books, zero_config(), random.Random(1))
    hedge_qty = min(leg.filled_qty for leg in result.legs.values())
    assert hedge_qty == 50
    # realized_profit = hedge_qty*1000 - total_capital - total_fees
    total_capital = sum(leg.filled_qty * (leg.avg_price or 0) for leg in result.legs.values())
    total_fees = sum(leg.fee for leg in result.legs.values())
    expected_profit = hedge_qty * 1000 - total_capital - total_fees
    assert result.realized_profit == expected_profit


async def test_realized_profit_formula_buy_side():
    books = {
        "A": make_buy_book(480, 100),
        "B": make_buy_book(480, 100),
    }
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 480, "B": 480},
        capital_required=96000,
        profit=500,
    )
    result = await execute_tier("E", "buy", tier, books, zero_config(), random.Random(1))
    legs = result.legs
    hedge_qty = min(leg.filled_qty for leg in legs.values())
    total_capital = sum(leg.filled_qty * (leg.avg_price or 0) for leg in legs.values())
    total_fees = sum(leg.fee for leg in legs.values())
    assert result.realized_profit == hedge_qty * 1000 - total_capital - total_fees


async def test_leg_fill_has_correct_ticker():
    books = {"A": make_buy_book(480, 100)}
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 480},
        capital_required=48000,
        profit=100,
    )
    result = await execute_tier("E", "buy", tier, books, zero_config(), random.Random(1))
    assert result.legs["A"].ticker == "A"
    assert result.legs["A"].side == "buy"


# Sell side: capital is collateral

async def test_sell_side_total_capital_is_collateral():
    books = {
        "A": make_sell_book(600, 100),
        "B": make_sell_book(600, 100),
    }
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 600, "B": 600},
        capital_required=100 * (2 * 1000 - 1200),
        profit=100,
    )
    result = await execute_tier("E", "sell", tier, books, zero_config(), random.Random(1))
    # 100 filled on each leg at 600: collateral = 200*1000 - 120000 = 80000, positive
    assert result.total_capital == 80_000


async def test_sell_side_realized_profit_formula():
    books = {
        "A": make_sell_book(600, 100),
        "B": make_sell_book(600, 100),
    }
    tier = OpportunityTier(
        quantity=100,
        leg_prices={"A": 600, "B": 600},
        capital_required=80_000,
        profit=100,
    )
    result = await execute_tier("E", "sell", tier, books, zero_config(), random.Random(1))
    total_fees = sum(l.fee for l in result.legs.values())
    # proceeds 120000, hedged 100 sets owe 100*1000 at settlement
    assert result.realized_profit == 120_000 - 100_000 - total_fees


# Cancellation safety 

async def test_execute_leg_cancellation_detaches_listener():
    import asyncio
    from execution.executor import execute_leg
    book = make_buy_book(480, 100)
    config = ExecutionConfig(min_latency_ms=50, max_latency_ms=50, participation_rate=1.0)

    task = asyncio.create_task(
        execute_leg("A", "buy", 100, 480, book, config, random.Random(1), 0.0)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert book.listener_count("buy") == 0


# Limit-price execution 

async def test_execute_leg_respects_limit_price():
    from execution.executor import execute_leg
    b = PublicMarketBook()
    b.no_buys.set_real_qty(520, 100)   # YES ask 480
    b.no_buys.set_real_qty(100, 1000)  # YES ask 900

    leg = await execute_leg("A", "buy", 500, 480, b, zero_config(), random.Random(1), 0.0,
                            limit_price=480)

    assert leg.filled_qty == 100
    assert leg.avg_price == 480.0
