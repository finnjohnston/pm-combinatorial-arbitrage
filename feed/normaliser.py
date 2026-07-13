from book.types import QTY_SCALE


def price_to_ticks(dollar_str: str) -> int:
    """Convert a Kalshi dollar-string price ("0.4200") to integer deci-cents (420)."""
    return round(float(dollar_str) * 1000)


def qty_to_units(qty_str: str | float) -> int:
    """Convert a Kalshi fixed-point quantity string ("10.00") to integer centi-contracts (1000)."""
    return round(float(qty_str) * QTY_SCALE)
