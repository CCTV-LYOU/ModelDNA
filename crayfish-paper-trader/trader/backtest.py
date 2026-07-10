"""历史回测:用与实时模拟完全相同的撮合/风控逻辑回放历史 K 线。

用法:
  python -m trader.backtest --days 365          # 真实历史(ccxt 公共接口分页拉取)
  python -m trader.backtest --days 365 --mock   # 合成数据,离线自检

注意:回测跑的是内置规则策略(与 --mock-llm 相同的动量规则),不是 Claude——
对上万根 K 线逐根调用 LLM 既不现实也会耗尽订阅额度,LLM 策略只能向前模拟验证。
回测的意义:
  1. 用几分钟看到策略在牛市/熊市/震荡市的表现,不用等几个月;
  2. 和"买入持有"基准硬碰硬对比——跑不赢基准的策略没有存在价值;
  3. 验证撮合、止损/止盈、移动止损、ADX 过滤、二次确认这套管线在长序列上的行为。

局限:回测按单交易对隔离账户,组合级敞口上限(max_total_exposure_pct)
只在实时模拟里生效;--mock 的合成数据用 stable_seed 固定种子,结果完全可复现,
但合成数据的收益数字是噪声,只用于验管线,不代表任何真实表现。
"""
from __future__ import annotations

import argparse
import logging
import time

from . import data, llm
from .bot import load_config
from .broker import PaperBroker
from .risk import RiskManager
from .store import Store

log = logging.getLogger("crayfish.backtest")

WARMUP = 31  # 指标需要的最少 K 线数


def fetch_history(exchange_id: str, symbol: str, timeframe: str,
                  since_ms: int) -> list[data.Candle]:
    import ccxt

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    out: list[data.Candle] = []
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        since_ms = batch[-1][0] + 1
    return out


