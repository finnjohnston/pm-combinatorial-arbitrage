import sqlite3
import time
from pathlib import Path

from execution.models import ExecutionResult, LegFill
from risk.models import ResolutionResult


class TradeDB:

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id              TEXT    NOT NULL,
                side                  TEXT    NOT NULL,
                status                TEXT    NOT NULL,
                opened_at             REAL    NOT NULL,
                closed_at             REAL,
                winner_ticker         TEXT,
                num_legs              INTEGER NOT NULL,
                target_qty            INTEGER NOT NULL,
                capital_deployed      INTEGER NOT NULL,
                hedge_qty             INTEGER NOT NULL,
                estimated_profit      INTEGER NOT NULL,
                realized_profit       INTEGER,
                total_realized_profit INTEGER,
                action                TEXT    NOT NULL,
                fees                  INTEGER NOT NULL,
                fill_rate             REAL    NOT NULL,
                avg_slippage_ticks    REAL    NOT NULL,
                avg_latency_ms        REAL    NOT NULL,
                naked_qty             INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS capital_snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp         REAL    NOT NULL,
                total_capital     INTEGER NOT NULL,
                locked_capital    INTEGER NOT NULL,
                available_capital INTEGER NOT NULL
            );
        """)
        self._conn.commit()

    def record_unwound(self, event_id: str, result: ExecutionResult, resolution: ResolutionResult) -> None:
        legs = list(result.legs.values())
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO trades (
                event_id, side, status, opened_at, closed_at,
                num_legs, target_qty, capital_deployed, hedge_qty,
                estimated_profit, total_realized_profit, action, fees,
                fill_rate, avg_slippage_ticks, avg_latency_ms
            ) VALUES (?, ?, 'unwound', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                result.side,
                now, now,
                len(legs),
                result.target_qty,
                abs(result.total_capital),
                resolution.final_hedge_qty,
                result.estimated_profit,
                resolution.total_realized_profit,
                resolution.action,
                resolution.resolution_fees,
                _fill_rate(resolution.final_hedge_qty, result.target_qty),
                _avg_slippage(legs),
                _avg_latency(legs),
            ),
        )
        self._conn.commit()

    def record_open(self, event_id: str, result: ExecutionResult, resolution: ResolutionResult, capital_deployed: int,
                    naked_qty: int = 0) -> None:
        legs = list(result.legs.values())
        self._conn.execute(
            """
            INSERT INTO trades (
                event_id, side, status, opened_at,
                num_legs, target_qty, capital_deployed, hedge_qty,
                estimated_profit, total_realized_profit, action, fees,
                fill_rate, avg_slippage_ticks, avg_latency_ms, naked_qty
            ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                result.side,
                time.time(),
                len(legs),
                result.target_qty,
                capital_deployed,
                resolution.final_hedge_qty,
                result.estimated_profit,
                resolution.total_realized_profit,
                resolution.action,
                resolution.resolution_fees,
                _fill_rate(resolution.final_hedge_qty, result.target_qty),
                _avg_slippage(legs),
                _avg_latency(legs),
                naked_qty,
            ),
        )
        self._conn.commit()

    def record_close(self, event_id: str, winner_ticker: str, realized_profit: int, total_realized_profit: int) -> None:
        self._conn.execute(
            """
            UPDATE trades
            SET status = 'settled',
                closed_at = ?,
                winner_ticker = ?,
                realized_profit = ?,
                total_realized_profit = ?
            WHERE event_id = ? AND status = 'open'
            """,
            (time.time(), winner_ticker, realized_profit, total_realized_profit, event_id),
        )
        self._conn.commit()

    def force_settle(self, event_id: str, estimated_profit: int) -> None:
        self._conn.execute(
            """
            UPDATE trades
            SET status = 'force_settled',
                closed_at = ?,
                realized_profit = ?,
                total_realized_profit = ?
            WHERE event_id = ? AND status = 'open'
            """,
            (time.time(), estimated_profit, estimated_profit, event_id),
        )
        self._conn.commit()

    def snapshot_capital(self, total_capital: int, locked_capital: int, available_capital: int) -> None:
        self._conn.execute(
            """
            INSERT INTO capital_snapshots (timestamp, total_capital, locked_capital, available_capital)
            VALUES (?, ?, ?, ?)
            """,
            (time.time(), total_capital, locked_capital, available_capital),
        )
        self._conn.commit()

    def open_trades(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def all_trades(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def capital_snapshots(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM capital_snapshots ORDER BY timestamp"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _fill_rate(final_hedge_qty: int, target_qty: int) -> float:
    if target_qty == 0:
        return 0.0
    return final_hedge_qty / target_qty


def _avg_slippage(legs: list[LegFill]) -> float:
    if not legs:
        return 0.0
    return sum(leg.slippage_ticks for leg in legs) / len(legs)


def _avg_latency(legs: list[LegFill]) -> float:
    if not legs:
        return 0.0
    return sum(leg.latency_ms for leg in legs) / len(legs)
