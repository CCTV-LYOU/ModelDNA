"""只读监控面板:读取 SQLite,展示权益曲线、每只虾的状态与诚实统计。

用法(项目根目录):
  python dashboard/app.py [--config config.yaml] [--port 8787]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flask import Flask, render_template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.bot import load_config  # noqa: E402
from trader.store import Store  # noqa: E402

app = Flask(__name__)
CFG: dict = {}


def max_drawdown(series: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return mdd


@app.route("/")
def index():
    store = Store(CFG["db_path"])
    try:
        curve = store.equity_curve()
        totals = [v for _, v in curve]
        initial_total = totals[0] if totals else len(CFG["symbols"]) * CFG["initial_balance"]
        current_total = totals[-1] if totals else initial_total

        day = curve[-1][0][:10] if curve else ""
        day_start = store.day_start_total(day) if day else None

        crayfish = []
        realized_sum = fees_sum = 0.0
        wins = closed = 0
        for i, symbol in enumerate(CFG["symbols"], start=1):
            acc = store.ensure_account(symbol, CFG["initial_balance"])
            stats = store.symbol_stats(symbol)
            realized_sum += stats["realized"]
            fees_sum += stats["fees"]
            wins += stats["wins"]
            closed += stats["closed"]
            last_eq = store.conn.execute(
                "SELECT price, equity FROM equity WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            price = last_eq["price"] if last_eq else 0.0
            equity = last_eq["equity"] if last_eq else acc.balance
            last_dec = store.recent("decisions", 1, symbol)
            crayfish.append({
                "tag": f"#{i:03d}",
                "symbol": symbol,
                "has_position": acc.has_position,
                "entry_price": acc.entry_price,
                "stop_price": acc.stop_price,
                "price": price,
                "equity": equity,
                "pnl": equity - CFG["initial_balance"],
                "stats": stats,
                "last": dict(last_dec[0]) if last_dec else None,
            })

        summary = {
            "current_total": current_total,
            "total_pnl": current_total - initial_total,
            "total_pnl_pct": (current_total / initial_total - 1) * 100 if initial_total else 0,
            "today_pnl": (current_total - day_start) if day_start else 0.0,
            "realized": realized_sum,
            "fees": fees_sum,
            "win_rate": wins / closed if closed else None,
            "closed": closed,
            "max_drawdown": max_drawdown(totals),
            "cycles": len(curve),
        }
        return render_template(
            "index.html",
            summary=summary,
            crayfish=crayfish,
            curve=[{"ts": ts, "total": round(v, 2)} for ts, v in curve],
            decisions=[dict(r) for r in store.recent("decisions", 15)],
            trades=[dict(r) for r in store.recent("trades", 15)],
        )
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    CFG.update(load_config(args.config))
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
