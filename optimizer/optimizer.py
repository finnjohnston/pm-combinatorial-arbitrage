from .opportunity import Opportunity, OpportunityTier


class Optimizer:

    def __init__(self) -> None:
        self.open_opportunities: dict[str, Opportunity] = {}
        self.committed: dict[str, int] = {}

    def update(self, event_id: str, opportunity: Opportunity | None) -> None:
        if opportunity is None:
            self.open_opportunities.pop(event_id, None)
        else:
            self.open_opportunities[event_id] = opportunity

    def commit(self, event_id: str, capital: int) -> None:
        self.committed[event_id] = self.committed.get(event_id, 0) + capital

    def release(self, event_id: str) -> None:
        self.committed.pop(event_id, None)

    def allocate(self, available_capital: int, exclude_events: set[str] | None = None,
                 max_event_capital: int | None = None) -> list[tuple[str, OpportunityTier]]:
        available = available_capital - sum(self.committed.values())
        skip = set(self.committed) | (exclude_events or set())

        pool = [
            (event_id, tier)
            for event_id, opportunity in self.open_opportunities.items()
            for tier in opportunity.tiers
            if event_id not in skip
        ]
        pool.sort(key=lambda pair: pair[1].profit / pair[1].capital_required, reverse=True)

        selected: list[tuple[str, OpportunityTier]] = []
        selected_events: set[str] = set()
        for event_id, tier in pool:
            if event_id in selected_events:
                continue
            if tier.profit <= 0:
                break
            if available <= 0:
                break
            budget = available if max_event_capital is None else min(available, max_event_capital)
            if budget <= 0:
                break
            if tier.capital_required <= budget:
                selected.append((event_id, tier))
                selected_events.add(event_id)
                available -= tier.capital_required
            else:
                partial = self._partial_tier(tier, budget)
                if partial is not None:
                    selected.append((event_id, partial))
                    selected_events.add(event_id)
                    available -= partial.capital_required

        return selected

    @staticmethod
    def _partial_tier(tier: OpportunityTier, available: int) -> OpportunityTier | None:
        unit_capital = tier.capital_required // tier.quantity
        partial_qty = available // unit_capital
        if partial_qty <= 0:
            return None
        return OpportunityTier(
            quantity=partial_qty,
            leg_prices=tier.leg_prices,
            capital_required=partial_qty * unit_capital,
            profit=(tier.profit * partial_qty) // tier.quantity,
        )
