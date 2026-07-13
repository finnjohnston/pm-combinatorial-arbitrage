from book.buy_queue import BuyQueue
from book.types import FillResult


def make_queue(levels: dict[int, int]) -> BuyQueue:
    q = BuyQueue()
    for price, qty in levels.items():
        q.set_real_qty(price, qty)
    return q


def test_simulate_fill_greedy_from_best():
    q = make_queue({500: 100, 400: 100})
    result = q.simulate_fill(150)
    assert result.fills[0].price == 500
    assert result.fills[0].qty == 100
    assert result.fills[1].price == 400
    assert result.fills[1].qty == 50
    assert result.filled_qty == 150
    assert result.unfilled_qty == 0


def test_simulate_fill_full_at_single_level():
    q = make_queue({600: 200})
    result = q.simulate_fill(100)
    assert len(result.fills) == 1
    assert result.fills[0].price == 600
    assert result.fills[0].qty == 100
    assert result.filled_qty == 100
    assert result.unfilled_qty == 0


def test_simulate_fill_unfilled_when_insufficient():
    q = make_queue({500: 50})
    result = q.simulate_fill(100)
    assert result.filled_qty == 50
    assert result.unfilled_qty == 50


def test_participation_rate_halves_available():
    q = make_queue({500: 100})
    result = q.simulate_fill(100, participation_rate=0.5)
    assert result.fills[0].qty == 50
    assert result.filled_qty == 50
    assert result.unfilled_qty == 50


def test_participation_rate_integer_floor():
    # 3 * 0.5 = 1.5 → floor → 1
    q = make_queue({500: 3})
    result = q.simulate_fill(3, participation_rate=0.5)
    assert result.fills[0].qty == 1
    assert result.filled_qty == 1


def test_best_price_empty_queue():
    q = BuyQueue()
    assert q.best_price() is None


def test_best_price_returns_highest():
    q = make_queue({300: 10, 500: 20, 400: 15})
    assert q.best_price() == 500


def test_set_real_qty_zero_removes_from_mask():
    q = make_queue({500: 100})
    q.set_real_qty(500, 0)
    assert q.best_price() is None


def test_top_levels_returns_descending():
    q = make_queue({300: 10, 500: 20, 400: 15})
    levels = q.top_levels(2)
    assert levels == [(500, 20), (400, 15)]


def test_simulate_fill_empty_queue():
    q = BuyQueue()
    result = q.simulate_fill(100)
    assert result.filled_qty == 0
    assert result.unfilled_qty == 100
    assert result.fills == []


def test_participation_rate_multi_level():
    q = make_queue({500: 10, 400: 10})
    result = q.simulate_fill(20, participation_rate=0.5)
    # available 5 at 500, 5 at 400 → total 10
    assert result.filled_qty == 10
    assert result.unfilled_qty == 10


def test_simulate_fill_min_price_stops_walking():
    from book.buy_queue import BuyQueue
    q = BuyQueue()
    q.set_real_qty(600, 100)
    q.set_real_qty(500, 100)
    q.set_real_qty(400, 100)

    result = q.simulate_fill(300, min_price=500)

    assert result.filled_qty == 200  # 600 and 500 levels only
    assert all(f.price >= 500 for f in result.fills)
    assert result.unfilled_qty == 100


def test_simulate_fill_no_min_price_walks_all():
    from book.buy_queue import BuyQueue
    q = BuyQueue()
    q.set_real_qty(600, 100)
    q.set_real_qty(400, 100)
    result = q.simulate_fill(200)
    assert result.filled_qty == 200
