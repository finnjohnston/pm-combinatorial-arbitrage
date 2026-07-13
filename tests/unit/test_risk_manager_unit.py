import pytest
from book.public_book import PublicMarketBook
from execution.config import ExecutionConfig
from risk.manager import RiskManager


def make_book_sell_side(yes_bid: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.yes_buys.set_real_qty(yes_bid, qty)
    return b


def make_book_buy_side(yes_ask: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.no_buys.set_real_qty(1000 - yes_ask, qty)
    return b


def make_config():
    return ExecutionConfig(min_latency_ms=0, max_latency_ms=0, participation_rate=1.0)


def test_compute_unhedged_returns_correct_hedge_and_excess():
    hedge, excess = RiskManager._compute_unhedged({"A": 100, "B": 80})
    assert hedge == 80
    assert excess == {"A": 20}


def test_compute_unhedged_returns_empty_when_equal():
    hedge, excess = RiskManager._compute_unhedged({"A": 100, "B": 100})
    assert hedge == 100
    assert excess == {}


def test_compute_unhedged_three_legs():
    hedge, excess = RiskManager._compute_unhedged({"A": 50, "B": 80, "C": 60})
    assert hedge == 50
    assert excess == {"B": 30, "C": 10}


def test_compute_unhedged_single_leg():
    hedge, excess = RiskManager._compute_unhedged({"A": 75})
    assert hedge == 75
    assert excess == {}


def test_eval_completion_pnl_positive_when_arb_exists():
    book_a = make_book_buy_side(yes_ask=480, qty=50)
    book_b = make_book_buy_side(yes_ask=480, qty=50)
    rm = RiskManager({"A": book_a, "B": book_b}, make_config())
    holdings = {"A": 80, "B": 100}
    hedge_qty = 80
    pnl = rm._eval_completion_pnl("buy", holdings, hedge_qty)
    assert pnl > 0


def test_eval_completion_pnl_zero_when_no_short_legs():
    book_a = make_book_buy_side(yes_ask=480, qty=50)
    book_b = make_book_buy_side(yes_ask=480, qty=50)
    rm = RiskManager({"A": book_a, "B": book_b}, make_config())
    holdings = {"A": 100, "B": 100}
    hedge_qty = 100
    pnl = rm._eval_completion_pnl("buy", holdings, hedge_qty)
    assert pnl == 0


def test_eval_unwind_pnl_returns_integer():
    book_a = make_book_sell_side(yes_bid=600, qty=50)
    book_b = make_book_sell_side(yes_bid=600, qty=50)
    rm = RiskManager({"A": book_a, "B": book_b}, make_config())
    holdings = {"A": 80, "B": 100}
    hedge_qty = 80
    pnl = rm._eval_unwind_pnl("buy", holdings, hedge_qty)
    assert isinstance(pnl, int)


def test_eval_unwind_pnl_uses_excess_only():
    book_a = make_book_sell_side(yes_bid=600, qty=50)
    book_b = make_book_sell_side(yes_bid=600, qty=50)
    rm = RiskManager({"A": book_a, "B": book_b}, make_config())
    holdings = {"A": 80, "B": 100}
    hedge_qty = 80
    pnl = rm._eval_unwind_pnl("buy", holdings, hedge_qty)
    from book.fees import taker_fee_order
    from book.types import Fill
    fee = taker_fee_order([Fill(price=600, qty=20)])
    assert pnl == 12000 - fee


def test_eval_unwind_pnl_zero_when_no_excess():
    book_a = make_book_sell_side(yes_bid=600, qty=50)
    book_b = make_book_sell_side(yes_bid=600, qty=50)
    rm = RiskManager({"A": book_a, "B": book_b}, make_config())
    holdings = {"A": 100, "B": 100}
    hedge_qty = 100
    pnl = rm._eval_unwind_pnl("buy", holdings, hedge_qty)
    assert pnl == 0
