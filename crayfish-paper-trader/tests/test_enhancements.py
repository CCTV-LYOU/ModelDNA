"""回测复现修复 + 四项增强(敞口上限 / ADX 过滤 / ATR 止损仓位 / 止盈降频)的测试。"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader import data
from trader.backtest import backtest_symbol
from trader.bot import trailing_buy_streak
from trader.broker import PaperBroker
from trader.llm import Decision
from trader.risk import RiskManager
from trader.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def broker(store):
    return PaperBroker(store, fee_pct=0.1, slippage_pct=0.05)


# ---- 回测可复现修复 ----

def test_stable_seed_cross_process():
    """种子必须跨进程一致。内置 hash() 每个进程随机化(这正是原 bug),
    子进程强制 PYTHONHASHSEED=random 后,CRC32 种子仍应与本进程完全一致。"""
    import os
    env = {**os.environ, "PYTHONHASHSEED": "random"}
    out = subprocess.run(
        [sys.executable, "-c",
         "from trader.data import stable_seed; print(stable_seed('BTC/USDT'))"],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == data.stable_seed("BTC/USDT")
    # 不同交易对应有不同种子(否则三只虾走出同一条价格路径)
    assert data.stable_seed("BTC/USDT") != data.stable_seed("ETH/USDT")


def test_backtest_seed_path_reproducible():
    """与 backtest.main --mock 相同的种子路径,连跑两次结果一致。"""
    cfg = {"initial_balance": 10000.0, "fee_pct": 0.1, "slippage_pct": 0.05,
           "candles": 48, "risk": {}}
    runs = []
    for _ in range(2):
        candles = data.mock_ohlcv("ETH/USDT", 800, seed=data.stable_seed("ETH/USDT"))
        runs.append(backtest_symbol("ETH/USDT", candles, cfg))
    assert runs[0]["final"] == pytest.approx(runs[1]["final"])
    assert runs[0]["trades"] == runs[1]["trades"]
    assert runs[0]["buy_hold_pct"] == pytest.approx(runs[1]["buy_hold_pct"])


# ---- ADX 行情过滤 ----

def _candle(ts, o, hi, lo, c):
    return [ts, o, hi, lo, c, 1000.0]


def test_adx_trend_high_chop_low():
    trend = []
    p = 100.0
    for i in range(60):
        nxt = p * 1.01
        trend.append(_candle(i, p, nxt * 1.001, p * 0.999, nxt))
        p = nxt
    chop = []
    for i in range(60):
        up = i % 2 == 0
        o = 100.0 if up else 101.0
        c = 101.0 if up else 100.0
        chop.append(_candle(i, o, 101.2, 99.8, c))
    adx_trend = data.adx(trend)
    adx_chop = data.adx(chop)
    assert adx_trend is not None and adx_chop is not None
    assert adx_trend > 25          # 单边趋势:强
    assert adx_chop < adx_trend    # 震荡:显著更弱
    assert adx_chop < 20
    assert data.adx(trend[:20]) is None  # 数据不足


def test_regime_gate(store):
    risk = RiskManager(store, {"adx_min_to_open": 18})
    assert not risk.regime_ok(None)     # 数据不足,保守不开仓
    assert not risk.regime_ok(10.0)
    assert risk.regime_ok(25.0)
    off = RiskManager(store, {"adx_min_to_open": 0})
    assert off.regime_ok(None) and off.regime_ok(1.0)  # 0=关闭


# ---- ATR 波动率止损 + 风险预算仓位 ----

def test_atr_stop_and_risk_budget_sizing(store):
    risk = RiskManager(store, {"max_position_pct": 20, "default_stop_loss_pct": 3,
                               "max_stop_loss_pct": 10, "atr_stop_mult": 2,
                               "risk_per_trade_pct": 1})
    assert risk.stop_loss_pct_for(100.0, 2.0, None) == pytest.approx(4.0)   # 2×ATR
    assert risk.stop_loss_pct_for(100.0, 20.0, None) == 10.0                # 钳到上限
    assert risk.stop_loss_pct_for(100.0, None, 5.0) == 5.0                  # 无ATR用AI值
    # 仓位:止损越宽仓位越小(单笔最多亏 1% = 100 USDT)
    assert risk.position_size(10000, 1.0, 4.0) == pytest.approx(2000.0)     # 上限约束
    assert risk.position_size(10000, 1.0, 8.0) == pytest.approx(1250.0)     # 风险预算约束
    assert risk.position_size(10000, 1.0) == pytest.approx(2000.0)          # 兼容旧调用
    off = RiskManager(store, {"atr_stop_mult": 0, "risk_per_trade_pct": 0,
                              "max_position_pct": 20, "default_stop_loss_pct": 3,
                              "max_stop_loss_pct": 10})
    assert off.stop_loss_pct_for(100.0, 2.0, 5.0) == 5.0                    # 关闭则AI优先
    assert off.position_size(10000, 1.0, 8.0) == pytest.approx(2000.0)


# ---- 组合敞口上限(相关性防护)----

def test_exposure_headroom(store):
    risk = RiskManager(store, {"max_total_exposure_pct": 30})
    assert risk.exposure_headroom(30000, 6000) == pytest.approx(3000.0)
    assert risk.exposure_headroom(30000, 9000) == 0.0
    assert risk.exposure_headroom(30000, 12000) == 0.0   # 超限不为负
    off = RiskManager(store, {"max_total_exposure_pct": 0})
    assert off.exposure_headroom(30000, 29999) == float("inf")


# ---- 盈亏比止盈 ----

def test_take_profit_rr(store, broker):
    acc = store.ensure_account("BTC/USDT", 10000.0)
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=4.0,
                     take_profit_rr=2.0)
    exec_price = 100.0 * 1.0005
    assert acc.tp_price == pytest.approx(exec_price * 1.08)  # 2×4% = 8%
    assert broker.check_take_profit(acc, price=107.0) is None
    pnl = broker.check_take_profit(acc, price=108.2)
    assert pnl is not None and pnl > 0
    assert not acc.has_position and acc.tp_price == 0.0
    # rr=0 不设止盈
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=4.0,
                     take_profit_rr=0.0)
    assert acc.tp_price == 0.0
    assert broker.check_take_profit(acc, price=99999.0) is None


# ---- 最短持仓(降频)----

def test_min_hold(store, broker):
    risk = RiskManager(store, {"min_hold_hours": 4})
    acc = store.ensure_account("SOL/USDT", 10000.0)
    t0 = 1_700_000_000_000.0
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=3.0,
                     ts_ms=t0)
    assert acc.opened_ts_ms == t0
    assert not risk.min_hold_ok(acc, t0 + 2 * 3_600_000)   # 2h < 4h,禁止主动离场
    assert risk.min_hold_ok(acc, t0 + 4 * 3_600_000)       # 满 4h
    broker.close_long(acc, 100.0)
    assert risk.min_hold_ok(acc, t0)                        # 空仓不受限
    off = RiskManager(store, {"min_hold_hours": 0})
    assert off.min_hold_ok(acc, t0)                         # 0=关闭


# ---- BUY 二次确认(降频)----

def test_trailing_buy_streak_counts(store):
    assert trailing_buy_streak(store, "BTC/USDT") == 0
    store.add_decision("BTC/USDT", "BUY", 0.7, 3, "r", "mock", False)
    store.add_decision("BTC/USDT", "HOLD", 0.0, None, "r", "mock", False)
    store.add_decision("BTC/USDT", "BUY", 0.7, 3, "r", "mock", False)
    store.add_decision("BTC/USDT", "BUY", 0.7, 3, "r", "mock", False)
    assert trailing_buy_streak(store, "BTC/USDT") == 2      # HOLD 中断连击
    store.add_decision("ETH/USDT", "BUY", 0.7, 3, "r", "mock", False)
    assert trailing_buy_streak(store, "BTC/USDT") == 2      # 不串号


def test_buy_confirmation_delays_entry_in_backtest():
    always_buy = lambda ind, acc: Decision("BUY", 0.9, 3.0, "always", "test")  # noqa: E731
    base_risk = {"adx_min_to_open": 0, "atr_stop_mult": 0, "risk_per_trade_pct": 0,
                 "min_hold_hours": 0, "take_profit_rr": 0, "trailing_stop_pct": 0,
                 "max_position_pct": 20, "default_stop_loss_pct": 3,
                 "max_stop_loss_pct": 10, "min_confidence": 0.6}
    candles = data.mock_ohlcv("BTC/USDT", 120, seed=7)
    cfg1 = {"initial_balance": 10000.0, "fee_pct": 0.1, "slippage_pct": 0.05,
            "candles": 48, "risk": {**base_risk, "buy_confirm_bars": 1}}
    cfg2 = {"initial_balance": 10000.0, "fee_pct": 0.1, "slippage_pct": 0.05,
            "candles": 48, "risk": {**base_risk, "buy_confirm_bars": 2}}
    r1 = backtest_symbol("BTC/USDT", list(candles), cfg1, strategy=always_buy)
    r2 = backtest_symbol("BTC/USDT", list(candles), cfg2, strategy=always_buy)
    initial = 10000.0
    assert r1["equity_series"][0] < initial   # 确认=1:首根信号即开仓(扣了手续费)
    assert r2["equity_series"][0] == pytest.approx(initial)  # 确认=2:首根只等待
    assert r2["equity_series"][1] < initial   # 第二根确认后才开仓
