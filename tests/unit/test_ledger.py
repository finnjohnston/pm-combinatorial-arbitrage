from execution.models import ExecutionResult, LegFill
from risk.models import ResolutionResult
from optimizer.optimizer import Optimizer
from portfolio.db import TradeDB
from portfolio.ledger import Ledger


def make_leg(ticker="MKT-A", side="buy", requested=100, filled=100, price=500):
    return LegFill(
        ticker=ticker,
        side=side,
        requested_qty=requested,
        filled_qty=filled,
        unfilled_qty=requested - filled,
        avg_price=float(price),
        expected_price=price,
        fee=10,
        latency_ms=50.0,
        timestamp_ms=1000.0,
    )


def make_result(event_id="EVT-1", side="buy", target_qty=100, total_capital=9500,
                estimated_profit=500, realized_profit=450):
    legs = {
        "MKT-A": make_leg("MKT-A", side, filled=100),
        "MKT-B": make_leg("MKT-B", side, filled=100),
    }
    return ExecutionResult(
        event_id=event_id,
        side=side,
        target_qty=target_qty,
        legs=legs,
        total_capital=total_capital,
        estimated_profit=estimated_profit,
        realized_profit=realized_profit,
    )


def make_resolution(result, action="none", hedge_qty=10, resolution_capital=0,
                    resolution_fees=50, total_realized_profit=450):
    return ResolutionResult(
        original_result=result,
        action=action,
        final_hedge_qty=hedge_qty,
        resolution_capital=resolution_capital,
        resolution_fees=resolution_fees,
        total_realized_profit=total_realized_profit,
    )


def make_ledger(initial=10000):
    opt = Optimizer()
    return Ledger(initial_capital=initial, optimizer=opt), opt


def test_initial_capital():
    ledger, _ = make_ledger(10000)
    assert ledger.available_capital == 10000
    assert ledger.locked_capital == 0
    assert ledger.total_capital == 10000


def test_record_open_locks_execution_capital():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, resolution_capital=0)
    ledger.record_open("EVT-1", result, resolution)
    assert ledger.available_capital == 500
    assert ledger.locked_capital == 9500
    assert ledger.total_capital == 10000


def test_record_open_includes_resolution_capital():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=9000)
    resolution = make_resolution(result, resolution_capital=300)
    ledger.record_open("EVT-1", result, resolution)
    assert ledger.locked_capital == 9300
    assert ledger.available_capital == 700


def test_record_open_unwind_reduces_locked():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, resolution_capital=-400)
    ledger.record_open("EVT-1", result, resolution)
    assert ledger.locked_capital == 9100
    assert ledger.available_capital == 900


def test_record_close_unlocks_and_credits_profit():
    # profit is now computed at close: payout(winner) - cost - fees
    # holdings fall back to hedged {A:10, B:10}; payout = 10*1000 = 10000
    # fees = leg fees (10+10) + resolution fees (50) = 70 → profit = 430
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-1", result, resolution)
    profit = ledger.record_close("EVT-1", "MKT-A")
    assert profit == 10_000 - 9500 - 70
    assert ledger.locked_capital == 0
    assert ledger.available_capital == 10430
    assert ledger.total_capital == 10430


def test_record_open_releases_optimizer_commitment():
    ledger, opt = make_ledger(10000)
    opt.commit("EVT-1", 9500)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-1", result, resolution)
    assert "EVT-1" not in opt.committed


def test_record_close_releases_optimizer_commitment():
    ledger, opt = make_ledger(10000)
    opt.commit("EVT-1", 9500)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-1", result, resolution)
    ledger.record_close("EVT-1", "MKT-A")
    assert "EVT-1" not in opt.committed


def test_record_close_unknown_returns_zero():
    ledger, _ = make_ledger(10000)
    result = ledger.record_close("EVT-X", "MKT-A")
    assert result == 0
    assert ledger.total_capital == 10000


