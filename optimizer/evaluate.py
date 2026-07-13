from book.fees import taker_fee
from book.public_book import PublicMarketBook
from book.types import PRICE_MAX
from graph.event_state import EventState
from .opportunity import Opportunity, OpportunityTier

MIN_RETURN_RATE = 0.02

# Edges beyond this are treated as model risk, not opportunity: a joint price
# this far from $1.00 means a non-exhaustive event, a phantom book, or a payout
# structure we don't understand — never free money someone left on a public
# exchange.
MAX_EDGE_TICKS = 100


def evaluate_event(event_id: str, event_state: EventState, books: dict[str, PublicMarketBook],
                   min_profit_dc: int = 0, stats: dict[str, int] | None = None) -> Opportunity | None:
    """`stats`, when provided, is incremented with funnel telemetry: how many
    evaluations ran and which gate rejected the near-misses."""

    def bump(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    def note_miss(net_return: float, joint: int) -> None:
        # remember the best net-positive candidate that a gate rejected, so the
        # funnel log shows how close the market came to tradeable
        if stats is not None and net_return > stats.get("best_miss_return", 0.0):
            stats["best_miss_return"] = net_return
            stats["best_miss_event"] = event_id
            stats["best_miss_joint"] = joint

    bump("evaluated")

    buy_cost = event_state.total_buy_cost()
    sell_proceeds = event_state.total_sell_proceeds()

    if buy_cost is not None and buy_cost < 1000:
        if 1000 - buy_cost > MAX_EDGE_TICKS:
            bump("edge_ceiling")
            return None
        side = "buy"
        level_key = "yes_asks"
    elif sell_proceeds is not None and sell_proceeds > 1000:
        if sell_proceeds - 1000 > MAX_EDGE_TICKS:
            bump("edge_ceiling")
            return None
        side = "sell"
        level_key = "yes_bids"
    else:
        bump("no_cross")
        return None

    curves: dict[str, list[list[int]]] = {}
    for ticker in event_state.tickers:
        levels = books[ticker].snapshot(depth=PRICE_MAX)[level_key]
        if not levels:
            bump("empty_book")
            return None
        curves[ticker] = [[price, qty] for price, qty in levels]

    spread_floor = sum(
        max(0, ask - bid)
        for t in curves
        if (ask := books[t].best_yes_ask()) is not None
        and (bid := books[t].best_yes_bid()) is not None
    )

    pointers = {ticker: 0 for ticker in curves}
    tiers: list[OpportunityTier] = []
    reject = "fees"

    while True:
        if any(pointers[t] >= len(curves[t]) for t in curves):
            break

        current_prices = {t: curves[t][pointers[t]][0] for t in curves}
        joint_price = sum(current_prices.values())

        remaining_at_level = {t: curves[t][pointers[t]][1] for t in curves}
        batch_qty = min(remaining_at_level.values())

        total_fee = sum(taker_fee(batch_qty, price) for price in current_prices.values())
        if side == "buy":
            profit = batch_qty * (1000 - joint_price) - total_fee
        else:
            profit = batch_qty * (joint_price - 1000) - total_fee

        if profit <= 0:
            reject = "fees"
            break

        if side == "buy":
            capital = batch_qty * joint_price
        else:
            # Selling YES on every leg == buying NO on every leg: the capital
            # consumed is the NO-side collateral, not the sale proceeds.
            capital = batch_qty * (len(curves) * 1000 - joint_price)
        net_return = profit / (capital + total_fee)
        if profit < (capital + total_fee) * MIN_RETURN_RATE:
            reject = "return_rate"
            if not tiers:
                note_miss(net_return, joint_price)
            break
        if spread_floor > 0 and profit < batch_qty * spread_floor:
            reject = "spread_floor"
            if not tiers:
                note_miss(net_return, joint_price)
            break
        if profit < min_profit_dc:
            reject = "min_profit"
            if not tiers:
                note_miss(net_return, joint_price)
            break

        tiers.append(OpportunityTier(
            quantity=batch_qty,
            leg_prices=dict(current_prices),
            capital_required=capital,
            profit=profit,
        ))

        for t in curves:
            if remaining_at_level[t] == batch_qty:
                pointers[t] += 1
            else:
                curves[t][pointers[t]][1] -= batch_qty

    if not tiers:
        bump(reject)
        return None

    bump("opportunities")
    return Opportunity(event_id=event_id, side=side, tiers=tiers)
