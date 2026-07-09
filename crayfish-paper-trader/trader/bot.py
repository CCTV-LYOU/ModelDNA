"""主循环:行情 → AI 决策 → 风控 → 纸面下单 → 落库。

用法:
  python -m trader.bot --mock --once   # 离线跑一轮(mock 行情 + mock 决策)
  python -m trader.bot --mock-llm      # 真行情 + 规则决策(不消耗订阅额度)
  python -m trader.bot                 # 真行情 + Claude 决策(需已登录 claude CLI)
"""
from __future__ import annotations

import argparse
import logging
import time

import yaml

from . import data, llm
from .broker import PaperBroker
from .risk import RiskManager
from .store import Store, new_cycle_id

log = logging.getLogger("crayfish.bot")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_candles(cfg: dict, symbol: str, mock: bool) -> list[data.Candle]:
    if mock:
        return data.mock_ohlcv(symbol, cfg["candles"])
    return data.fetch_ohlcv(cfg["exchange"], symbol, cfg["timeframe"], cfg["candles"])


def run_cycle(cfg: dict, store: Store, broker: PaperBroker, risk: RiskManager,
              mock_data: bool, mock_llm: bool) -> None:
    cycle = new_cycle_id()
    symbols = cfg["symbols"]

    # 先取一遍行情,算总权益,判断当日熔断
    market: dict[str, tuple[list[data.Candle], dict]] = {}
    total = 0.0
    for symbol in symbols:
        try:
            candles = get_candles(cfg, symbol, mock_data)
        except Exception as e:  # 网络抖动等,跳过该虾本轮
            log.warning("[%s] 拉取行情失败:%s", symbol, e)
            continue
        ind = data.summarize(candles)
        market[symbol] = (candles, ind)
        acc = store.ensure_account(symbol, cfg["initial_balance"])
        total += broker.equity(acc, ind["last_price"])
    halted_reason = ""
    if market:
        if risk.kill_switch_tripped(total):
            halted_reason = "总回撤开关触发"
        elif risk.circuit_breaker_tripped(total):
            halted_reason = "当日熔断中"

    for symbol, (candles, ind) in market.items():
        acc = store.ensure_account(symbol, cfg["initial_balance"])
        price = ind["last_price"]

        # 移动止损上调、止损检查,永远先于 AI 决策
        broker.apply_trailing_stop(acc, price, risk.trailing_stop_pct)
        stop_pnl = broker.check_stop_loss(acc, price)

        if mock_llm:
            decision = llm.mock_decision(ind, acc)
        else:
            prompt = llm.build_prompt(symbol, candles, ind, acc)
            decision = llm.ask(cfg["llm"], prompt)

        executed, note = False, ""
        if stop_pnl is not None and decision.action == "SELL":
            note = "止损已先行平仓"
        elif decision.action == "BUY":
            if acc.has_position:
                note = "已持仓,忽略加仓建议"
            elif halted_reason:
                note = f"{halted_reason},禁止开新仓"
            elif not risk.confidence_ok(decision.confidence):
                note = f"置信度 {decision.confidence:.2f} 低于门槛"
            else:
                quote = risk.position_size(acc.balance, decision.confidence)
                if quote <= 0:
                    note = "余额不足最小下单额"
                else:
                    stop = risk.clamp_stop_loss(decision.stop_loss_pct)
                    executed = broker.open_long(acc, price, quote, stop,
                                                note=decision.reason)
                    if not executed:
                        note = "余额不足"
        elif decision.action == "SELL":
            if acc.has_position:
                broker.close_long(acc, price, note=decision.reason)
                executed = True
            else:
                note = "空仓,SELL 无操作"

        store.add_decision(symbol, decision.action, decision.confidence,
                           decision.stop_loss_pct, decision.reason,
                           decision.source, executed, note)
        acc = store.ensure_account(symbol, cfg["initial_balance"])
        store.add_equity(cycle, symbol, price, acc.balance, acc.pos_amount * price)
        log.info("[%s] %s(%.2f)%s | 权益 %.2f | %s", symbol, decision.action,
                 decision.confidence, " ✔" if executed else "",
                 broker.equity(acc, price), note or decision.reason)


def main() -> None:
    parser = argparse.ArgumentParser(description="小龙虾模拟盘(纸面交易,不动真钱)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mock", action="store_true",
                        help="mock 行情 + mock 决策,完全离线验证")
    parser.add_argument("--mock-llm", action="store_true",
                        help="真行情,但用内置规则代替 AI(不消耗订阅额度)")
    parser.add_argument("--mock-data", action="store_true",
                        help="mock 行情 + 真实 AI 决策,用于验证 codex/claude 接线")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    store = Store(cfg["db_path"])
    broker = PaperBroker(store, cfg["fee_pct"], cfg["slippage_pct"])
    risk = RiskManager(store, cfg.get("risk", {}))
    mock_llm = args.mock or args.mock_llm
    mock_data = args.mock or args.mock_data

    log.info("🦞 小龙虾模拟盘启动:%s | 数据=%s 决策=%s | 每 %s 分钟一轮",
             ", ".join(cfg["symbols"]),
             "mock" if mock_data else cfg["exchange"],
             "mock" if mock_llm else cfg["llm"].get("provider", "codex"),
             cfg["interval_minutes"])
    while True:
        try:
            run_cycle(cfg, store, broker, risk, mock_data, mock_llm)
        except Exception:
            log.exception("本轮异常,%s 分钟后重试", cfg["interval_minutes"])
        if args.once:
            break
        time.sleep(cfg["interval_minutes"] * 60)


if __name__ == "__main__":
    main()
