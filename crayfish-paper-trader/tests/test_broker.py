"""确定性逻辑(撮合 / 风控 / JSON 解析)的单元测试。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.broker import PaperBroker
from trader.llm import Decision, extract_json, mock_decision, normalize
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


def test_open_close_pnl_math(store, broker):
    acc = store.ensure_account("BTC/USDT", 10000.0)
    assert broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=3.0)
    exec_buy = 100.0 * 1.0005
    assert acc.balance == pytest.approx(10000.0 - 1000.0 - 1.0)  # 本金+0.1%费
    assert acc.pos_amount == pytest.approx(1000.0 / exec_buy)
    assert acc.stop_price == pytest.approx(exec_buy * 0.97)

    pnl = broker.close_long(acc, price=110.0)
    exec_sell = 110.0 * 0.9995
    expected_pnl = (exec_sell - exec_buy) * (1000.0 / exec_buy) - exec_sell * (1000.0 / exec_buy) * 0.001
    assert pnl == pytest.approx(expected_pnl)
    assert not acc.has_position
    # 平仓后余额 = 开仓后余额 + 卖出净得
    assert acc.balance == pytest.approx(8999.0 + (1000.0 / exec_buy) * exec_sell * 0.999)


def test_insufficient_balance(store, broker):
    acc = store.ensure_account("ETH/USDT", 100.0)
    assert not broker.open_long(acc, price=100.0, quote_amount=200.0, stop_loss_pct=3.0)
    assert acc.balance == 100.0 and not acc.has_position


def test_stop_loss_triggers(store, broker):
    acc = store.ensure_account("SOL/USDT", 10000.0)
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=3.0)
    assert broker.check_stop_loss(acc, price=99.0) is None      # 未触及
    pnl = broker.check_stop_loss(acc, price=97.0)               # 触及止损
    assert pnl is not None and pnl < 0
    assert not acc.has_position


def test_account_persists_across_reload(store, broker, tmp_path):
    acc = store.ensure_account("BTC/USDT", 10000.0)
    broker.open_long(acc, price=100.0, quote_amount=1000.0, stop_loss_pct=3.0)
    again = store.ensure_account("BTC/USDT", 10000.0)
    assert again.has_position and again.balance == pytest.approx(acc.balance)


def test_risk_clamps(store):
    risk = RiskManager(store, {"max_position_pct": 20, "default_stop_loss_pct": 3,
                               "max_stop_loss_pct": 10, "min_confidence": 0.6,
                               "daily_loss_limit_pct": 5})
    assert risk.position_size(10000, 0.8) == pytest.approx(1600.0)
    assert risk.position_size(10, 0.8) == 0.0          # 低于最小下单额
    assert risk.clamp_stop_loss(None) == 3
    assert risk.clamp_stop_loss(50) == 10              # 压到上限
    assert risk.clamp_stop_loss(0.1) == 0.5
    assert not risk.confidence_ok(0.5) and risk.confidence_ok(0.6)


def test_circuit_breaker(store):
    risk = RiskManager(store, {"daily_loss_limit_pct": 5})
    from trader.store import utcnow
    day_cycle = utcnow()
    store.add_equity(day_cycle, "BTC/USDT", 100, 10000, 0)
    store.add_equity(day_cycle, "ETH/USDT", 100, 10000, 0)
    assert not risk.circuit_breaker_tripped(19500)     # -2.5%,未触发
    assert risk.circuit_breaker_tripped(18000)         # -10%,熔断


def test_extract_json_tolerates_noise():
    assert extract_json('前置废话 {"action": "BUY", "confidence": 0.7} 后置') == {
        "action": "BUY", "confidence": 0.7}
    assert extract_json("完全没有 JSON") is None
    nested = extract_json('{"a": {"b": 1}, "action": "HOLD"}')
    assert nested["a"]["b"] == 1


def test_normalize_bad_output_defaults_to_hold():
    d = normalize(None, "claude")
    assert d.action == "HOLD" and d.source == "fallback"
    d = normalize({"action": "YOLO全仓梭哈", "confidence": 99}, "claude")
    assert d.action == "HOLD" and d.confidence == 1.0
    d = normalize({"action": "buy", "confidence": "0.7", "stop_loss_pct": "3"}, "claude")
    assert d.action == "BUY" and d.confidence == 0.7 and d.stop_loss_pct == 3.0


def test_mock_decision_shapes(store):
    acc = store.ensure_account("BTC/USDT", 10000.0)
    ind = {"sma_fast": 110, "sma_slow": 100, "rsi14": 50, "last_price": 110,
           "atr14": 1, "change_24h_pct": 1}
    d = mock_decision(ind, acc)
    assert isinstance(d, Decision) and d.action == "BUY"
    acc.pos_amount = 1.0
    d = mock_decision({**ind, "rsi14": 80}, acc)
    assert d.action == "SELL"
