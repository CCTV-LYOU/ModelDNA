"""行情数据:CCXT 公共接口(无需账号)+ 离线 mock 数据 + 简单技术指标。"""
from __future__ import annotations

import random
import time

# candle: [timestamp_ms, open, high, low, close, volume]
Candle = list[float]

_MOCK_BASE = {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0, "SOL/USDT": 150.0}
_mock_last: dict[str, float] = {}


def fetch_ohlcv(exchange_id: str, symbol: str, timeframe: str, limit: int) -> list[Candle]:
    import ccxt  # 延迟导入,mock 模式不需要装网络环境

    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def mock_ohlcv(symbol: str, limit: int, seed: int | None = None) -> list[Candle]:
    """随机游走的合成 K 线,用于离线验证全链路。

    带 seed 时结果完全可复现(从基准价开始,不接续上次价格),回测自检用;
    不带 seed 时接续上次收盘价,模拟实时行情的连续性。
    """
    deterministic = seed is not None
    rng = random.Random(seed if deterministic else time.time_ns())
    price = (_MOCK_BASE.get(symbol, 100.0) if deterministic
             else _mock_last.get(symbol, _MOCK_BASE.get(symbol, 100.0)))
    now_ms = int(time.time() * 1000)
    candles: list[Candle] = []
    for i in range(limit):
        drift = rng.gauss(0, 0.008)
        o = price
        c = max(o * (1 + drift), 0.01)
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.003)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.003)))
        vol = abs(rng.gauss(1000, 300))
        candles.append([now_ms - (limit - i) * 3600_000, o, hi, lo, c, vol])
        price = c
    if not deterministic:
        _mock_last[symbol] = price
    return candles


# ---- 指标(纯 Python,避免引入 numpy/pandas)----

def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for prev, cur in zip(closes[-n - 1:-1], closes[-n:]):
        diff = cur - prev
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def atr(candles: list[Candle], n: int = 14) -> float | None:
    if len(candles) < n + 1:
        return None
    trs = []
    for prev, cur in zip(candles[-n - 1:-1], candles[-n:]):
        _, _, hi, lo, _, _ = cur
        prev_close = prev[4]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    return sum(trs) / n


def summarize(candles: list[Candle]) -> dict:
    closes = [c[4] for c in candles]
    last = closes[-1]
    first_24h = closes[-24] if len(closes) >= 24 else closes[0]
    return {
        "last_price": last,
        "sma_fast": sma(closes, 10),
        "sma_slow": sma(closes, 30),
        "rsi14": rsi(closes, 14),
        "atr14": atr(candles, 14),
        "change_24h_pct": (last / first_24h - 1) * 100 if first_24h else 0.0,
    }
