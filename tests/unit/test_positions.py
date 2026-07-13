import pytest
from execution.models import ExecutionResult, LegFill
from risk.models import ResolutionResult
from portfolio.positions import PositionTracker


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
                estimated_profit=500, realized_profit=400):
    legs = {
        "MKT-A": make_leg("MKT-A", side),
        "MKT-B": make_leg("MKT-B", side),
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


def make_resolution(result, action="none", hedge_qty=10, resolution_fees=50,
                    total_realized_profit=400):
    return ResolutionResult(
        original_result=result,
        action=action,
        final_hedge_qty=hedge_qty,
        resolution_fees=resolution_fees,
        total_realized_profit=total_realized_profit,
    )


def test_open_position_marks_as_open():
    pt = PositionTracker()
    result = make_result()
    resolution = make_resolution(result)
    pt.open_position("EVT-1", result, resolution)
    assert pt.is_open("EVT-1")


def test_unknown_event_not_open():
    pt = PositionTracker()
    assert not pt.is_open("EVT-X")


def test_close_buy_profit():
    pt = PositionTracker()
    result = make_result(total_capital=9500)
    resolution = make_resolution(result, total_realized_profit=450)
    pt.open_position("EVT-1", result, resolution)
    profit = pt.close_position("EVT-1", "MKT-A")
    assert profit == 450


def test_close_sell_profit():
    pt = PositionTracker()
    result = make_result(side="sell", total_capital=-5100)
    resolution = make_resolution(result, total_realized_profit=50)
    pt.open_position("EVT-1", result, resolution)
    profit = pt.close_position("EVT-1", "MKT-A")
    assert profit == 50


def test_close_removes_from_open():
    pt = PositionTracker()
    result = make_result()
    resolution = make_resolution(result)
    pt.open_position("EVT-1", result, resolution)
    pt.close_position("EVT-1", "MKT-A")
    assert not pt.is_open("EVT-1")


def test_close_unknown_raises():
    pt = PositionTracker()
    with pytest.raises(KeyError):
        pt.close_position("EVT-X", "MKT-A")


def test_open_property_returns_copy():
    pt = PositionTracker()
    result = make_result()
    resolution = make_resolution(result)
    pt.open_position("EVT-1", result, resolution)
    copy = pt.open
    copy.pop("EVT-1")
    assert pt.is_open("EVT-1")


def test_multiple_positions_independent():
    pt = PositionTracker()
    r1 = make_result(event_id="EVT-1", total_capital=9000)
    r2 = make_result(event_id="EVT-2", total_capital=8800)
    res1 = make_resolution(r1, total_realized_profit=1000)
    res2 = make_resolution(r2, total_realized_profit=200)
    pt.open_position("EVT-1", r1, res1)
    pt.open_position("EVT-2", r2, res2)
    profit1 = pt.close_position("EVT-1", "MKT-A")
    assert profit1 == 1000
    assert pt.is_open("EVT-2")
    profit2 = pt.close_position("EVT-2", "MKT-A")
    assert profit2 == 200
