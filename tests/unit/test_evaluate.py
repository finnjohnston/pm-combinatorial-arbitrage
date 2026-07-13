import pytest
from graph.event_state import EventState
from book.public_book import PublicMarketBook
from book.fees import taker_fee
from optimizer.evaluate import evaluate_event, MIN_RETURN_RATE


def make_state(tickers, bids=None, asks=None):
    s = EventState(frozenset(tickers))
    bids = bids or {}
    asks = asks or {}
    for t in tickers:
        s.update_market(t, bids.get(t), asks.get(t))
    return s


def make_book_with_yes_ask(price: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.no_buys.set_real_qty(1000 - price, qty)
    return b


def make_book_with_yes_bid(price: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.yes_buys.set_real_qty(price, qty)
    return b


def test_returns_none_when_no_arb():
    s = make_state(["A", "B"], asks={"A": 500, "B": 500})
    books = {
        "A": make_book_with_yes_ask(500, 100),
        "B": make_book_with_yes_ask(500, 100),
    }
    assert evaluate_event("E", s, books) is None


def test_returns_none_when_ask_above_1000():
    s = make_state(["A", "B"], asks={"A": 600, "B": 600})
    books = {
        "A": make_book_with_yes_ask(600, 100),
        "B": make_book_with_yes_ask(600, 100),
    }
    assert evaluate_event("E", s, books) is None


def test_detects_buy_side_arb():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert opp.side == "buy"
    assert len(opp.tiers) == 1
    tier = opp.tiers[0]
    assert tier.quantity == 100
    fee = taker_fee(100, 450) * 2
    expected_profit = 100 * (1000 - 900) - fee
    assert tier.profit == expected_profit


def test_detects_sell_side_arb():
    s = make_state(["A", "B"], bids={"A": 545, "B": 545}, asks={"A": 510, "B": 510})
    books = {
        "A": PublicMarketBook(),
        "B": PublicMarketBook(),
    }
    for ticker in ("A", "B"):
        books[ticker].yes_buys.set_real_qty(545, 100)
        books[ticker].no_buys.set_real_qty(490, 100)
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert opp.side == "sell"
    assert len(opp.tiers) >= 1
    assert opp.tiers[0].profit > 0


def test_multiple_tiers_when_book_has_multiple_levels():
    # Tier 1: A=450, B=455 → joint 905, ~6.4% return
    # Tier 2: A=460, B=455 → joint 915, ~5.3% return — both clear the gates
    s = make_state(["A", "B"], asks={"A": 450, "B": 455})
    bk_A = PublicMarketBook()
    bk_A.no_buys.set_real_qty(1000 - 450, 100)
    bk_A.no_buys.set_real_qty(1000 - 460, 100)
    bk_B = PublicMarketBook()
    bk_B.no_buys.set_real_qty(1000 - 455, 200)
    books = {"A": bk_A, "B": bk_B}
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert len(opp.tiers) == 2


def test_tier_qty_is_bottleneck():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 50),
        "B": make_book_with_yes_ask(450, 100),
    }
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert opp.tiers[0].quantity == 50


def test_stops_when_profit_nonpositive():
    # Level 1: joint 900 → ~7% return, included. Level 2: joint 1980 → negative profit, stopped.
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    bk_A = PublicMarketBook()
    bk_A.no_buys.set_real_qty(1000 - 450, 100)
    bk_A.no_buys.set_real_qty(1000 - 990, 100)
    bk_B = PublicMarketBook()
    bk_B.no_buys.set_real_qty(1000 - 450, 100)
    bk_B.no_buys.set_real_qty(1000 - 990, 100)
    books = {"A": bk_A, "B": bk_B}
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert len(opp.tiers) == 1


def test_returns_none_when_book_empty():
    s = make_state(["A", "B"], asks={"A": 480, "B": 480})
    books = {
        "A": make_book_with_yes_ask(480, 100),
        "B": PublicMarketBook(),
    }
    assert evaluate_event("E", s, books) is None


# Return rate filter

def test_return_rate_blocks_thin_opportunity():
    # joint = 960 dc, net profit after fees = 500 dc on 100 sets
    # total cash out = 96000 + 3500 fees = 99500 dc, return = 0.5% < 1% → blocked
    s = make_state(["A", "B"], asks={"A": 480, "B": 480})
    books = {
        "A": make_book_with_yes_ask(480, 100),
        "B": make_book_with_yes_ask(480, 100),
    }
    assert evaluate_event("E", s, books) is None


def test_return_rate_passes_adequate_opportunity():
    # joint = 900 dc, net profit = 6520 dc on 100 sets
    # total cash out = 90000 + 3480 fees = 93480 dc, return = 7% >> 1% → passes
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    assert evaluate_event("E", s, books) is not None


