import time
from collections import defaultdict

from book.types import MONEY_SCALE
from datetime import datetime, timezone


def local_day_start(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def daily_report(trades: list[dict], initial_capital: int, current_capital: int, runtime_s: float,
                 since: float | None = None) -> str:
    return _build_report("Daily Report", trades, initial_capital, current_capital, runtime_s,
                         since=since, final=False)


def final_report(trades: list[dict], initial_capital: int, current_capital: int, runtime_s: float) -> str:
    return _build_report("Final Session Report", trades, initial_capital, current_capital, runtime_s,
                         final=True)


def _build_report(title: str, trades: list[dict], initial_capital: int, current_capital: int,
                  runtime_s: float, since: float | None = None, final: bool = False) -> str:
    now = time.time()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    runtime_days = max(runtime_s / 86400, 1e-9)

    closed = [t for t in trades if t["status"] != "open"]
    open_trades = [t for t in trades if t["status"] == "open"]
    force_settled = [t for t in closed if t["status"] == "force_settled"]
    settled = [t for t in closed if t["status"] != "force_settled"]

    return_pct = (current_capital - initial_capital) / initial_capital * 100 if initial_capital else 0.0
    pnl = sum(t["total_realized_profit"] for t in closed if t.get("total_realized_profit") is not None)

    SEP = "=" * 56
    capital_label = "Final capital" if final else "Current capital"

    lines = [
        SEP,
        f"  {title} — {now_str}",
        SEP,
        "",
        "Overview",
        f"  {'Runtime':<17}: {_fmt_duration(runtime_s)}",
        f"  {'Initial capital':<17}: {_fmt_usd(initial_capital)}",
        f"  {capital_label:<17}: {_fmt_usd(current_capital)}  ({return_pct:+.2f}%)",
    ]

    if closed:
        lines += [
            "",
            "P&L",
            f"  {'Total':<17}: {_fmt_signed_usd(pnl)}",
            f"  {'Per day':<17}: {_fmt_signed_usd(pnl / runtime_days)}",
            f"  {'Average/trade':<17}: {_fmt_signed_usd(pnl / len(closed))}",
        ]

        avg_fill = sum(t["fill_rate"] for t in closed) / len(closed)
        avg_slip = sum(t["avg_slippage_ticks"] for t in closed) / len(closed)
        lines += [
            "",
            "Execution",
            f"  {'Fill rate':<17}: {avg_fill * 100:.1f}%",
            f"  {'Slippage':<17}: {avg_slip:+.1f} ticks",
        ]

        by_side: dict[str, list] = defaultdict(list)
        for t in closed:
            by_side[t["side"]].append(t)

        lines += ["", "By side"]
        for side in ("buy", "sell"):
            group = by_side.get(side, [])
            side_pnl = sum(t["total_realized_profit"] for t in group)
            label = "Buy arb" if side == "buy" else "Sell arb"
            lines.append(f"  {label:<17}: {len(group):3d} trades   {_fmt_signed_usd(side_pnl)}")

    if final:
        lines += ["", f"Settled  ({len(settled)})"]
        lines += [_position_line(t, now, closed=True) for t in _by_close_time(settled)] or ["  none"]
        if force_settled:
            lines += ["", f"Force-Settled  ({len(force_settled)})"]
            lines += [_position_line(t, now, closed=True, duration_label="age") for t in _by_close_time(force_settled)]
    else:
        window = [
            t for t in settled
            if t.get("closed_at") and (since is None or t["closed_at"] >= since)
        ]
        lines += ["", f"Settled Since Last Report  ({len(window)})"]
        lines += [_position_line(t, now, closed=True) for t in _by_close_time(window)] or ["  none"]
        lines += ["", f"Open Positions  ({len(open_trades)})"]
        lines += [_position_line(t, now, closed=False) for t in open_trades] or ["  none"]

    lines += ["", SEP]
    return "\n".join(lines)


def _by_close_time(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda t: t.get("closed_at") or 0, reverse=True)


def _position_line(t: dict, now: float, closed: bool, duration_label: str = "held") -> str:
    naked = f"  naked={t['naked_qty']}" if t.get("naked_qty") else ""
    base = f"  {t['event_id']:<34}  {t['side']:<4}  qty={t['hedge_qty']}{naked}  cost={_fmt_usd(t['capital_deployed'])}"
    if closed:
        duration = (t.get("closed_at") or now) - t["opened_at"]
        return f"{base}  pnl={_fmt_signed_usd(t.get('total_realized_profit') or 0)}  {duration_label}={_fmt_duration(duration)}"
    return f"{base}  est_pnl={_fmt_signed_usd(t.get('total_realized_profit') or 0)}  age={_fmt_duration(now - t['opened_at'])}"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_usd(money_units: float) -> str:
    return f"${money_units / MONEY_SCALE:.4f}"


def _fmt_signed_usd(money_units: float) -> str:
    usd = money_units / MONEY_SCALE
    sign = "+" if usd >= 0 else "-"
    return f"{sign}${abs(usd):.4f}"
