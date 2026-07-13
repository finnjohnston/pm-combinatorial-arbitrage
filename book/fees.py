def taker_fee(quantity: int, price: int) -> int:
    cents = -(-(7 * quantity * price * (1000 - price)) // 1_000_000)
    return cents * 10


def taker_fee_order(fills) -> int:
    """Fee for one order: Kalshi sums the raw fee across fills and rounds up to
    the next cent once per order (not per fill)."""
    raw = sum(7 * f.qty * f.price * (1000 - f.price) for f in fills)
    if raw == 0:
        return 0
    return -(-raw // 100_000_000) * 1000
