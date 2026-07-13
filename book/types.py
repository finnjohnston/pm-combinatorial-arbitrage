from dataclasses import dataclass, field

# Kalshi's finest real tick size is the deci-cent
PRICE_MIN = 1
PRICE_MAX = 999

# Kalshi's *_fp contract-count fields carry 2 decimal places. Internally we represent quantity as integer "centi-contracts" to avoid float rounding entirely.
QTY_SCALE = 100

# All money amounts (capital, profit, fees) are in "money units": the product of a
# centi-contract quantity and a deci-cent price, i.e. 1 unit = 10^-5 dollars.
MONEY_SCALE = 100_000


def dollars_to_ticks(dollars: str) -> int:
    """Convert a Kalshi dollar price string to integer deci-cents."""
    return round(float(dollars) * 1000)


def ticks_to_dollars(ticks: int) -> str:
    """Convert integer deci-cents back to a Kalshi-style dollar string."""
    return f"{ticks / 1000:.4f}"


def contracts_to_units(contracts: str) -> int:
    """Convert a Kalshi *_fp contract-count string to integer centi-contracts."""
    return round(float(contracts) * QTY_SCALE)


def units_to_contracts(units: int) -> str:
    """Convert integer centi-contracts back to a Kalshi-style *_fp string."""
    return f"{units / QTY_SCALE:.2f}"


@dataclass(frozen=True)
class Fill:
    """A single fill produced by walking a price queue."""

    price: int
    qty: int


@dataclass
class FillResult:
    """The outcome of matching a hypothetical order against real liquidity."""

    fills: list[Fill] = field(default_factory=list)
    filled_qty: int = 0
    unfilled_qty: int = 0

    @property
    def avg_price(self) -> float | None:
        if self.filled_qty == 0:
            return None
        total_cost = sum(fill.price * fill.qty for fill in self.fills)
        return total_cost / self.filled_qty