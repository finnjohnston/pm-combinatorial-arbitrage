from dataclasses import dataclass, field

from execution.models import ExecutionResult, LegFill


@dataclass
class ResolutionResult:
    original_result: ExecutionResult
    action: str
    resolution_legs: list[LegFill] = field(default_factory=list)
    final_hedge_qty: int = 0
    resolution_capital: int = 0
    resolution_fees: int = 0
    total_realized_profit: int = 0
    # net per-leg quantity after resolution; unequal values mean naked exposure
    final_holdings: dict[str, int] = field(default_factory=dict)
