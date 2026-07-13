import pytest
from execution.models import ExecutionResult, LegFill
from risk.models import ResolutionResult
from portfolio.db import TradeDB, _fill_rate, _avg_slippage, _avg_latency


def make_leg(ticker="MKT-A", side="buy", requested=100, filled=100, price=400):
    return LegFill(
        ticker=ticker,
        side=side,
        requested_qty=requested,
        filled_qty=filled,
        unfilled_qty=requested - filled,
        avg_price=float(price),
        expected_price=price,
        fee=10,
        latency_ms=80.0,
        timestamp_ms=1000.0,
    )


def make_result(event_id="EVT-1", side="buy", target_qty=100,
                total_capital=80_000, estimated_profit=500, realized_profit=450):
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


def make_resolution(result, action="none", hedge_qty=100,
                    resolution_capital=0, resolution_fees=20,
                    total_realized_profit=450):
    return ResolutionResult(
        original_result=result,
        action=action,
        final_hedge_qty=hedge_qty,
        resolution_capital=resolution_capital,
        resolution_fees=resolution_fees,
        total_realized_profit=total_realized_profit,
    )


@pytest.fixture
def db():
    d = TradeDB(":memory:")
    yield d
    d.close()


# --- schema ---

def test_tables_exist(db):
    tables = {
        row[0] for row in
        db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "trades" in tables
    assert "capital_snapshots" in tables


# --- record_open ---

def test_record_open_inserts_open_row(db):
    result = make_result()
    resolution = make_resolution(result)
    db.record_open("EVT-1", result, resolution, capital_deployed=80_000)
    rows = db.open_trades()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_id"] == "EVT-1"
    assert row["side"] == "buy"
    assert row["status"] == "open"
    assert row["num_legs"] == 2
    assert row["target_qty"] == 100
    assert row["capital_deployed"] == 80_000
    assert row["hedge_qty"] == 100
    assert row["estimated_profit"] == 500
    assert row["action"] == "none"
    assert row["fees"] == 20
    assert row["closed_at"] is None
    assert row["winner_ticker"] is None
    assert row["realized_profit"] is None
    assert row["total_realized_profit"] == 450


def test_record_open_fill_rate(db):
    result = make_result(target_qty=100)
    resolution = make_resolution(result)
    db.record_open("EVT-1", result, resolution, capital_deployed=80_000)
    row = db.open_trades()[0]
    assert row["fill_rate"] == pytest.approx(1.0)


def test_record_open_slippage_and_latency(db):
    result = make_result()
    resolution = make_resolution(result)
    db.record_open("EVT-1", result, resolution, capital_deployed=80_000)
    row = db.open_trades()[0]
    assert row["avg_latency_ms"] == pytest.approx(80.0)
    # avg_price == expected_price (both 400) → slippage = 0
    assert row["avg_slippage_ticks"] == pytest.approx(0.0)


# record_close

def test_record_close_updates_to_settled(db):
    result = make_result()
    resolution = make_resolution(result)
    db.record_open("EVT-1", result, resolution, capital_deployed=80_000)
    db.record_close("EVT-1", "MKT-A", realized_profit=450, total_realized_profit=450)
    assert db.open_trades() == []
    trades = db.all_trades()
    assert len(trades) == 1
    row = trades[0]
    assert row["status"] == "settled"
    assert row["winner_ticker"] == "MKT-A"
    assert row["realized_profit"] == 450
    assert row["total_realized_profit"] == 450
    assert row["closed_at"] is not None


def test_record_close_unknown_event_is_noop(db):
    db.record_close("NONEXISTENT", "MKT-A", 100, 100)
    assert db.all_trades() == []


# force_settle

def test_force_settle_updates_to_force_settled(db):
    result = make_result()
    resolution = make_resolution(result)
    db.record_open("EVT-1", result, resolution, capital_deployed=80_000)
    db.force_settle("EVT-1", estimated_profit=500)
    assert db.open_trades() == []
    row = db.all_trades()[0]
    assert row["status"] == "force_settled"
    assert row["winner_ticker"] is None
    assert row["realized_profit"] == 500
    assert row["total_realized_profit"] == 500
    assert row["closed_at"] is not None


# snapshot_capital

def test_snapshot_capital_inserts_row(db):
    db.snapshot_capital(total_capital=100_000, locked_capital=5_000, available_capital=95_000)
    snaps = db.capital_snapshots()
    assert len(snaps) == 1
    s = snaps[0]
    assert s["total_capital"] == 100_000
    assert s["locked_capital"] == 5_000
    assert s["available_capital"] == 95_000
    assert s["timestamp"] > 0


def test_multiple_snapshots_ordered(db):
    db.snapshot_capital(100_000, 0, 100_000)
    db.snapshot_capital(100_500, 0, 100_500)
    snaps = db.capital_snapshots()
    assert len(snaps) == 2
    assert snaps[1]["total_capital"] >= snaps[0]["total_capital"]


# query methods

def test_open_trades_excludes_closed(db):
    r1, res1 = make_result(event_id="EVT-1"), None
    r1 = make_result(event_id="EVT-1")
    res1 = make_resolution(r1)
    r2 = make_result(event_id="EVT-2")
    res2 = make_resolution(r2)
    db.record_open("EVT-1", r1, res1, capital_deployed=80_000)
    db.record_open("EVT-2", r2, res2, capital_deployed=80_000)
    db.record_close("EVT-1", "MKT-A", 450, 450)
    open_trades = db.open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["event_id"] == "EVT-2"


def test_all_trades_returns_all_statuses(db):
    r1 = make_result(event_id="EVT-1")
    res1 = make_resolution(r1)
    r2 = make_result(event_id="EVT-2")
    res2 = make_resolution(r2)
    db.record_open("EVT-1", r1, res1, capital_deployed=80_000)
    db.record_open("EVT-2", r2, res2, capital_deployed=80_000)
    db.record_close("EVT-1", "MKT-A", 450, 450)
    db.force_settle("EVT-2", 500)
    trades = db.all_trades()
    assert len(trades) == 2
    statuses = {t["status"] for t in trades}
    assert statuses == {"settled", "force_settled"}


# record_unwound

def test_record_unwound_not_in_open_trades(db):
    result = make_result(total_capital=90_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-80_000)
    db.record_unwound("EVT-1", result, resolution)
    assert db.open_trades() == []


def test_record_unwound_in_all_trades(db):
    result = make_result(total_capital=90_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-80_000)
    db.record_unwound("EVT-1", result, resolution)
    trades = db.all_trades()
    assert len(trades) == 1
    assert trades[0]["status"] == "unwound"


def test_record_unwound_fields(db):
    result = make_result(total_capital=90_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-80_000)
    db.record_unwound("EVT-1", result, resolution)
    row = db.all_trades()[0]
    assert row["event_id"] == "EVT-1"
    assert row["hedge_qty"] == 0
    assert row["total_realized_profit"] == -80_000
    assert row["capital_deployed"] == 90_000
    assert row["closed_at"] is not None
    assert row["opened_at"] is not None


def test_record_unwound_capital_deployed_is_abs_for_sell_arb(db):
    result = make_result(total_capital=-90_000)
    resolution = make_resolution(result, action="unwound", hedge_qty=0, total_realized_profit=-5_000)
    db.record_unwound("EVT-1", result, resolution)
    assert db.all_trades()[0]["capital_deployed"] == 90_000


def test_record_unwound_mixed_with_open_trades(db):
    r_open = make_result(event_id="EVT-1", total_capital=80_000)
    res_open = make_resolution(r_open)
    db.record_open("EVT-1", r_open, res_open, capital_deployed=80_000)

    r_unwound = make_result(event_id="EVT-2", total_capital=90_000)
    res_unwound = make_resolution(r_unwound, action="unwound", hedge_qty=0, total_realized_profit=-80_000)
    db.record_unwound("EVT-2", r_unwound, res_unwound)

    assert len(db.open_trades()) == 1
    assert db.open_trades()[0]["event_id"] == "EVT-1"
    assert len(db.all_trades()) == 2


# helper functions

def test_fill_rate_full():
    assert _fill_rate(final_hedge_qty=100, target_qty=100) == pytest.approx(1.0)


def test_fill_rate_partial():
    assert _fill_rate(final_hedge_qty=60, target_qty=100) == pytest.approx(0.6)


def test_fill_rate_zero_target():
    assert _fill_rate(final_hedge_qty=0, target_qty=0) == 0.0


def test_avg_slippage_zero_when_price_matches():
    legs = [make_leg(price=400)]
    assert _avg_slippage(legs) == pytest.approx(0.0)


def test_avg_slippage_positive_on_buy_overpay():
    leg = make_leg(side="buy", price=410)
    leg = LegFill(
        ticker="MKT-A", side="buy",
        requested_qty=100, filled_qty=100, unfilled_qty=0,
        avg_price=415.0, expected_price=410,
        fee=10, latency_ms=80.0, timestamp_ms=1000.0,
    )
    assert _avg_slippage([leg]) == pytest.approx(5.0)


def test_avg_latency(db):
    legs = [
        make_leg("MKT-A"),
        LegFill("MKT-B", "buy", 100, 100, 0, 400.0, 400, 10, 120.0, 1000.0),
    ]
    assert _avg_latency(legs) == pytest.approx(100.0)


def test_avg_slippage_empty():
    assert _avg_slippage([]) == 0.0


def test_avg_latency_empty():
    assert _avg_latency([]) == 0.0