def test_multiple_trades_compound_capital():
    ledger, opt = make_ledger(10000)

    r1 = make_result(event_id="EVT-1", total_capital=9500)
    res1 = make_resolution(r1, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-1", r1, res1)
    ledger.record_close("EVT-1", "MKT-A")

    r2 = make_result(event_id="EVT-2", total_capital=9500)
    res2 = make_resolution(r2, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-2", r2, res2)
    ledger.record_close("EVT-2", "MKT-B")

    # each close: payout 10000 - cost 9500 - fees 70 = 430
    assert ledger.total_capital == 10000 + 2 * 430


def test_force_settle_all_credits_profit_and_clears_positions():
    ledger, opt = make_ledger(10000)

    r1 = make_result(event_id="EVT-1", total_capital=9500)
    res1 = make_resolution(r1, hedge_qty=10, resolution_fees=50, total_realized_profit=450)
    ledger.record_open("EVT-1", r1, res1)

    r2 = make_result(event_id="EVT-2", total_capital=9000)
    res2 = make_resolution(r2, hedge_qty=10, resolution_fees=50, total_realized_profit=400)
    ledger.record_open("EVT-2", r2, res2)

    total = ledger.force_settle_all()

    # worst-case payout for hedged holdings {A:10, B:10} = 10000 each;
    # trade1: 10000 - 9500 - 70 = 430, trade2: 10000 - 9000 - 70 = 930
    assert total == 430 + 930
    assert ledger.locked_capital == 0
    assert ledger.total_capital == 10000 + 1360


def test_record_unwound_debits_loss_from_available():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=9_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-8_500)
    ledger.record_unwound("EVT-1", result, resolution)
    assert ledger.available_capital == 10_000 - 8_500
    assert ledger.locked_capital == 0
    assert ledger.total_capital == 10_000 - 8_500


def test_record_unwound_releases_optimizer_commitment():
    ledger, opt = make_ledger(10000)
    opt.commit("EVT-1", 9_000)
    result = make_result(total_capital=9_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-8_500)
    ledger.record_unwound("EVT-1", result, resolution)
    assert "EVT-1" not in opt.committed


def test_record_unwound_does_not_create_open_position():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=9_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-8_500)
    ledger.record_unwound("EVT-1", result, resolution)
    assert not ledger._positions.is_open("EVT-1")


def test_force_settle_all_empty_is_noop():
    ledger, _ = make_ledger(10000)
    assert ledger.force_settle_all() == 0
    assert ledger.total_capital == 10000


# Capital snapshot written to DB on every state change

def make_ledger_with_db(initial=10000):
    db = TradeDB(":memory:")
    opt = Optimizer()
    ledger = Ledger(initial_capital=initial, optimizer=opt, db=db)
    return ledger, db


def test_record_open_snapshots_capital():
    ledger, db = make_ledger_with_db(10000)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, resolution_capital=0)
    ledger.record_open("EVT-1", result, resolution)

    snaps = db.capital_snapshots()
    assert len(snaps) == 1
    assert snaps[0]["total_capital"] == 10000
    assert snaps[0]["locked_capital"] == 9500
    assert snaps[0]["available_capital"] == 500
    db.close()


def test_record_close_snapshots_capital():
    ledger, db = make_ledger_with_db(10000)
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, resolution_capital=0, total_realized_profit=450)
    ledger.record_open("EVT-1", result, resolution)
    ledger.record_close("EVT-1", "MKT-A")

    snaps = db.capital_snapshots()
    assert len(snaps) == 2  # one from open, one from close
    close_snap = snaps[-1]
    assert close_snap["locked_capital"] == 0
    assert close_snap["total_capital"] == 10430  # payout 10000 - cost 9500 - fees 70
    assert close_snap["available_capital"] == 10430
    db.close()