def test_return_rate_stops_second_tier_when_too_thin():
    # Tier 1: joint 900 dc (7% return) → included
    # Tier 2: joint 960 dc (0.5% return) → positive gross profit but blocked by return rate
    bk_A = PublicMarketBook()
    bk_A.no_buys.set_real_qty(1000 - 450, 100)  # YES ask 450
    bk_A.no_buys.set_real_qty(1000 - 480, 100)  # YES ask 480
    bk_B = PublicMarketBook()
    bk_B.no_buys.set_real_qty(1000 - 450, 100)
    bk_B.no_buys.set_real_qty(1000 - 480, 100)
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {"A": bk_A, "B": bk_B}
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert len(opp.tiers) == 1


# Spread floor filter

def _make_two_sided_book(ask: int, bid: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.no_buys.set_real_qty(1000 - ask, qty)  # YES ask
    b.yes_buys.set_real_qty(bid, qty)         # YES bid
    return b


def test_spread_floor_blocks_when_profit_below_reversal_cost():
    # Spread per leg = 50 dc (ask=450, bid=400). Total spread_floor = 100 dc/set.
    # Profit per set = 1000 - 900 - fees ≈ 99 dc < 100 → blocked.
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": _make_two_sided_book(ask=450, bid=400, qty=100),
        "B": _make_two_sided_book(ask=450, bid=400, qty=100),
    }
    assert evaluate_event("E", s, books) is None


def test_spread_floor_passes_when_profit_exceeds_reversal_cost():
    # Spread per leg = 5 dc (ask=450, bid=445). Total spread_floor = 10 dc/set.
    # Profit per set = 100 dc - fees >> 10 → passes.
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": _make_two_sided_book(ask=450, bid=445, qty=100),
        "B": _make_two_sided_book(ask=450, bid=445, qty=100),
    }
    assert evaluate_event("E", s, books) is not None


# Capital-based minimum profit floor

def test_capital_floor_blocks_when_profit_below_floor():
    # joint=900, qty=100 → profit≈6520 dc; floor=7000 → blocked
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    assert evaluate_event("E", s, books, min_profit_dc=7000) is None


def test_capital_floor_passes_when_profit_above_floor():
    # joint=900, qty=100 → profit≈6520 dc; floor=6000 → passes
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    assert evaluate_event("E", s, books, min_profit_dc=6000) is not None


def test_spread_floor_not_applied_when_book_one_sided():
    # No bids in book → spread unknown → floor = 0 → only return rate applies.
    # Return rate = ~11% >> 1% → passes.
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    assert evaluate_event("E", s, books) is not None


def test_sell_side_capital_is_collateral():
    # bids 545/545 → proceeds 1090/set; collateral = 2*1000 - 1090 = 910/set
    s = make_state(["A", "B"], bids={"A": 545, "B": 545}, asks={"A": 510, "B": 510})
    books = {
        "A": PublicMarketBook(),
        "B": PublicMarketBook(),
    }
    for ticker in ("A", "B"):
        books[ticker].yes_buys.set_real_qty(545, 100)
        books[ticker].no_buys.set_real_qty(490, 100)
    opp = evaluate_event("E", s, books)
    assert opp is not None
    assert opp.side == "sell"
    assert opp.tiers[0].capital_required == opp.tiers[0].quantity * 910


# Maximum-edge sanity ceiling: too good to be true = model risk

def test_edge_ceiling_rejects_buy_side_too_cheap():
    # joint 510: a 49% "riskless" edge means non-exhaustive event or phantom
    # book, never free money (the METXCOMBO trade)
    s = make_state(["A", "B"], asks={"A": 255, "B": 255})
    books = {
        "A": make_book_with_yes_ask(255, 100),
        "B": make_book_with_yes_ask(255, 100),
    }
    assert evaluate_event("E", s, books) is None


def test_edge_ceiling_rejects_sell_side_too_rich():
    s = make_state(["A", "B"], bids={"A": 600, "B": 600})
    books = {
        "A": make_book_with_yes_bid(600, 100),
        "B": make_book_with_yes_bid(600, 100),
    }
    assert evaluate_event("E", s, books) is None  # joint 1200 > 1100


def test_edge_ceiling_boundary_buy_passes():
    # joint exactly 900 = 100 ticks of edge: at the ceiling, not beyond it
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    assert evaluate_event("E", s, books) is not None


# Funnel telemetry

def test_stats_counts_no_cross():
    s = make_state(["A", "B"], asks={"A": 550, "B": 550})
    books = {
        "A": make_book_with_yes_ask(550, 100),
        "B": make_book_with_yes_ask(550, 100),
    }
    stats: dict = {}
    evaluate_event("E", s, books, stats=stats)
    assert stats == {"evaluated": 1, "no_cross": 1}