def max_drawdown(series: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    return mdd


def backtest_symbol(symbol: str, candles: list[data.Candle], cfg: dict,
                    strategy=llm.mock_decision) -> dict:
    """单交易对回测,撮合逻辑与实时 bot 完全一致。返回统计 + 权益序列。"""
    store = Store(":memory:")
    try:
        broker = PaperBroker(store, cfg["fee_pct"], cfg["slippage_pct"])
        risk = RiskManager(store, cfg.get("risk", {}))
        acc = store.ensure_account(symbol, cfg["initial_balance"])
        window_n = cfg["candles"]
        equity_series: list[float] = []

        buy_streak = 0
        for i in range(WARMUP, len(candles)):
            window = candles[max(0, i - window_n + 1): i + 1]
            ind = data.summarize(window)
            ts, high, low, price = (candles[i][0], candles[i][2],
                                    candles[i][3], candles[i][4])

            broker.apply_trailing_stop(acc, price, risk.trailing_stop_pct)
            # 回测里用当根 K 线的最低/最高价判断止损/止盈,按触发价成交;
            # 同一根 K 线同时触及两者时按止损算(悲观假设)
            if acc.has_position and low <= acc.stop_price:
                broker.close_long(acc, acc.stop_price, note="止损触发")
            elif acc.has_position and acc.tp_price > 0 and high >= acc.tp_price:
                broker.close_long(acc, acc.tp_price, note="止盈触发(盈亏比达标)")

            d = strategy(ind, acc)
            buy_streak = buy_streak + 1 if d.action == "BUY" else 0
            if d.action == "BUY" and not acc.has_position and risk.confidence_ok(d.confidence):
                if (risk.regime_ok(ind.get("adx14"))
                        and buy_streak >= max(1, risk.buy_confirm_bars)):
                    stop_pct = risk.stop_loss_pct_for(price, ind.get("atr14"),
                                                      d.stop_loss_pct)
                    quote = risk.position_size(acc.balance, d.confidence, stop_pct)
                    if quote > 0:
                        broker.open_long(acc, price, quote, stop_pct, d.reason,
                                         ts_ms=ts, take_profit_rr=risk.take_profit_rr)
            elif d.action == "SELL" and acc.has_position and risk.min_hold_ok(acc, ts):
                broker.close_long(acc, price, d.reason)

            equity_series.append(broker.equity(acc, price))

        stats = store.symbol_stats(symbol)
        pnls = [r["pnl"] for r in store.conn.execute(
            "SELECT pnl FROM trades WHERE pnl IS NOT NULL")]
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        initial = cfg["initial_balance"]
        final = equity_series[-1] if equity_series else initial
        start_price, end_price = candles[WARMUP][4], candles[-1][4]
        return {
            "symbol": symbol,
            "candles": len(candles),
            "initial": initial,
            "final": final,
            "return_pct": (final / initial - 1) * 100,
            "buy_hold_pct": (end_price / start_price - 1) * 100,
            "max_drawdown_pct": max_drawdown(equity_series),
            "trades": stats["closed"],
            "win_rate": stats["win_rate"],
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "fees": stats["fees"],
            "equity_series": equity_series,
        }
    finally:
        store.close()


def run_backtest(cfg: dict, candles_by_symbol: dict[str, list[data.Candle]],
                 strategy=llm.mock_decision) -> list[dict]:
    return [backtest_symbol(s, c, cfg, strategy)
            for s, c in candles_by_symbol.items() if len(c) > WARMUP + 1]


def print_report(results: list[dict]) -> None:
    fmt_pct = lambda v: f"{v:+8.2f}%" if v is not None else "       —"  # noqa: E731
    print("\n=== 回测报告(规则策略基线,非 Claude)===")
    print(f"{'交易对':<10} {'K线数':>6} {'策略收益':>9} {'买入持有':>9} "
          f"{'最大回撤':>9} {'笔数':>4} {'胜率':>5} {'盈亏比':>6} {'手续费':>8}")
    for r in results:
        wr = f"{r['win_rate'] * 100:3.0f}%" if r["win_rate"] is not None else "  —"
        pf = f"{r['profit_factor']:5.2f}" if r["profit_factor"] is not None else "    —"
        print(f"{r['symbol']:<10} {r['candles']:>6} {fmt_pct(r['return_pct'])} "
              f"{fmt_pct(r['buy_hold_pct'])} {r['max_drawdown_pct']:>8.2f}% "
              f"{r['trades']:>4} {wr:>5} {pf:>6} {r['fees']:>8.2f}")
    if len(results) > 1:
        # 总计:各权益序列从尾部对齐后相加
        n = min(len(r["equity_series"]) for r in results)
        total_series = [sum(r["equity_series"][len(r["equity_series"]) - n + i]
                            for r in results) for i in range(n)]
        initial = sum(r["initial"] for r in results)
        final = total_series[-1]
        bh = sum(r["initial"] * (1 + r["buy_hold_pct"] / 100) for r in results)
        print("-" * 84)
        print(f"{'合计':<10} {'':>6} {fmt_pct((final / initial - 1) * 100)} "
              f"{fmt_pct((bh / initial - 1) * 100)} {max_drawdown(total_series):>8.2f}% "
              f"{sum(r['trades'] for r in results):>4}")
    print("\n提示:策略收益跑不赢『买入持有』就说明该策略没有价值;回测盈利也不代表未来盈利。")


def main() -> None:
    parser = argparse.ArgumentParser(description="小龙虾回测(纸面,不动真钱)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days", type=int, default=365, help="回看多少天(默认365)")
    parser.add_argument("--mock", action="store_true", help="合成数据离线自检")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    hours_per_candle = {"1h": 1, "4h": 4, "1d": 24}.get(cfg["timeframe"], 1)
    n = args.days * 24 // hours_per_candle

    candles_by_symbol: dict[str, list[data.Candle]] = {}
    for symbol in cfg["symbols"]:
        if args.mock:
            # 用 stable_seed(CRC32)而不是内置 hash():后者每个进程都随机化,
            # 会让"同一条回测命令每次结果都不一样",无法比较改动前后的差异
            candles_by_symbol[symbol] = data.mock_ohlcv(symbol, n, seed=data.stable_seed(symbol))
        else:
            since = int((time.time() - args.days * 86400) * 1000)
            print(f"拉取 {symbol} 最近 {args.days} 天 {cfg['timeframe']} K 线 ...")
            candles_by_symbol[symbol] = fetch_history(
                cfg["exchange"], symbol, cfg["timeframe"], since)

    print_report(run_backtest(cfg, candles_by_symbol))


if __name__ == "__main__":
    main()
