import argparse
import asyncio
import logging
import os
import random
import signal
import time
from pathlib import Path

from book.types import MONEY_SCALE
from execution.config import ExecutionConfig
from feed.feed import KalshiFeed
from feed.rest_client import KalshiRestClient, fetch_all_mx_events, build_graph_and_books
from optimizer.optimizer import Optimizer
from portfolio.db import TradeDB
from portfolio.ledger import Ledger
from portfolio.metrics import daily_report, final_report, local_day_start
from engine import Engine
from risk.manager import RiskManager

def _setup_logging(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_dir / "engine.log")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    reports_logger = logging.getLogger("reports")
    reports_handler = logging.FileHandler(log_dir / "reports.log", mode="w")
    reports_handler.setFormatter(logging.Formatter("%(message)s"))
    reports_logger.addHandler(reports_handler)
    reports_logger.propagate = False


    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    return reports_logger


async def _daily_report_task(db: TradeDB, reports_logger: logging.Logger, initial_capital: int, start_time: float) -> None:
    logger = logging.getLogger("main")
    last_report_at = start_time
    while True:
        # fire at local midnight so each report covers a real calendar day
        sleep_s = local_day_start() + 86400 - time.time()
        await asyncio.sleep(max(sleep_s, 60))
        try:
            trades = db.all_trades()
            snapshots = db.capital_snapshots()
            current_capital = snapshots[-1]["total_capital"] if snapshots else initial_capital
            report = daily_report(trades, initial_capital, current_capital, time.time() - start_time,
                                  since=last_report_at)
            last_report_at = time.time()
            reports_logger.info(report)
        except Exception as exc:
            logger.error("ERROR  source=report  msg=%s", exc)


async def run(initial_capital: int, log_dir: Path) -> None:
    logger = logging.getLogger("main")
    reports_logger = _setup_logging(log_dir)

    key_id = os.environ.get("KALSHI_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not key_id or not private_key_path:
        raise RuntimeError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY env vars must be set")

    rest_client = KalshiRestClient()
    events = fetch_all_mx_events(rest_client)
    graph, books, cold_events = build_graph_and_books(events)
    hot_count = len(graph.events) - len(cold_events)
    cold_ticker_count = sum(len(tickers) for tickers in cold_events.values())
    logger.info(
        "LOAD  events=%d  hot=%d(%d tickers)  cold=%d(%d tickers)",
        len(graph.events), hot_count, len(books), len(cold_events), cold_ticker_count,
    )

    db_path = log_dir / "trades.db"
    db_path.unlink(missing_ok=True)
    db = TradeDB(db_path)
    config = ExecutionConfig()
    rng = random.Random()
    optimizer = Optimizer()
    ledger = Ledger(initial_capital=initial_capital, optimizer=optimizer, db=db)
    risk_manager = RiskManager(books=books, config=config, rng=rng, capital_provider=lambda: ledger.available_capital)

    event_queue: asyncio.Queue = asyncio.Queue()
    resolution_queue: asyncio.Queue = asyncio.Queue()

    feed = KalshiFeed(
        books=books,
        graph=graph,
        event_queue=event_queue,
        resolution_queue=resolution_queue,
        key_id=key_id,
        private_key_path=private_key_path,
        cold_events=cold_events,
    )
    engine = Engine(
        books=books,
        graph=graph,
        optimizer=optimizer,
        ledger=ledger,
        event_queue=event_queue,
        resolution_queue=resolution_queue,
        config=config,
        risk_manager=risk_manager,
        feed=feed,
        rest_client=rest_client,
        rng=rng,
        cold_events=cold_events,
    )

    start_time = time.time()
    db.snapshot_capital(ledger.total_capital, ledger.locked_capital, ledger.available_capital)
    logger.info("START  capital=$%.4f", initial_capital / MONEY_SCALE)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    def _handle_signal():
        print()
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _handle_signal)
    loop.add_signal_handler(signal.SIGTERM, _handle_signal)

    feed_task = asyncio.create_task(feed.run())
    engine_task = asyncio.create_task(engine.run())
    report_task = asyncio.create_task(_daily_report_task(db, reports_logger, initial_capital, start_time))

    await stop_event.wait()

    for task in (feed_task, engine_task, report_task):
        task.cancel()
    await asyncio.gather(feed_task, engine_task, report_task, return_exceptions=True)

    total_profit = ledger.force_settle_all()
    db.snapshot_capital(ledger.total_capital, ledger.locked_capital, ledger.available_capital)
    logger.info("SHUTDOWN  settled=$%.4f", total_profit / MONEY_SCALE)

    trades = db.all_trades()
    snapshots = db.capital_snapshots()
    current_capital = snapshots[-1]["total_capital"] if snapshots else initial_capital
    report = final_report(trades, initial_capital, current_capital, time.time() - start_time)
    reports_logger.info(report)

    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi arbitrage engine")
    parser.add_argument("--capital", type=float, required=True, help="Starting capital in USD")
    args = parser.parse_args()

    initial_capital = int(args.capital * MONEY_SCALE)
    log_dir = Path("logs")

    asyncio.run(run(initial_capital, log_dir))


if __name__ == "__main__":
    main()
