import pytest
from portfolio.metrics import daily_report, final_report, _fmt_duration, _fmt_usd, _fmt_signed_usd


def make_trade(
    event_id="KXWTAMATCH-26JUL05BENGAU",
    side="buy",
    status="settled",
    total_realized_profit=111_100,
    fees=20,
    fill_rate=1.0,
    avg_slippage_ticks=0.0,
    avg_latency_ms=120.0,
    num_legs=3,
    action="none",
):
    return {
        "id": 1,
        "event_id": event_id,
        "side": side,
        "status": status,
        "opened_at": 1_000_000.0,
        "closed_at": 1_001_000.0 if status != "open" else None,
        "winner_ticker": "MKT-A" if status == "settled" else None,
        "num_legs": num_legs,
        "target_qty": 100,
        "capital_deployed": 8_000_000,
        "hedge_qty": 100,
        "estimated_profit": 120_000,
        "realized_profit": 110_000,
        "total_realized_profit": total_realized_profit,
        "action": action,
        "fees": fees,
        "fill_rate": fill_rate,
        "avg_slippage_ticks": avg_slippage_ticks,
        "avg_latency_ms": avg_latency_ms,
    }


# daily_report: structure

def test_report_contains_header():
    report = daily_report([], 10_000_000, 10_000_000, 86400)
    assert "Daily Report" in report


def test_report_contains_overview_section():
    report = daily_report([], 10_000_000, 10_000_000, 86400)
    assert "Overview" in report
    assert "Runtime" in report
    assert "Initial capital" in report
    assert "Current capital" in report


def test_report_contains_itemized_sections():
    report = daily_report([], 10_000_000, 10_000_000, 86400)
    assert "Settled Since Last Report  (0)" in report
    assert "Open Positions  (0)" in report


def test_report_no_closed_trades_skips_pnl_sections():
    trades = [make_trade(status="open")]
    report = daily_report(trades, 10_000_000, 10_000_000, 86400)
    assert "P&L" not in report
    assert "Execution quality" not in report
    assert "Risk management" not in report


# daily_report: capital and P&L

def test_report_capital_display():
    report = daily_report([], 10_000_000, 10_095_000, 86400)
    assert "$100.0000" in report
    assert "$100.9500" in report
    assert "+0.95%" in report


def test_report_pnl_total():
    trades = [
        make_trade(total_realized_profit=100_000),
        make_trade(total_realized_profit=50_000),
    ]
    report = daily_report(trades, 10_000_000, 10_150_000, 86400)
    assert "P&L" in report
    assert "+$1.5000" in report


def test_report_open_trades_excluded_from_pnl():
    trades = [
        make_trade(status="settled", total_realized_profit=100_000),
        make_trade(status="open", total_realized_profit=999_900),
    ]
    report = daily_report(trades, 10_000_000, 10_100_000, 86400)
    # the open trade's estimate appears only as est_pnl in the itemized section,
    # never in the P&L totals
    assert "Total            : +$1.0000" in report
    assert "est_pnl=+$9.9990" in report


def test_report_force_settled_included_in_pnl():
    trades = [
        make_trade(status="settled", total_realized_profit=100_000),
        make_trade(status="force_settled", total_realized_profit=50_000),
    ]
    report = daily_report(trades, 10_000_000, 10_150_000, 86400)
    assert "+$1.5000" in report


# daily_report: execution quality

def test_report_fill_rate():
    trades = [make_trade(fill_rate=0.80), make_trade(fill_rate=0.60)]
    report = daily_report(trades, 10_000_000, 10_000_000, 86400)
    assert "70.0%" in report


def test_report_slippage():
    trades = [make_trade(avg_slippage_ticks=2.0), make_trade(avg_slippage_ticks=4.0)]
    report = daily_report(trades, 10_000_000, 10_000_000, 86400)
    assert "+3.0 ticks" in report


# daily_report: by side 

def test_report_by_side():
    trades = [
        make_trade(side="buy", total_realized_profit=80_000),
        make_trade(side="buy", total_realized_profit=60_000),
        make_trade(side="sell", total_realized_profit=30_000),
    ]
    report = daily_report(trades, 10_000_000, 10_170_000, 86400)
    assert "Buy arb" in report
    assert "Sell arb" in report
    assert "+$1.4000" in report
    assert "+$0.3000" in report


