from dataclasses import dataclass


@dataclass(frozen=True)
class OpportunityTier:
    quantity: int
    leg_prices: dict[str, int]
    capital_required: int
    profit: int


@dataclass(frozen=True)
class Opportunity:
    event_id: str
    side: str
    tiers: list[OpportunityTier]

    @property
    def total_capital_required(self) -> int:
        return sum(tier.capital_required for tier in self.tiers)

    @property
    def total_profit(self) -> int:
        return sum(tier.profit for tier in self.tiers)
