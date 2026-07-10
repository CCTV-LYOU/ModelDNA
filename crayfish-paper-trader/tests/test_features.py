"""四项增强的测试:移动止损、总回撤开关、回测引擎、codex 输出解析。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader import data
from trader.backtest import backtest_symbol, max_drawdown
from trader.broker import PaperBroker
from trader.llm import extract_last_json, normalize
from trader.risk import RiskManager
from trader.store import Store, new_cycle_id


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def broker(store):
    return PaperBroker(store, fee_pct=0.1, slippage_pct=0.05)


def test_trailing_stop_rises_and_triggers(store, broker):
    acc = store.ensure_account("BTC/USDT", 10000.0)
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=3.0)
    initial_stop = acc.stop_price

    broker.apply_trailing_stop(acc, price=120.0, trailing_pct=4.0)
    assert acc.peak_price == 120.0
    assert acc.stop_price == pytest.approx(120.0 * 0.96)   # 已高于开仓止损
    assert acc.stop_price > initial_stop

    broker.apply_trailing_stop(acc, price=110.0, trailing_pct=4.0)
    assert acc.stop_price == pytest.approx(120.0 * 0.96)   # 只升不降

    pnl = broker.check_stop_loss(acc, price=114.0)          # 114 < 115.2 触发
    assert pnl is not None and pnl > 0                      # 移动止损锁住了利润
    assert not acc.has_position


def test_trailing_stop_disabled_and_flat(store, broker):
    acc = store.ensure_account("ETH/USDT", 10000.0)
    broker.apply_trailing_stop(acc, price=100.0, trailing_pct=4.0)  # 空仓:无操作
    assert acc.stop_price == 0.0
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=3.0)
    stop = acc.stop_price
    broker.apply_trailing_stop(acc, price=200.0, trailing_pct=0)    # 关闭:无操作
    assert acc.stop_price == stop


def test_total_drawdown_kill_switch(store):
    risk = RiskManager(store, {"total_drawdown_limit_pct": 20})
    c1, c2 = new_cycle_id(), new_cycle_id()
    store.add_equity(c1, "BTC/USDT", 100, 15000, 0)
    store.add_equity(c1, "ETH/USDT", 100, 15000, 0)   # 峰值 30000
    store.add_equity(c2, "BTC/USDT", 100, 14000, 0)
    store.add_equity(c2, "ETH/USDT", 100, 14000, 0)
    assert not risk.kill_switch_tripped(28000)   # -6.7%,未触发
    assert risk.kill_switch_tripped(23000)       # -23.3%,触发
    off = RiskManager(store, {"total_drawdown_limit_pct": 0})
    assert not off.kill_switch_tripped(1)        # 0 = 关闭


def test_backtest_runs_and_reports(tmp_path):
    cfg = {
        "initial_balance": 10000.0, "fee_pct": 0.1, "slippage_pct": 0.05,
        "candles": 48,
        "risk": {"max_position_pct": 20, "default_stop_loss_pct": 3,
                 "max_stop_loss_pct": 10, "min_confidence": 0.6,
                 "trailing_stop_pct": 4.0},
    }
    candles = data.mock_ohlcv("BTC/USDT", 500, seed=42)
    r = backtest_symbol("BTC/USDT", candles, cfg)
    assert r["candles"] == 500
    assert r["final"] > 0
    assert len(r["equity_series"]) == 500 - 31
    assert 0 <= r["max_drawdown_pct"] < 100
    assert r["trades"] >= 0
    # 同样的种子,结果可复现
    r2 = backtest_symbol("BTC/USDT", data.mock_ohlcv("BTC/USDT", 500, seed=42), cfg)
    assert r2["final"] == pytest.approx(r["final"])


def test_max_drawdown_math():
    assert max_drawdown([100, 120, 90, 110]) == pytest.approx(25.0)
    assert max_drawdown([100, 110, 120]) == 0.0
    assert max_drawdown([]) == 0.0


def test_extract_last_json_for_codex_output():
    noisy = ('[2026-07-09] workdir: /tmp {"event":"log"}\n'
             '思考中...\n最终答案:\n'
             '{"action": "BUY", "confidence": 0.65, "stop_loss_pct": 3, "reason": "test"}')
    got = extract_last_json(noisy)
    assert got["action"] == "BUY" and got["confidence"] == 0.65
    assert extract_last_json("没有 JSON") is None
    d = normalize(got, "codex")
    assert d.action == "BUY" and d.source == "codex"
