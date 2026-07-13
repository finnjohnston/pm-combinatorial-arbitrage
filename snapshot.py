import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from portfolio.db import TradeDB
from portfolio.metrics import _fmt_duration, _fmt_usd, _fmt_signed_usd, local_day_start


def build_snapshot(db: TradeDB, now: float | None = None) -> str | None:
    snapshots = db.capital_snapshots()
    if not snapshots:
        return None

    now = now if now is not None else time.time()
    started_at = snapshots[0]["timestamp"]
    latest = snapshots[-1]

    total = latest["total_capital"]
    locked = latest["locked_capital"]
    available = latest["available_capital"]

    open_trades = db.open_trades()
    all_trades = db.all_trades()
    closed = [t for t in all_trades if t["status"] != "open"]

    today_start = local_day_start()
    settled_today = [
        t for t in closed
        if t.get("closed_at") and t["closed_at"] >= today_start
    ]
    today_pnl = sum(t["total_realized_profit"] or 0 for t in settled_today)
    total_pnl = sum(t["total_realized_profit"] for t in closed if t.get("total_realized_profit") is not None)

    SEP = "=" * 56
    started_str = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        SEP,
        f"  Snapshot — {now_str}",
        SEP,
        "",
        "Session",
        f"  Started          : {started_str}",
        f"  Runtime          : {_fmt_duration(now - started_at)}",
        "",
        "Capital",
        f"  Total            : {_fmt_usd(total)}",
        f"  Locked           : {_fmt_usd(locked)}",
        f"  Available        : {_fmt_usd(available)}",
        "",
        f"Open Positions  ({len(open_trades)})",
    ]

    if open_trades:
        for t in open_trades:
            qty = t["hedge_qty"]
            avg = t["capital_deployed"] / qty / 1000 if qty else 0.0
            naked = f"  naked={t['naked_qty']}" if t.get("naked_qty") else ""
            lines.append(
                f"  {t['event_id']:<32}  {t['side']}  "
                f"qty={qty}{naked}  avg=${avg:.4f}  cost={_fmt_usd(t['capital_deployed'])}  "
                f"est_pnl={_fmt_signed_usd(t['total_realized_profit'] or 0)}  age={_fmt_duration(now - t['opened_at'])}"
            )
    else:
        lines.append("  none")

    lines += [
        "",
        f"Settled Today  ({len(settled_today)})",
    ]

    if settled_today:
        for t in sorted(settled_today, key=lambda t: t["closed_at"], reverse=True):
            naked = f"  naked={t['naked_qty']}" if t.get("naked_qty") else ""
            lines.append(
                f"  {t['event_id']:<32}  {t['side']}  "
                f"qty={t['hedge_qty']}{naked}  cost={_fmt_usd(t['capital_deployed'])}  "
                f"pnl={_fmt_signed_usd(t['total_realized_profit'] or 0)}  "
                f"held={_fmt_duration(t['closed_at'] - t['opened_at'])}"
            )
    else:
        lines.append("  none")

    lines += [
        "",
        f"Session Totals  ({len(closed)} settled)",
        f"  Total P&L        : {_fmt_signed_usd(total_pnl)}",
        f"  Today's P&L      : {_fmt_signed_usd(today_pnl)}",
        "",
        SEP,
    ]

    return "\n".join(lines)


def main(db_path: str) -> None:
    db = TradeDB(db_path)
    report = build_snapshot(db)
    print(report if report is not None else "No data yet.")
    db.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/trades.db"
    main(path)