# helpers

def test_fmt_duration_minutes_only():
    assert _fmt_duration(0) == "0m"
    assert _fmt_duration(59) == "0m"
    assert _fmt_duration(600) == "10m"


def test_fmt_duration_hours():
    assert _fmt_duration(3600) == "1h 0m"
    assert _fmt_duration(3600 + 900) == "1h 15m"


def test_fmt_duration_days():
    assert _fmt_duration(86400) == "1d 0h 0m"
    assert _fmt_duration(86400 * 3 + 3600 * 2 + 60 * 14) == "3d 2h 14m"


def test_fmt_usd():
    assert _fmt_usd(10_000_000) == "$100.0000"
    assert _fmt_usd(111_100) == "$1.1110"
    assert _fmt_usd(0) == "$0.0000"


def test_fmt_signed_usd_positive():
    assert _fmt_signed_usd(50_000) == "+$0.5000"


def test_fmt_signed_usd_negative():
    assert _fmt_signed_usd(-20_000) == "-$0.2000"


def test_fmt_signed_usd_zero():
    assert _fmt_signed_usd(0) == "+$0.0000"


# new report structure

def test_capital_line_shows_return_rate_only():
    report = daily_report([], 10_000_000, 10_095_000, 86400)
    assert "(+0.95%)" in report
    assert "+$0.9500, " not in report  # absolute delta no longer shown


def test_daily_report_itemizes_window_settlements_only():
    old = make_trade(event_id="EVT-OLD")
    old["closed_at"] = 500.0
    recent = make_trade(event_id="EVT-RECENT")
    recent["closed_at"] = 2_000.0
    report = daily_report([old, recent], 10_000_000, 10_000_000, 86400, since=1_000.0)
    assert "Settled Since Last Report  (1)" in report
    assert "EVT-RECENT" in report
    assert "EVT-OLD" not in report


def test_daily_report_itemizes_open_positions():
    trades = [make_trade(event_id="EVT-CARRIED", status="open")]
    report = daily_report(trades, 10_000_000, 10_000_000, 86400)
    assert "Open Positions  (1)" in report
    assert "EVT-CARRIED" in report
    assert "age=" in report


def test_final_report_header_and_sections():
    trades = [
        make_trade(event_id="EVT-DONE", status="settled"),
        make_trade(event_id="EVT-KILLED", status="force_settled"),
    ]
    report = final_report(trades, 10_000_000, 10_000_000, 86400)
    assert "Final Session Report" in report
    assert "Final capital" in report
    assert "Settled  (1)" in report
    assert "Force-Settled  (1)" in report
    assert "worst case" not in report
    assert "EVT-DONE" in report
    assert "EVT-KILLED" in report


def test_final_report_has_no_open_section():
    report = final_report([], 10_000_000, 10_000_000, 86400)
    assert "Open Positions" not in report


def test_position_line_shows_naked_when_present():
    t = make_trade(event_id="EVT-NKD")
    t["naked_qty"] = 345
    report = final_report([t], 10_000_000, 10_000_000, 86400)
    assert "naked=345" in report


def test_position_line_hides_naked_when_zero():
    t = make_trade(event_id="EVT-CLEAN")
    t["naked_qty"] = 0
    report = final_report([t], 10_000_000, 10_000_000, 86400)
    assert "naked=" not in report


# local day boundary

def test_local_day_start_is_midnight_of_that_day():
    from datetime import datetime
    from portfolio.metrics import local_day_start

    noon_today = datetime.now().astimezone().replace(hour=12, minute=30, second=0, microsecond=0)
    start = local_day_start(noon_today.timestamp())

    start_dt = datetime.fromtimestamp(start).astimezone()
    assert (start_dt.hour, start_dt.minute, start_dt.second) == (0, 0, 0)
    assert start_dt.date() == noon_today.date()
    assert start <= noon_today.timestamp() < start + 86400 + 3700  # DST tolerance


def test_local_day_start_defaults_to_now():
    import time as _time
    from portfolio.metrics import local_day_start
    start = local_day_start()
    assert start <= _time.time() < start + 86400 + 3700
