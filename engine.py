import asyncio
import logging
import random

from book.public_book import PublicMarketBook
from book.types import MONEY_SCALE
from execution.config import ExecutionConfig
from execution.executor import execute_tier
from execution.models import ExecutionResult, LegFill
from feed.feed import KalshiFeed
from feed.rest_client import KalshiRestClient, classify_event, fetch_all_mx_events, fetch_event_markets
from graph.graph import StateGraph
from optimizer.evaluate import evaluate_event
from optimizer.opportunity import OpportunityTier
from optimizer.optimizer import Optimizer
from portfolio.ledger import Ledger
from risk.manager import RiskManager

logger = logging.getLogger("engine")
trading_logger = logging.getLogger("trading")
feed_logger = logging.getLogger("feed")

_DISCOVERY_INTERVAL_S = 300
_BOOT_EVAL_DELAY_S = 60
_MIN_PROFIT_RATE = 0.0005
_DRIFT_CAP_TICKS = 100
_DEMOTION_CYCLES = 3
_RECONCILE_PAUSE_S = 0.15
_PERSISTENCE_DELAY_S = 3.0
_MAX_EVENT_CAPITAL_RATE = 0.10


class Engine:

    def __init__(self, books: dict[str, PublicMarketBook], graph: StateGraph, optimizer: Optimizer, ledger: Ledger, event_queue: asyncio.Queue, resolution_queue: asyncio.Queue,
                 config: ExecutionConfig, risk_manager: RiskManager, feed: KalshiFeed, rest_client: KalshiRestClient, rng: random.Random | None = None,
                 cold_events: dict[str, list[str]] | None = None,
                 persistence_delay_s: float = _PERSISTENCE_DELAY_S) -> None:
        self._books = books
        self._graph = graph
        self._optimizer = optimizer
        self._ledger = ledger
        self._event_queue = event_queue
        self._resolution_queue = resolution_queue
        self._config = config
        self._risk_manager = risk_manager
        self._feed = feed
        self._rest_client = rest_client
        self._rng = rng or random.Random()
        self._cold_events: dict[str, list[str]] = cold_events or {}
        self._persistence_delay_s = persistence_delay_s
        self._tasks: set[asyncio.Task] = set()
        self._pending_repairs: set[str] = set()
        self._settled_events = 0
        self._settled_tickers = 0
        self._demotion_strikes: dict[str, int] = {}
        self._eval_stats: dict[str, int] = {}

    async def run(self) -> None:
        await asyncio.gather(
            self._arb_loop(),
            self._resolution_loop(),
            self._discovery_loop(),
        )

    async def _arb_loop(self) -> None:
        while True:
            try:
                event_id = await self._event_queue.get()
                pending = {event_id}
                while not self._event_queue.empty():
                    pending.add(self._event_queue.get_nowait())
                for eid in pending:
                    self._evaluate_and_dispatch(eid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("ERROR  source=arb  msg=%s", exc)

    def _evaluate_and_dispatch(self, event_id: str) -> None:
        if event_id in self._pending_repairs:
            # fires once every ticker of the rebuilt event has a fresh book
            self._pending_repairs.discard(event_id)
            task = asyncio.create_task(self._repair_position(event_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        event_state = self._graph.events.get(event_id)
        if event_state is None:
            return

        if any(ticker not in self._books for ticker in event_state.tickers):
            return

        for ticker in event_state.tickers:
            book = self._books[ticker]
            self._graph.update_market(ticker, book.best_yes_bid(), book.best_yes_ask())

        min_profit = int(self._ledger.available_capital * _MIN_PROFIT_RATE)
        opportunity = evaluate_event(event_id, event_state, self._books, min_profit_dc=min_profit,
                                     stats=self._eval_stats)
        self._optimizer.update(event_id, opportunity)

        max_event_capital = int(self._ledger.total_capital * _MAX_EVENT_CAPITAL_RATE)
        allocations = self._optimizer.allocate(
            self._ledger.available_capital, self._ledger.open_event_ids(), max_event_capital,
        )
        for alloc_event_id, tier in allocations:
            side = self._optimizer.open_opportunities[alloc_event_id].side
            self._optimizer.commit(alloc_event_id, tier.capital_required)
            task = asyncio.create_task(self._execute_and_settle(alloc_event_id, side, tier))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute_and_settle(self, event_id: str, side: str, tier: OpportunityTier) -> None:
        try:
            # persistence gate: phantom quotes (cancel-on-touch flicker) die on
            # the sub-second scale; genuine abandoned mispricings persist for
            # minutes. Waiting turns the recheck below into a durability filter —
            # capital stays committed so the allocation can't be double-spent.
            if self._persistence_delay_s > 0:
                await asyncio.sleep(self._persistence_delay_s)

            event_state = self._graph.events.get(event_id)
            min_profit = int(self._ledger.available_capital * _MIN_PROFIT_RATE)
            if event_state is None or evaluate_event(event_id, event_state, self._books, min_profit_dc=min_profit) is None:
                self._optimizer.release(event_id)
                logger.info("ABORT  %s  reason=reeval  est=$%.4f", event_id, tier.profit / MONEY_SCALE)
                return

            max_drift = min(tier.profit // tier.quantity // 2, _DRIFT_CAP_TICKS) if tier.quantity else _DRIFT_CAP_TICKS
            for ticker, expected in tier.leg_prices.items():
                book = self._books.get(ticker)
                current = book.best_yes_ask() if side == "buy" else book.best_yes_bid() if book else None
                if current is None or abs(current - expected) > max_drift:
                    self._optimizer.release(event_id)
                    logger.info(
                        "ABORT  %s  reason=drift  ticker=%s  expected=%d  current=%s  est=$%.4f",
                        event_id, ticker, expected, current, tier.profit / MONEY_SCALE,
                    )
                    return

            result = await execute_tier(event_id, side, tier, self._books, self._config, self._rng)
            resolution = await self._risk_manager.handle(result)

            total_filled = sum(l.filled_qty for l in result.legs.values())
            if total_filled == 0 and not resolution.resolution_legs:
                self._optimizer.release(event_id)
                logger.info("ABORT  %s  reason=zero_fill  est=$%.4f", event_id, tier.profit / MONEY_SCALE)
                return

            holdings = resolution.final_holdings
            flat = all(q == 0 for q in holdings.values()) if holdings else resolution.final_hedge_qty == 0
            if flat:
                self._ledger.record_unwound(event_id, result, resolution)
            else:
                # any residual exposure — hedged or not — is tracked to settlement;
                # only a truly flat book realizes cash P&L now
                self._ledger.record_open(event_id, result, resolution)
                if self._ledger.available_capital < 0:
                    trading_logger.warning(
                        "CAPITAL  available went negative after %s: $%.4f",
                        event_id, self._ledger.available_capital / MONEY_SCALE,
                    )
            filled_legs = [l for l in result.legs.values() if l.filled_qty > 0]
            avg_slip = (
                sum(l.slippage_ticks for l in filled_legs) / len(filled_legs)
                if filled_legs else 0.0
            )
            fill_pct = int(resolution.final_hedge_qty / result.target_qty * 100) if result.target_qty else 0
            avg_usd = result.total_capital / total_filled / 1000 if total_filled else 0
            naked = sum(q - resolution.final_hedge_qty for q in holdings.values()) if holdings else 0
            leg_summary = " ".join(
                f"{t}:{lf.filled_qty}/{lf.requested_qty}" for t, lf in result.legs.items()
            )
            trading_logger.info(
                "TRADE  %s  %s  hedge=%d/%d(%d%%)  naked=%d  avg=$%.4f  cost=$%.4f  est=$%.4f  pnl=$%.4f  slip=%+.1ft  action=%s  [%s]",
                event_id, side,
                resolution.final_hedge_qty, result.target_qty, fill_pct, naked,
                avg_usd, result.total_capital / MONEY_SCALE,
                result.estimated_profit / MONEY_SCALE, resolution.total_realized_profit / MONEY_SCALE,
                avg_slip, resolution.action, leg_summary,
            )
        except Exception as exc:
            trading_logger.error("ERROR  source=execute  event=%s  msg=%s", event_id, exc)
            self._optimizer.release(event_id)

    async def _resolution_loop(self) -> None:
        while True:
            try:
                winner_ticker = await self._resolution_queue.get()
                event_id = self._graph.ticker_to_event.get(winner_ticker)
                if event_id is None:
                    continue
                event_state = self._graph.events.get(event_id)
                n_tickers = len(event_state.tickers) if event_state else 0
                had_position = self._ledger.has_open_position(event_id)
                profit = self._ledger.record_close(event_id, winner_ticker)
                if had_position:
                    trading_logger.info("SETTLE  %s  winner=%s  profit=$%.4f", event_id, winner_ticker, profit / MONEY_SCALE)
                await self._cleanup_event(event_id)
                self._settled_events += 1
                self._settled_tickers += n_tickers
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("ERROR  source=resolve  msg=%s", exc)

    async def _cleanup_event(self, event_id: str) -> None:
        self._cold_events.pop(event_id, None)
        self._demotion_strikes.pop(event_id, None)
        event_state = self._graph.events.pop(event_id, None)
        if event_state is None:
            return
        for ticker in event_state.tickers:
            self._graph.ticker_to_event.pop(ticker, None)
            self._books.pop(ticker, None)
        self._optimizer.update(event_id, None)
        await self._feed.remove_event(event_id, event_state.tickers)

    async def _discovery_loop(self) -> None:
        # the boot sweep (first sight of every hot book) is the most diagnostic
        # evaluation sample; emit it on its own instead of blending it into the
        # first discovery cycle's funnel
        await asyncio.sleep(_BOOT_EVAL_DELAY_S)
        self._log_eval_stats()
        await asyncio.sleep(_DISCOVERY_INTERVAL_S - _BOOT_EVAL_DELAY_S)
        while True:
            try:
                events = await asyncio.to_thread(fetch_all_mx_events, self._rest_client)
                await self._process_discovered_events(events)
            except Exception as exc:
                logger.warning("ERROR  source=discovery  msg=%s", exc)
            await asyncio.sleep(_DISCOVERY_INTERVAL_S)

    async def _process_discovered_events(self, events: list[dict]) -> None:
        new_hot: list[tuple[str, list[str]]] = []
        new_cold: list[tuple[str, list[str]]] = []
        promoted: list[tuple[str, list[str]]] = []

        returned_ids = {
            event.get("event_ticker")
            for event in events
            if event.get("mutually_exclusive")
        }
        removed_events = 0
        removed_tickers = 0
        for event_id in list(self._cold_events):
            if event_id not in returned_ids:
                removed_tickers += len(self._cold_events[event_id])
                removed_events += 1
                await self._cleanup_event(event_id)

        reconciled = await self._reconcile_missing_hot_events(returned_ids)

        resynced = 0
        demoted: list[tuple[str, list[str]]] = []
        for event in events:
            if not event.get("mutually_exclusive"):
                continue
            event_id = event.get("event_ticker")
            tickers = [m["ticker"] for m in event.get("markets", [])]
            if len(tickers) < 2:
                continue

            event_state = self._graph.events.get(event_id)
            if event_state is not None and set(tickers) != set(event_state.tickers):
                # Kalshi added/removed markets on this event; a stale leg set breaks
                # the mutually-exclusive payout guarantee, so re-sync before trading.
                if self._ledger.has_open_position(event_id):
                    handled = await self._handle_membership_change_with_position(event_id, event_state, tickers)
                    if not handled:
                        logger.error(
                            "MEMBERSHIP  %s markets changed while position open — keeping stale set until settlement",
                            event_id,
                        )
                    continue
                await self._cleanup_event(event_id)
                resynced += 1
                event_state = None

            if event_state is None:
                self._graph.add_event(event_id, tickers)
                if classify_event(event):
                    for ticker in tickers:
                        self._books[ticker] = PublicMarketBook()
                    new_hot.append((event_id, tickers))
                else:
                    self._cold_events[event_id] = tickers
                    new_cold.append((event_id, tickers))

            elif event_id in self._cold_events and classify_event(event):
                self._cold_events.pop(event_id)
                for ticker in tickers:
                    self._books[ticker] = PublicMarketBook()
                promoted.append((event_id, tickers))

            elif event_id not in self._cold_events:
                # currently hot: demote after _DEMOTION_CYCLES consecutive
                # failing liquidity checks (hysteresis against flapping)
                if classify_event(event):
                    self._demotion_strikes.pop(event_id, None)
                elif (self._ledger.has_open_position(event_id)
                      or event_id in self._optimizer.committed
                      or event_id in self._pending_repairs):
                    self._demotion_strikes.pop(event_id, None)
                else:
                    strikes = self._demotion_strikes.get(event_id, 0) + 1
                    if strikes < _DEMOTION_CYCLES:
                        self._demotion_strikes[event_id] = strikes
                    else:
                        self._demotion_strikes.pop(event_id, None)
                        for ticker in tickers:
                            self._books.pop(ticker, None)
                        self._cold_events[event_id] = tickers
                        self._optimizer.update(event_id, None)
                        await self._feed.remove_event(event_id, tickers)
                        await self._feed.subscribe_new(event_id, tickers, hot=False)
                        demoted.append((event_id, tickers))

        for event_id, tickers in new_hot:
            await self._feed.subscribe_new(event_id, tickers, hot=True)
        for event_id, tickers in new_cold:
            await self._feed.subscribe_new(event_id, tickers, hot=False)
        for event_id, tickers in promoted:
            await self._feed.subscribe_orderbook(event_id, tickers)

        hot_e, hot_t = len(new_hot), sum(len(t) for _, t in new_hot)
        cold_e, cold_t = len(new_cold), sum(len(t) for _, t in new_cold)
        prom_e, prom_t = len(promoted), sum(len(t) for _, t in promoted)
        dem_e, dem_t = len(demoted), sum(len(t) for _, t in demoted)
        settled_e, settled_t = self._settled_events, self._settled_tickers
        self._settled_events = 0
        self._settled_tickers = 0
        if hot_e or cold_e or prom_e or removed_events or resynced or settled_e or reconciled or dem_e:
            feed_logger.info(
                "DISCOVERY  +hot=%d(%d tickers)  +cold=%d(%d tickers)  +promoted=%d(%d tickers)  -removed=%d(%d tickers)  "
                "-settled=%d(%d tickers)  -demoted=%d(%d tickers)  resynced=%d  total_hot=%d(%d tickers)  total_cold=%d(%d tickers)",
                hot_e, hot_t, cold_e, cold_t, prom_e, prom_t, removed_events, removed_tickers,
                settled_e, settled_t, dem_e, dem_t, resynced,
                self._feed.hot_event_count, self._feed.hot_ticker_count,
                self._feed.cold_event_count, self._feed.cold_ticker_count,
            )

        self._log_eval_stats()

    def _log_eval_stats(self) -> None:
        stats = self._eval_stats
        self._eval_stats = {}
        if "best_miss_event" in stats:
            best_miss = "%s@%.2f%% joint=%d" % (
                stats["best_miss_event"], stats["best_miss_return"] * 100, stats["best_miss_joint"],
            )
        else:
            best_miss = "none"
        logger.info(
            "EVAL  evaluated=%d  no_cross=%d  edge_ceiling=%d  empty_book=%d  fees=%d  "
            "return_rate=%d  spread_floor=%d  min_profit=%d  opportunities=%d  best_miss=%s",
            stats.get("evaluated", 0), stats.get("no_cross", 0), stats.get("edge_ceiling", 0),
            stats.get("empty_book", 0), stats.get("fees", 0), stats.get("return_rate", 0),
            stats.get("spread_floor", 0), stats.get("min_profit", 0), stats.get("opportunities", 0),
            best_miss,
        )

    async def _reconcile_missing_hot_events(self, returned_ids: set) -> int:
        missing = [
            event_id for event_id in list(self._graph.events)
            if event_id not in returned_ids and event_id not in self._cold_events
        ]
        reconciled = 0
        for i, event_id in enumerate(missing):
            if i:
                await asyncio.sleep(_RECONCILE_PAUSE_S)
            try:
                markets = await asyncio.to_thread(fetch_event_markets, self._rest_client, event_id)
            except Exception as exc:
                logger.warning("ERROR  source=reconcile  event=%s  msg=%s", event_id, exc)
                continue
            if not markets:
                continue

            winner = next((m.get("ticker") for m in markets if m.get("result") == "yes"), None)
            if winner:
                logger.debug("RECONCILE  %s  winner=%s  (settled while unobserved)", event_id, winner)
                reconciled += 1
                if winner in self._graph.ticker_to_event:
                    self._resolution_queue.put_nowait(winner)
                else:
                    # Winner is a market we never tracked (membership changed after
                    # entry) — resolve by event id since the ticker lookup in the
                    # resolution loop would miss.
                    if self._ledger.has_open_position(event_id):
                        logger.error(
                            "RECONCILE  %s won by untracked market %s — booked profit assumed a "
                            "tracked leg would win and may overstate P&L",
                            event_id, winner,
                        )
                    event_state = self._graph.events.get(event_id)
                    n_tickers = len(event_state.tickers) if event_state else 0
                    profit = self._ledger.record_close(event_id, winner)
                    if profit:
                        trading_logger.info("SETTLE  %s  winner=%s  profit=$%.4f", event_id, winner, profit / MONEY_SCALE)
                    await self._cleanup_event(event_id)
                    self._settled_events += 1
                    self._settled_tickers += n_tickers
            elif all(m.get("result") or m.get("status") == "settled" for m in markets):
                if self._ledger.has_open_position(event_id):
                    logger.error("RECONCILE  %s finalized without winner but position open — leaving in place", event_id)
                else:
                    logger.debug("RECONCILE  %s finalized without winner (voided) — cleaning up", event_id)
                    reconciled += 1
                    event_state = self._graph.events.get(event_id)
                    n_tickers = len(event_state.tickers) if event_state else 0
                    await self._cleanup_event(event_id)
                    self._settled_events += 1
                    self._settled_tickers += n_tickers
            # else: closed but not yet settled — check again next cycle
        return reconciled

    async def _handle_membership_change_with_position(self, event_id: str, event_state, tickers: list[str]) -> bool:
        record = self._ledger.get_position(event_id)
        known = set(event_state.tickers)
        added = set(tickers) - known
        removed = known - set(tickers)
        if record is None or removed:
            # a removed market while holding is usually a per-leg void with a refund
            # we don't model — surface it instead of guessing
            return False

        # Rebuild graph/books/subscriptions around the full new set so settlement
        # and evaluation see every leg.
        self._graph.events.pop(event_id, None)
        for ticker in known:
            self._graph.ticker_to_event.pop(ticker, None)
        self._graph.add_event(event_id, tickers)
        for ticker in added:
            self._books[ticker] = PublicMarketBook()
        await self._feed.add_tickers(event_id, sorted(added))

        if record.side == "buy":
            # our YES set no longer covers every outcome: decide complete-vs-unwind
            # once the new legs' books are snapshotted
            self._pending_repairs.add(event_id)
            logger.warning(
                "MEMBERSHIP  %s gained %s while buy position open — repair queued (complete vs unwind)",
                event_id, sorted(added),
            )
        else:
            # sell side: an added market can only help (every NO leg we hold pays
            # out if the new market wins) — no trading needed
            logger.info("MEMBERSHIP  %s gained %s with sell position — no action needed", event_id, sorted(added))
        return True

    async def _repair_position(self, event_id: str) -> None:
        try:
            record = self._ledger.get_position(event_id)
            event_state = self._graph.events.get(event_id)
            if record is None or event_state is None:
                return
            holdings = record.holdings or {t: record.hedge_qty for t in record.result.legs}
            if not any(holdings.values()):
                return
            side = record.side
            max_qty = max(holdings.values())
            legs = {}
            fees_assigned = False
            for ticker in event_state.tickers:
                filled = holdings.get(ticker, 0)
                # carry the fees already paid so settlement accounting stays exact
                fee = 0 if fees_assigned else record.fees_total
                fees_assigned = True
                legs[ticker] = LegFill(
                    ticker=ticker, side=side,
                    requested_qty=max_qty, filled_qty=filled, unfilled_qty=max_qty - filled,
                    avg_price=None, expected_price=0, fee=fee, latency_ms=0.0, timestamp_ms=0.0,
                )
            synthetic = ExecutionResult(
                event_id=event_id, side=side, target_qty=max_qty, legs=legs,
                total_capital=record.cost_basis,
                estimated_profit=record.resolution.total_realized_profit,
                realized_profit=-record.cost_basis,
            )
            # cost basis is already locked, so the full available balance is spendable
            resolution = await self._risk_manager.handle(synthetic, budget=max(0, self._ledger.available_capital))
            resolution_filled = sum(l.filled_qty for l in resolution.resolution_legs)

            if resolution.final_hedge_qty > 0:
                profit = self._ledger.record_repair(event_id, synthetic, resolution)
                trading_logger.info(
                    "REPAIR  %s completed  hedge=%d  locked_pnl=$%.4f",
                    event_id, resolution.final_hedge_qty, profit / MONEY_SCALE,
                )
            elif resolution_filled == 0:
                # nothing could be done (no cash to complete, no bids to unwind):
                # the contracts are still held, so the position must not be closed
                self._pending_repairs.add(event_id)
                trading_logger.error(
                    "REPAIR  %s could not complete or unwind — holding unhedged, will retry",
                    event_id,
                )
            else:
                profit = self._ledger.record_repair(event_id, synthetic, resolution)
                still_held = any(resolution.final_holdings.values()) if resolution.final_holdings else False
                if still_held:
                    trading_logger.warning("REPAIR  %s partially unwound — naked exposure remains", event_id)
                else:
                    trading_logger.warning("REPAIR  %s unwound  pnl=$%.4f", event_id, profit / MONEY_SCALE)
        except Exception as exc:
            logger.error("ERROR  source=repair  event=%s  msg=%s", event_id, exc)
