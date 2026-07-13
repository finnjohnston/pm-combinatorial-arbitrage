from book.fees import taker_fee, taker_fee_order
from book.types import Fill


def test_order_fee_single_fill_rounds_up_to_cent():
    # 1 contract (100 units) at $0.50: raw fee $0.0175 → ceil to $0.02 = 2000 units
    assert taker_fee_order([Fill(price=500, qty=100)]) == 2000


def test_order_fee_rounds_once_not_per_fill():
    # each fill's raw fee is 0.336 cents; per-fill ceiling would charge 2 cents,
    # per-order rounds the 0.672-cent sum up once → 1 cent
    fills = [Fill(price=600, qty=20), Fill(price=600, qty=20)]
    assert taker_fee_order(fills) == 1000


def test_order_fee_empty_fills_is_zero():
    assert taker_fee_order([]) == 0


def test_order_fee_never_below_per_fill_raw_sum():
    fills = [Fill(price=450, qty=100), Fill(price=480, qty=50)]
    raw_units = sum(7 * f.qty * f.price * (1000 - f.price) for f in fills) / 100_000
    assert taker_fee_order(fills) >= raw_units
