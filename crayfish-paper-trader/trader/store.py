"""SQLite 存储层:账户状态、成交、AI 决策、权益快照。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  symbol       TEXT PRIMARY KEY,
  balance      REAL NOT NULL,
  pos_amount   REAL NOT NULL DEFAULT 0,
  entry_price  REAL NOT NULL DEFAULT 0,
  stop_price   REAL NOT NULL DEFAULT 0,
  peak_price   REAL NOT NULL DEFAULT 0,
  tp_price     REAL NOT NULL DEFAULT 0,
  opened_ts_ms REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side   TEXT NOT NULL,
  price  REAL NOT NULL,
  amount REAL NOT NULL,
  fee    REAL NOT NULL,
  pnl    REAL,
  note   TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  action        TEXT NOT NULL,
  confidence    REAL,
  stop_loss_pct REAL,
  reason        TEXT,
  source        TEXT,
  executed      INTEGER NOT NULL DEFAULT 0,
  note          TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  cycle     TEXT NOT NULL,
  symbol    TEXT NOT NULL,
  price     REAL NOT NULL,
  balance   REAL NOT NULL,
  pos_value REAL NOT NULL,
  equity    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equity_cycle ON equity(cycle);
CREATE INDEX IF NOT EXISTS idx_equity_symbol ON equity(symbol, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_cycle_id() -> str:
    """轮次 ID 必须唯一(微秒级),否则同秒内的多轮会被聚合成一轮。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Account:
    symbol: str
    balance: float
    pos_amount: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    peak_price: float = 0.0    # 开仓以来的最高价,移动止损用
    tp_price: float = 0.0      # 止盈价(按盈亏比目标算出),0=未设置
    opened_ts_ms: float = 0.0  # 开仓 K 线时间戳(ms),最短持仓判断用

    @property
    def has_position(self) -> bool:
        return self.pos_amount > 0


class Store:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 旧库迁移:accounts 补新增列
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(accounts)")]
        for col in ("peak_price", "tp_price", "opened_ts_ms"):
            if col not in cols:
                self.conn.execute(
                    f"ALTER TABLE accounts ADD COLUMN {col} REAL NOT NULL DEFAULT 0")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- accounts ----
    def ensure_account(self, symbol: str, initial_balance: float) -> Account:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO accounts (symbol, balance) VALUES (?, ?)",
                (symbol, initial_balance),
            )
            self.conn.commit()
            return Account(symbol=symbol, balance=initial_balance)
        return Account(
            symbol=row["symbol"],
            balance=row["balance"],
            pos_amount=row["pos_amount"],
            entry_price=row["entry_price"],
            stop_price=row["stop_price"],
            peak_price=row["peak_price"],
            tp_price=row["tp_price"],
            opened_ts_ms=row["opened_ts_ms"],
        )

    def save_account(self, acc: Account) -> None:
        self.conn.execute(
            "UPDATE accounts SET balance=?, pos_amount=?, entry_price=?, stop_price=?,"
            " peak_price=?, tp_price=?, opened_ts_ms=? WHERE symbol=?",
            (acc.balance, acc.pos_amount, acc.entry_price, acc.stop_price,
             acc.peak_price, acc.tp_price, acc.opened_ts_ms, acc.symbol),
        )
        self.conn.commit()

    # ---- writes ----
    def add_trade(self, symbol: str, side: str, price: float, amount: float,
                  fee: float, pnl: float | None, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO trades (ts, symbol, side, price, amount, fee, pnl, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (utcnow(), symbol, side, price, amount, fee, pnl, note),
        )
        self.conn.commit()

    def add_decision(self, symbol: str, action: str, confidence: float | None,
                     stop_loss_pct: float | None, reason: str, source: str,
                     executed: bool, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO decisions (ts, symbol, action, confidence, stop_loss_pct,"
            " reason, source, executed, note) VALUES (?,?,?,?,?,?,?,?,?)",
            (utcnow(), symbol, action, confidence, stop_loss_pct, reason, source,
             1 if executed else 0, note),
        )
        self.conn.commit()

    def add_equity(self, cycle: str, symbol: str, price: float,
                   balance: float, pos_value: float) -> None:
        self.conn.execute(
            "INSERT INTO equity (ts, cycle, symbol, price, balance, pos_value, equity)"
            " VALUES (?,?,?,?,?,?,?)",
            (utcnow(), cycle, symbol, price, balance, pos_value, balance + pos_value),
        )
        self.conn.commit()

    # ---- reads (dashboard / risk) ----
    def equity_curve(self) -> list[tuple[str, str, float]]:
        """每个决策轮次的 (cycle, ts, 总权益)。"""
        rows = self.conn.execute(
            "SELECT cycle, SUM(equity) AS total, MIN(ts) AS ts FROM equity"
            " GROUP BY cycle ORDER BY MIN(id)"
        ).fetchall()
        return [(r["cycle"], r["ts"], r["total"]) for r in rows]

    def peak_total(self) -> float | None:
        """历史最高的单轮总权益,总回撤开关用。"""
        row = self.conn.execute(
            "SELECT MAX(t) AS peak FROM"
            " (SELECT SUM(equity) AS t FROM equity GROUP BY cycle)"
        ).fetchone()
        return row["peak"]

    def day_start_total(self, day_prefix: str) -> float | None:
        """当日(UTC)第一轮的总权益,用于当日熔断计算。"""
        row = self.conn.execute(
            "SELECT cycle FROM equity WHERE ts LIKE ? ORDER BY id LIMIT 1",
            (day_prefix + "%",),
        ).fetchone()
        if row is None:
            return None
        total = self.conn.execute(
            "SELECT SUM(equity) AS total FROM equity WHERE cycle = ?",
            (row["cycle"],),
        ).fetchone()
        return total["total"]

    def recent(self, table: str, limit: int = 15, symbol: str | None = None) -> list[sqlite3.Row]:
        assert table in ("trades", "decisions")
        if symbol:
            return self.conn.execute(
                f"SELECT * FROM {table} WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return self.conn.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def symbol_stats(self, symbol: str) -> dict:
        """单只虾的已实现盈亏 / 胜率 / 手续费。"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) AS realized,"
            " COALESCE(SUM(fee),0) AS fees,"
            " SUM(CASE WHEN pnl IS NOT NULL THEN 1 ELSE 0 END) AS closed,"
            " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins"
            " FROM trades WHERE symbol=?",
            (symbol,),
        ).fetchone()
        closed = row["closed"] or 0
        return {
            "realized": row["realized"] or 0.0,
            "fees": row["fees"] or 0.0,
            "closed": closed,
            "wins": row["wins"] or 0,
            "win_rate": (row["wins"] or 0) / closed if closed else None,
        }