def test_record_unwound_snapshots_capital():
    ledger, db = make_ledger_with_db(10000)
    result = make_result(total_capital=9000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-500)
    ledger.record_unwound("EVT-1", result, resolution)

    snaps = db.capital_snapshots()
    assert len(snaps) == 1
    assert snaps[0]["available_capital"] == 9500
    assert snaps[0]["locked_capital"] == 0
    db.close()


# record_repair — membership repairs (complete vs unwind)

def test_record_repair_completed_locks_extra_cost():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=5000)
    resolution = make_resolution(result, hedge_qty=10, resolution_capital=0)
    ledger.record_open("EVT-1", result, resolution)
    assert ledger.locked_capital == 5000

    # repair completes new legs at an extra 1200 of collateral/cash
    repair_result = make_result(total_capital=5000)
    repair_resolution = make_resolution(repair_result, action="completed", hedge_qty=10,
                                        resolution_capital=1200, total_realized_profit=300)
    ledger.record_repair("EVT-1", repair_result, repair_resolution)

    assert ledger.locked_capital == 6200
    assert ledger.available_capital == 10000 - 6200
    assert ledger.has_open_position("EVT-1")


def test_record_repair_unwound_closes_position_and_realizes_pnl():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=5000)
    resolution = make_resolution(result, hedge_qty=10, resolution_capital=0)
    ledger.record_open("EVT-1", result, resolution)

    # full unwind recovered most of the cost: net P&L -800
    repair_result = make_result(total_capital=5000)
    repair_resolution = make_resolution(repair_result, action="unwound", hedge_qty=0,
                                        resolution_capital=-4200, total_realized_profit=-800)
    ledger.record_repair("EVT-1", repair_result, repair_resolution)

    assert not ledger.has_open_position("EVT-1")
    assert ledger.locked_capital == 0
    assert ledger.total_capital == 10000 - 800


def test_record_repair_unknown_event_is_noop():
    ledger, _ = make_ledger(10000)
    result = make_result(total_capital=5000)
    resolution = make_resolution(result, hedge_qty=0)
    assert ledger.record_repair("EVT-X", result, resolution) == 0
    assert ledger.total_capital == 10000


# Winner-dependent settlement of ragged (naked) positions

def make_naked_sell(ledger, proceeds_price=650, qty=345):
    """The CHCBAL scenario: sold `qty` units of one leg, other leg 0, unwind failed."""
    collateral = qty * (1000 - proceeds_price)
    legs = {
        "MKT-BAL": make_leg("MKT-BAL", side="sell", requested=qty, filled=qty, price=proceeds_price),
        "MKT-CHC": make_leg("MKT-CHC", side="sell", requested=qty, filled=0, price=proceeds_price),
    }
    legs["MKT-CHC"] = LegFill(
        ticker="MKT-CHC", side="sell", requested_qty=qty, filled_qty=0, unfilled_qty=qty,
        avg_price=None, expected_price=proceeds_price, fee=0, latency_ms=0.0, timestamp_ms=0.0,
    )
    result = ExecutionResult(
        event_id="EVT-N", side="sell", target_qty=qty, legs=legs,
        total_capital=collateral, estimated_profit=0, realized_profit=qty * proceeds_price,
    )
    resolution = ResolutionResult(
        original_result=result, action="unwound",
        final_hedge_qty=0, resolution_capital=0, resolution_fees=0,
        total_realized_profit=qty * proceeds_price,
        final_holdings={"MKT-BAL": qty, "MKT-CHC": 0},
    )
    ledger.record_open("EVT-N", result, resolution)
    return collateral


def test_naked_short_settles_favorably_when_other_leg_wins():
    ledger, _ = make_ledger(1_000_000)
    collateral = make_naked_sell(ledger)  # short 345 BAL at 0.65
    fees = 10  # only the filled leg's fee (make_leg default)

    profit = ledger.record_close("EVT-N", "MKT-CHC")

    # BAL lost: our NO pays 345*1000; profit = 345000 - collateral - fees
    assert profit == 345_000 - collateral - fees
    assert ledger.total_capital == 1_000_000 + profit


