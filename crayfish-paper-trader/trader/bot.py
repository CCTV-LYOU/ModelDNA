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
from .risk import MIN_ORDER_QUOTE, RiskManager
from .store import Store, new_cycle_id

log = logging.getLogger("crayfish.bot")


def trailing_buy_streak(store: Store, symbol: str, limit: int = 10) -> int:
    """该交易对最近连续多少轮决策是 BUY(用于二次确认,遇到非 BUY 即中断)。"""
    n = 0
    for row in store.recent("decisions", limit, symbol):
        if row["action"] == "BUY":
            n += 1
        else:
            break
    return n


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
    partial = len(market) < len(symbols)
    if partial and market:
        halted_reason = "partial data: totals not comparable, no new entries"
        log.warning("market data incomplete (%d/%d): skip drawdown checks this cycle",
                    len(market), len(symbols))
    elif market:
        if risk.kill_switch_tripped(total):
            halted_reason = "总回撤开关触发"
        elif risk.circuit_breaker_tripped(total):
            halted_reason = "当日熔断中"

    def portfolio_exposure() -> tuple[float, float]:
        """(总权益, 持仓市值合计)。BTC/ETH/SOL 高度相关,持仓合并算一个敞口。"""
        equity_sum = exposure_sum = 0.0
        for s, (_, m_ind) in market.items():
            a = store.ensure_account(s, cfg["initial_balance"])
            exposure_sum += a.pos_amount * m_ind["last_price"]
            equity_sum += a.balance + a.pos_amount * m_ind["last_price"]
        return equity_sum, exposure_sum

    for symbol, (candles, ind) in market.items():
        acc = store.ensure_account(symbol, cfg["initial_balance"])
        price = ind["last_price"]
        now_ts = candles[-1][0]

        # 移动止损上调、止损/止盈检查,永远先于 AI 决策
        broker.apply_trailing_stop(acc, price, risk.trailing_stop_pct)
        safety_pnl = broker.check_stop_loss(acc, price)
        if safety_pnl is None:
            safety_pnl = broker.check_take_profit(acc, price)

        if mock_llm:
            decision = llm.mock_decision(ind, acc)
        else:
            prompt = llm.build_prompt(symbol, candles, ind, acc)
            decision = llm.ask(cfg["llm"], prompt)

        executed, note = False, ""
        if safety_pnl is not None and decision.action == "SELL":
            note = "止损/止盈已先行平仓"
        elif decision.action == "BUY":
            adx_v = ind.get("adx14")
            streak = 1 + trailing_buy_streak(store, symbol)
            if acc.has_position:
                note = "已持仓,忽略加仓建议"
            elif halted_reason:
                note = f"{halted_reason},禁止开新仓"
            elif not risk.confidence_ok(decision.confidence):
                note = f"置信度 {decision.confidence:.2f} 低于门槛"
            elif not risk.regime_ok(adx_v):
                note = (f"ADX={adx_v:.1f} < {risk.adx_min_to_open:g},震荡市不开新仓"
                        if adx_v is not None else "ADX 数据不足,不开新仓")
            elif streak < max(1, risk.buy_confirm_bars):
                note = f"BUY 信号 {streak}/{risk.buy_confirm_bars} 轮,待下轮确认"
            else:
                stop_pct = risk.stop_loss_pct_for(price, ind.get("atr14"),
                                                  decision.stop_loss_pct)
                quote = risk.position_size(acc.balance, decision.confidence, stop_pct)
                total_now, exposure_now = portfolio_exposure()
                headroom = risk.exposure_headroom(total_now, exposure_now)
                if quote > headroom:
                    quote = headroom if headroom >= MIN_ORDER_QUOTE else 0.0
                if quote <= 0:
                    note = (f"组合敞口 {exposure_now:.0f} 已达总权益的 "
                            f"{risk.max_total_exposure_pct:g}% 上限或余额不足,跳过")
                else:
                    executed = broker.open_long(acc, price, quote, stop_pct,
                                                note=decision.reason, ts_ms=now_ts,
                                                take_profit_rr=risk.take_profit_rr)
                    if not executed:
                        note = "余额不足"
        elif decision.action == "SELL":
            if not acc.has_position:
                note = "空仓,SELL 无操作"
            elif not risk.min_hold_ok(acc, now_ts):
                note = (f"最短持仓 {risk.min_hold_hours:g}h 未到,忽略主动离场"
                        "(止损/止盈仍生效)")
            else:
                broker.close_long(acc, price, note=decision.reason)
                executed = True

        store.add_decision(symbol, decision.action, decision.confidence,
                           decision.stop_loss_pct, decision.reason,
                           decision.source, executed, note)
        acc = store.ensure_account(symbol, cfg["initial_balance"])
        if not partial:
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
