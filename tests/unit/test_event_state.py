import pytest
from graph.event_state import EventState


def make_state(tickers):
    return EventState(frozenset(tickers))


def test_total_buy_cost_none_until_all_asks_present():
    s = make_state(["A", "B"])
    assert s.total_buy_cost() is None
    s.update_market("A", 400, 450)
    assert s.total_buy_cost() is None
    s.update_market("B", 400, 450)
    assert s.total_buy_cost() == 900


def test_total_sell_proceeds_none_until_all_bids_present():
    s = make_state(["A", "B"])
    assert s.total_sell_proceeds() is None
    s.update_market("A", 550, 600)
    assert s.total_sell_proceeds() is None
    s.update_market("B", 550, 600)
    assert s.total_sell_proceeds() == 1100


def test_buy_cost_correct_sum():
    s = make_state(["A", "B", "C"])
    s.update_market("A", None, 300)
    s.update_market("B", None, 350)
    s.update_market("C", None, 280)
    assert s.total_buy_cost() == 930


def test_sell_proceeds_correct_sum():
    s = make_state(["A", "B"])
    s.update_market("A", 600, None)
    s.update_market("B", 500, None)
    assert s.total_sell_proceeds() == 1100


def test_update_replaces_old_ask_in_sum():
    s = make_state(["A", "B"])
    s.update_market("A", None, 300)
    s.update_market("B", None, 300)
    assert s.total_buy_cost() == 600
    s.update_market("A", None, 400)
    assert s.total_buy_cost() == 700


def test_update_replaces_old_bid_in_sum():
    s = make_state(["A", "B"])
    s.update_market("A", 500, None)
    s.update_market("B", 500, None)
    assert s.total_sell_proceeds() == 1000
    s.update_market("A", 600, None)
    assert s.total_sell_proceeds() == 1100


def test_unknown_ticker_raises():
    s = make_state(["A"])
    with pytest.raises(KeyError):
        s.update_market("X", 500, 600)


def test_missing_count_tracks_none_transitions():
    s = make_state(["A", "B"])
    s.update_market("A", 500, 400)
    s.update_market("B", 500, 400)
    assert s.total_buy_cost() == 800
    s.update_market("A", 500, None)
    assert s.total_buy_cost() is None