def test_naked_short_settles_at_loss_when_shorted_leg_wins():
    ledger, _ = make_ledger(1_000_000)
    collateral = make_naked_sell(ledger)
    fees = 10

    profit = ledger.record_close("EVT-N", "MKT-BAL")

    # BAL won: our NO pays nothing; we lose the collateral plus fees
    assert profit == -(collateral + fees)
    assert ledger.total_capital == 1_000_000 + profit


def test_naked_short_force_settle_books_worst_case():
    ledger, _ = make_ledger(1_000_000)
    collateral = make_naked_sell(ledger)
    fees = 10

    total = ledger.force_settle_all()

    # worst case: BAL (the shorted leg) wins → payout 0
    assert total == -(collateral + fees)
    assert ledger.total_capital == 1_000_000 - collateral - fees


def test_naked_buy_settles_by_winner():
    ledger, _ = make_ledger(1_000_000)
    legs = {
        "MKT-A": make_leg("MKT-A", side="buy", requested=2000, filled=2000, price=400),
        "MKT-B": make_leg("MKT-B", side="buy", requested=2000, filled=0, price=400),
    }
    result = ExecutionResult(
        event_id="EVT-B", side="buy", target_qty=2000, legs=legs,
        total_capital=800_000, estimated_profit=0, realized_profit=-800_000,
    )
    resolution = ResolutionResult(
        original_result=result, action="unwound",
        final_hedge_qty=0, resolution_capital=0, resolution_fees=0,
        total_realized_profit=-800_000,
        final_holdings={"MKT-A": 2000, "MKT-B": 0},
    )
    ledger.record_open("EVT-B", result, resolution)
    fees = 20  # both legs carry the make_leg default fee of 10

    # held leg wins: payout 2000*1000
    profit = ledger.record_close("EVT-B", "MKT-A")
    assert profit == 2_000_000 - 800_000 - fees


def test_untracked_winner_pays_nothing_on_buy_side():
    ledger, _ = make_ledger(1_000_000)
    result = make_result(event_id="EVT-U", total_capital=9500)
    resolution = make_resolution(result, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-U", result, resolution)

    profit = ledger.record_close("EVT-U", "MKT-NEVER-TRACKED")

    # winner is a market we never held: payout 0, full loss of cost + fees
    assert profit == -(9500 + 70)


def test_hedged_close_matches_worst_case():
    ledger, _ = make_ledger(1_000_000)
    result = make_result(event_id="EVT-H", total_capital=9500)
    resolution = make_resolution(result, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-H", result, resolution)
    record = ledger.get_position("EVT-H")

    # for a hedged set, worst case == payout for any held winner
    assert record.worst_case_payout() == record.settlement_payout("MKT-A")
    assert record.worst_case_payout() == record.settlement_payout("MKT-B")


def test_record_repair_partial_unwind_keeps_ragged_position():
    ledger, _ = make_ledger(1_000_000)
    result = make_result(event_id="EVT-R", total_capital=9500)
    resolution = make_resolution(result, hedge_qty=10, resolution_fees=50)
    ledger.record_open("EVT-R", result, resolution)

    # repair unwound part of the position but residue remains: must stay open
    repair_result = make_result(event_id="EVT-R", total_capital=9500)
    repair_resolution = make_resolution(repair_result, action="partial", hedge_qty=0,
                                        resolution_capital=-4000, total_realized_profit=0)
    repair_resolution.final_holdings = {"MKT-A": 5, "MKT-B": 0}
    ledger.record_repair("EVT-R", repair_result, repair_resolution)

    assert ledger.has_open_position("EVT-R")
    record = ledger.get_position("EVT-R")
    assert record.holdings == {"MKT-A": 5, "MKT-B": 0}
    assert ledger.locked_capital == 9500 - 4000  # unwind proceeds released
