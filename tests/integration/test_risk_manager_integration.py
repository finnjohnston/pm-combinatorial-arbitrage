import random
import pytest
from book.public_book import PublicMarketBook
from execution.config import ExecutionConfig
from execution.models import ExecutionResult, LegFill
from risk.manager import RiskManager


def zero_config():
    return ExecutionConfig(min_latency_ms=0, max_latency_ms=0, participation_rate=1.0)


def make_leg(ticker, filled_qty, avg_price, side="buy", fee=0):
    return LegFill(
        ticker=ticker,
        side=side,
        requested_qty=filled_qty,
        filled_qty=filled_qty,
        unfilled_qty=0,
        avg_price=float(avg_price),
        expected_price=avg_price,
        fee=fee,
        latency_ms=0.0,
        timestamp_ms=0.0,
    )


def make_result(event_id, side, legs: dict, realized_profit=0):
    return ExecutionResult(
        event_id=event_id,
        side=side,
        target_qty=100,
        legs=legs,
        total_capital=sum(round(l.filled_qty * (l.avg_price or 0)) for l in legs.values()),
        estimated_profit=realized_profit,
        realized_profit=realized_profit,
    )


def make_book_with_yes_ask(yes_ask: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.no_buys.set_real_qty(1000 - yes_ask, qty)
    return b


def make_book_with_yes_bid(yes_bid: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.yes_buys.set_real_qty(yes_bid, qty)
    return b


def make_full_book(yes_bid: int, yes_ask: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.yes_buys.set_real_qty(yes_bid, qty)
    b.no_buys.set_real_qty(1000 - yes_ask, qty)
    return b


async def test_balanced_fills_action_none():
    books = {
        "A": make_full_book(450, 550, 200),
        "B": make_full_book(450, 550, 200),
    }
    legs = {
        "A": make_leg("A", 100, 480),
        "B": make_leg("B", 100, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=500)
    rm = RiskManager(books, zero_config(), random.Random(42))
    res = await rm.handle(result)
    assert res.action == "none"
    assert res.total_realized_profit == result.realized_profit


async def test_unequal_fills_completing_profitable_action_completed():
    books = {
        "A": make_full_book(yes_bid=450, yes_ask=480, qty=20000),
        "B": make_full_book(yes_bid=450, yes_ask=480, qty=20000),
    }
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))
    res = await rm.handle(result)
    assert res.action in ("completed", "partial")
    assert res.final_hedge_qty >= 8000


async def test_unequal_fills_empty_completion_book_action_unwound():
    books = {
        "A": make_book_with_yes_bid(yes_bid=450, qty=200),
        "B": make_book_with_yes_bid(yes_bid=450, qty=200),
    }
    legs = {
        "A": make_leg("A", 80, 480),
        "B": make_leg("B", 100, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))
    res = await rm.handle(result)
    assert res.action == "unwound"


async def test_final_hedge_qty_gte_original():
    books = {
        "A": make_full_book(450, 480, 200),
        "B": make_full_book(450, 480, 200),
    }
    legs = {
        "A": make_leg("A", 80, 480),
        "B": make_leg("B", 100, 480),
    }
    result = make_result("E", "buy", legs)
    rm = RiskManager(books, zero_config(), random.Random(42))
    res = await rm.handle(result)
    original_hedge = min(l.filled_qty for l in result.legs.values())
    assert res.final_hedge_qty >= original_hedge


async def test_resolution_fees_nonnegative():
    books = {
        "A": make_full_book(450, 480, 200),
        "B": make_full_book(450, 480, 200),
    }
    legs = {
        "A": make_leg("A", 80, 480),
        "B": make_leg("B", 100, 480),
    }
    result = make_result("E", "buy", legs)
    rm = RiskManager(books, zero_config(), random.Random(42))
    res = await rm.handle(result)
    assert res.resolution_fees >= 0


async def test_total_realized_profit_type():
    books = {
        "A": make_full_book(450, 480, 200),
        "B": make_full_book(450, 480, 200),
    }
    legs = {
        "A": make_leg("A", 100, 480),
        "B": make_leg("B", 100, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=500)
    rm = RiskManager(books, zero_config(), random.Random(42))
    res = await rm.handle(result)
    assert isinstance(res.total_realized_profit, int | float)


# Completion spending capped at available capital

async def test_completion_capped_by_budget():
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    # gap is 2000 on A (~960k cost); budget only allows ~1000
    res = await rm.handle(result, budget=480_000)

    assert res.resolution_capital <= 480_000
    assert res.final_hedge_qty >= 8000


async def test_zero_budget_forces_unwind_or_none():
    books = {
        "A": make_full_book(450, 480, 200),
        "B": make_full_book(450, 480, 200),
    }
    legs = {
        "A": make_leg("A", 80, 480),
        "B": make_leg("B", 100, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    res = await rm.handle(result, budget=0)

    # no cash to complete with: must not spend anything on completion
    assert res.resolution_capital <= 0
    assert res.action in ("unwound", "none")


async def test_capital_provider_caps_completion():
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    base_cost = result.total_capital
    # provider leaves only 480k beyond the base trade's own outflow
    rm = RiskManager(books, zero_config(), random.Random(42),
                     capital_provider=lambda: base_cost + 480_000)

    res = await rm.handle(result)

    assert res.resolution_capital <= 480_000


async def test_no_provider_means_unlimited():
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    res = await rm.handle(result)

    assert res.final_hedge_qty == 10000  # full completion, uncapped


# Guard: dust fills never complete (fee floor) 

async def test_dust_fill_below_floor_never_completes():
    """A 3-unit fill (0.03 contracts) can never out-earn Kalshi's per-order
    cent fee floor: it must be unwound, not completed (the BURHER trade)."""
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 3, 480),
        "B": make_leg("B", 0, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    res = await rm.handle(result)

    assert res.action != "completed"
    assert res.final_hedge_qty == 0


async def test_floor_boundary_allows_completion():
    """At exactly the floor (1000 units = 10 contracts) completion is allowed."""
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 800, 480),
        "B": make_leg("B", 1000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    res = await rm.handle(result)

    assert res.action == "completed"
    assert res.final_hedge_qty == 1000


# Guard: never complete into a locked loss

async def test_completion_skipped_when_it_locks_a_loss():
    """Sell-side position, lagging leg only sellable at 370: completing would
    lock joint proceeds < $1/set. Even though the sim scores unwind worse,
    we must not add exposure to lock a guaranteed loss."""
    books = {
        "A": make_full_book(yes_bid=600, yes_ask=999, qty=20000),  # unwind = buy back at 999
        "B": make_full_book(yes_bid=370, yes_ask=990, qty=20000),  # completion = sell at 370
    }
    legs = {
        "A": make_leg("A", 5000, 600, side="sell"),
        "B": make_leg("B", 0, 600, side="sell"),
    }
    result = make_result("E", "sell", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    res = await rm.handle(result)

    assert res.action != "completed"
    # no completion legs were executed (sell-side completion legs have side "sell")
    assert all(l.side != "sell" for l in res.resolution_legs)


async def test_resolution_exposes_final_holdings():
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    res = await rm.handle(result)

    assert res.final_holdings == {"A": res.final_hedge_qty, "B": res.final_hedge_qty} or \
        set(res.final_holdings) == {"A", "B"}
    assert min(res.final_holdings.values()) == res.final_hedge_qty


# Price-capped completion

async def test_completion_passes_sim_worst_price_as_limit():
    from unittest.mock import AsyncMock, patch
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    # add a deeper, worse ask level on A that the gap doesn't need
    books["A"].no_buys.set_real_qty(100, 50000)  # YES ask 900
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    captured = {}

    async def spy_execute_leg(*args, **kwargs):
        captured.update(kwargs)
        return make_leg("A", 2000, 480)

    with patch("risk.manager.execute_leg", new=spy_execute_leg):
        await rm._execute_completion("buy", {"A": 8000, "B": 10000})

    # gap of 2000 fits entirely in the 480 level: limit must be 480, not 900
    assert captured["limit_price"] == 480


async def test_completion_fills_capped_when_book_worsens_after_decision():
    """If the cheap level vanishes before the order allocates, the limit refuses
    the deeper level instead of locking a loss (the JOHBLA trade)."""
    books = {
        "A": make_full_book(450, 480, 20000),
        "B": make_full_book(450, 480, 20000),
    }
    legs = {
        "A": make_leg("A", 8000, 480),
        "B": make_leg("B", 10000, 480),
    }
    result = make_result("E", "buy", legs, realized_profit=0)
    rm = RiskManager(books, zero_config(), random.Random(42))

    # compute the limit the sim would authorize, then worsen the book the way
    # a feed delta would between decision and allocation
    limit = rm._completion_limit("buy", "A", 2000)
    assert limit == 480
    books["A"].no_buys.set_real_qty(520, 0)     # cheap ask gone
    books["A"].no_buys.set_real_qty(100, 50000)  # only 900s remain

    from execution.executor import execute_leg
    leg = await execute_leg("A", "buy", 2000, 480, books["A"], zero_config(),
                            random.Random(1), 0.0, limit_price=limit)

    assert leg.filled_qty == 0  # refused to chase the 900 level
