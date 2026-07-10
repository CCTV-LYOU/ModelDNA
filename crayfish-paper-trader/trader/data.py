"""行情数据:CCXT 公共接口(无需账号)+ 离线 mock 数据 + 简单技术指标。"""
from __future__ import annotations

import random
import time
import zlib

# candle: [timestamp_ms, open, high, low, close, volume]
Candle = list[float]


def stable_seed(symbol: str) -> int:
    """跨进程稳定的随机种子。

    不要用内置 hash():Python 对字符串哈希默认每个进程随机化
    (PYTHONHASHSEED),会导致"同一条回测命令每次结果都不一样"。
    CRC32 是确定性的,任何机器、任何进程算出来都一样。
    """
    return zlib.crc32(symbol.encode("utf-8")) & 0xFFFF

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


def adx(candles: list[Candle], n: int = 14) -> float | None:
    """Wilder 平均趋向指数:>25 强趋势,<20 震荡。需要至少 2n+1 根 K 线。

    用途:行情状态过滤——趋势策略在横盘里会被反复止损(来回打脸+手续费),
    ADX 低于门槛时不开新的趋势单。
    """
    if len(candles) < 2 * n + 1:
        return None
    trs: list[float] = []
    pdms: list[float] = []
    ndms: list[float] = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        _, _, hi, lo, _, _ = cur
        prev_hi, prev_lo, prev_close = prev[2], prev[3], prev[4]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        up, down = hi - prev_hi, prev_lo - lo
        pdms.append(up if up > down and up > 0 else 0.0)
        ndms.append(down if down > up and down > 0 else 0.0)

    def dx(tr_s: float, pdm_s: float, ndm_s: float) -> float:
        if tr_s <= 0:
            return 0.0
        pdi, ndi = 100 * pdm_s / tr_s, 100 * ndm_s / tr_s
        total = pdi + ndi
        return 100 * abs(pdi - ndi) / total if total else 0.0

    tr_s, pdm_s, ndm_s = sum(trs[:n]), sum(pdms[:n]), sum(ndms[:n])
    dxs = [dx(tr_s, pdm_s, ndm_s)]
    for i in range(n, len(trs)):
        tr_s = tr_s - tr_s / n + trs[i]
        pdm_s = pdm_s - pdm_s / n + pdms[i]
        ndm_s = ndm_s - ndm_s / n + ndms[i]
        dxs.append(dx(tr_s, pdm_s, ndm_s))
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for v in dxs[n:]:
        a = (a * (n - 1) + v) / n
    return a


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
        "adx14": adx(candles, 14),
        "change_24h_pct": (last / first_24h - 1) * 100 if first_24h else 0.0,
    }
