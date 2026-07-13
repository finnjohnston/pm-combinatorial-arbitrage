import time

from execution.models import ExecutionResult, LegFill
from portfolio.db import TradeDB
from risk.models import ResolutionResult
from snapshot import build_snapshot


def make_leg(ticker="MKT-A", side="buy", filled=100, price=500):
    return LegFill(
        ticker=ticker, side=side,
        requested_qty=filled, filled_qty=filled, unfilled_qty=0,
        avg_price=float(price), expected_price=price,
        fee=10, latency_ms=50.0, timestamp_ms=1000.0,
    )


def make_result(event_id="EVT-1", side="buy", total_capital=9500):
    legs = {"MKT-A": make_leg("MKT-A", side), "MKT-B": make_leg("MKT-B", side)}
    return ExecutionResult(
        event_id=event_id, side=side, target_qty=100, legs=legs,
        total_capital=total_capital, estimated_profit=500, realized_profit=450,
    )


def make_resolution(result, hedge_qty=100, total_realized_profit=450):
    return ResolutionResult(
        original_result=result, action="none",
        final_hedge_qty=hedge_qty, resolution_capital=0,
        resolution_fees=50, total_realized_profit=total_realized_profit,
    )


def make_db_with_settled_trade(event_id="EVT-1", profit=450):
    db = TradeDB(":memory:")
    db.snapshot_capital(10000, 0, 10000)
    result = make_result(event_id=event_id)
    resolution = make_resolution(result, total_realized_profit=profit)
    db.record_open(event_id, result, resolution, capital_deployed=9500)
    db.record_close(event_id, "MKT-A", 450, profit)
    db.snapshot_capital(10000 + profit, 0, 10000 + profit)
    return db


def test_snapshot_empty_db_returns_none():
    db = TradeDB(":memory:")
    assert build_snapshot(db) is None
    db.close()


def test_snapshot_lists_settled_today():
    db = make_db_with_settled_trade("EVT-WON", profit=450)
    report = build_snapshot(db)
    assert "Settled Today  (1)" in report
    assert "EVT-WON" in report
    assert "pnl=+$0.0045" in report
    assert "held=" in report
    db.close()


def test_snapshot_settled_today_none_when_empty():
    db = TradeDB(":memory:")
    db.snapshot_capital(10000, 0, 10000)
    report = build_snapshot(db)
    assert "Settled Today  (0)" in report
    assert "Session Totals  (0 settled)" in report
    db.close()


def test_snapshot_session_totals_include_all_settled():
    db = make_db_with_settled_trade("EVT-1", profit=1000)
    report = build_snapshot(db)
    assert "Session Totals  (1 settled)" in report
    assert "Total P&L        : +$0.0100" in report
    assert "Today's P&L      : +$0.0100" in report
    db.close()


def test_snapshot_old_settlement_excluded_from_today_list():
    db = make_db_with_settled_trade("EVT-OLD", profit=1000)
    # backdate the close to well before today
    db._conn.execute("UPDATE trades SET closed_at = closed_at - 172800")
    db._conn.commit()
    report = build_snapshot(db)
    assert "Settled Today  (0)" in report
    assert "Session Totals  (1 settled)" in report
    assert "Total P&L        : +$0.0100" in report
    assert "Today's P&L      : +$0.0000" in report
    db.close()


def test_snapshot_shows_naked_qty_for_open_position():
    db = TradeDB(":memory:")
    db.snapshot_capital(100_000, 120_750, 100_000)
    result = make_result(event_id="EVT-NAKED", side="sell")
    resolution = make_resolution(result, hedge_qty=0, total_realized_profit=224_250)
    db.record_open("EVT-NAKED", result, resolution, capital_deployed=120_750, naked_qty=345)

    report = build_snapshot(db)

    assert "naked=345" in report
    assert "est_pnl=" in report
    db.close()


def test_snapshot_no_naked_marker_when_hedged():
    db = TradeDB(":memory:")
    db.snapshot_capital(100_000, 9500, 90_500)
    result = make_result(event_id="EVT-HEDGED")
    resolution = make_resolution(result)
    db.record_open("EVT-HEDGED", result, resolution, capital_deployed=9500)

    report = build_snapshot(db)

    assert "naked=" not in report
    db.close()


def test_snapshot_shows_naked_on_settled_lines():
    db = TradeDB(":memory:")
    db.snapshot_capital(100_000, 0, 100_000)
    result = make_result(event_id="EVT-NKDSETTLED", side="sell")
    resolution = make_resolution(result, hedge_qty=0, total_realized_profit=-120_750)
    db.record_open("EVT-NKDSETTLED", result, resolution, capital_deployed=120_750, naked_qty=345)
    db.record_close("EVT-NKDSETTLED", "MKT-A", -120_750, -120_750)
    db.snapshot_capital(100_000, 0, 100_000)

    report = build_snapshot(db)

    assert "Settled Today  (1)" in report
    assert "qty=0  naked=345" in report
    db.close()