def test_stats_counts_edge_ceiling():
    s = make_state(["A", "B"], asks={"A": 255, "B": 255})
    books = {
        "A": make_book_with_yes_ask(255, 100),
        "B": make_book_with_yes_ask(255, 100),
    }
    stats: dict = {}
    evaluate_event("E", s, books, stats=stats)
    assert stats == {"evaluated": 1, "edge_ceiling": 1}


def test_stats_counts_return_rate_rejection():
    # joint 960: crossed and inside ceiling, but ~0.5% net return < 5%
    s = make_state(["A", "B"], asks={"A": 480, "B": 480})
    books = {
        "A": make_book_with_yes_ask(480, 100),
        "B": make_book_with_yes_ask(480, 100),
    }
    stats: dict = {}
    evaluate_event("E", s, books, stats=stats)
    assert stats["evaluated"] == 1
    assert stats["return_rate"] == 1
    assert stats["best_miss_event"] == "E"
    assert stats["best_miss_joint"] == 960
    assert 0 < stats["best_miss_return"] < 0.05


def test_stats_counts_spread_floor_rejection():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": _make_two_sided_book(ask=450, bid=400, qty=100),
        "B": _make_two_sided_book(ask=450, bid=400, qty=100),
    }
    stats: dict = {}
    evaluate_event("E", s, books, stats=stats)
    assert stats["evaluated"] == 1
    assert stats["spread_floor"] == 1
    assert stats["best_miss_event"] == "E"  # net-positive, rejected: a near-miss


def test_stats_counts_min_profit_rejection():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    stats: dict = {}
    evaluate_event("E", s, books, min_profit_dc=7000, stats=stats)
    assert stats["evaluated"] == 1
    assert stats["min_profit"] == 1
    assert stats["best_miss_event"] == "E"


def test_stats_counts_empty_book():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": PublicMarketBook(),
    }
    stats: dict = {}
    evaluate_event("E", s, books, stats=stats)
    assert stats == {"evaluated": 1, "empty_book": 1}


def test_stats_counts_opportunity():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    stats: dict = {}
    opp = evaluate_event("E", s, books, stats=stats)
    assert opp is not None
    assert stats == {"evaluated": 1, "opportunities": 1}


def test_stats_accumulates_across_calls():
    crossed = make_state(["A", "B"], asks={"A": 450, "B": 450})
    flat = make_state(["C", "D"], asks={"C": 550, "D": 550})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
        "C": make_book_with_yes_ask(550, 100),
        "D": make_book_with_yes_ask(550, 100),
    }
    stats: dict = {}
    evaluate_event("E1", crossed, books, stats=stats)
    evaluate_event("E2", flat, books, stats=stats)
    assert stats["evaluated"] == 2
    assert stats["opportunities"] == 1
    assert stats["no_cross"] == 1


def test_stats_none_is_safe():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    assert evaluate_event("E", s, books) is not None  # no stats dict, no error


def test_best_miss_keeps_the_largest_return():
    thin = make_state(["A", "B"], asks={"A": 490, "B": 490})    # ~0.09% net
    thicker = make_state(["C", "D"], asks={"C": 480, "D": 480})  # ~0.5% net
    books = {
        "A": make_book_with_yes_ask(490, 100),
        "B": make_book_with_yes_ask(490, 100),
        "C": make_book_with_yes_ask(480, 100),
        "D": make_book_with_yes_ask(480, 100),
    }
    stats: dict = {}
    evaluate_event("E-THIN", thin, books, stats=stats)
    evaluate_event("E-THICK", thicker, books, stats=stats)
    assert stats["best_miss_event"] == "E-THICK"
    assert stats["best_miss_joint"] == 960


def test_no_best_miss_when_opportunity_found():
    s = make_state(["A", "B"], asks={"A": 450, "B": 450})
    books = {
        "A": make_book_with_yes_ask(450, 100),
        "B": make_book_with_yes_ask(450, 100),
    }
    stats: dict = {}
    assert evaluate_event("E", s, books, stats=stats) is not None
    assert "best_miss_event" not in stats


def test_no_best_miss_for_fee_eaten_crossings():
    # joint 995: crossed but net-negative after fees — not a near-miss
    s = make_state(["A", "B"], asks={"A": 497, "B": 498})
    books = {
        "A": make_book_with_yes_ask(497, 100),
        "B": make_book_with_yes_ask(498, 100),
    }
    stats: dict = {}
    evaluate_event("E", s, books, stats=stats)
    assert stats.get("fees") == 1
    assert "best_miss_event" not in stats
