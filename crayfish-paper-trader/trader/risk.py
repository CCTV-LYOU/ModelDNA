"""硬风控:仓位上限、止损钳制、置信度门槛、当日亏损熔断,以及四项增强——
组合敞口上限(相关性防护)、ADX 行情过滤、ATR 波动率止损/仓位、
最短持仓 + BUY 二次确认 + 盈亏比止盈。

这些规则写死在代码里,AI 的任何建议都要先过这一层。
诚实提示:风控只能控制亏损的速度和上限(堵漏保命),
不能把一个负期望的策略变成正期望(不造优势)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .store import Account, Store

log = logging.getLogger("crayfish.risk")

MIN_ORDER_QUOTE = 10.0  # 最小下单额(USDT),低于此值视为无意义交易


class RiskManager:
    def __init__(self, store: Store, cfg: dict):
        self.store = store
        self.max_position_pct = float(cfg.get("max_position_pct", 20.0))
        self.default_stop = float(cfg.get("default_stop_loss_pct", 3.0))
        self.max_stop = float(cfg.get("max_stop_loss_pct", 10.0))
        self.min_confidence = float(cfg.get("min_confidence", 0.6))
        self.daily_loss_limit_pct = float(cfg.get("daily_loss_limit_pct", 5.0))
        self.trailing_stop_pct = float(cfg.get("trailing_stop_pct", 4.0))
        self.total_drawdown_limit_pct = float(cfg.get("total_drawdown_limit_pct", 20.0))
        # ---- 四项增强(对应值设 0 即关闭该项)----
        self.max_total_exposure_pct = float(cfg.get("max_total_exposure_pct", 30.0))
        self.adx_min_to_open = float(cfg.get("adx_min_to_open", 18.0))
        self.atr_stop_mult = float(cfg.get("atr_stop_mult", 2.0))
        self.risk_per_trade_pct = float(cfg.get("risk_per_trade_pct", 1.0))
        self.min_hold_hours = float(cfg.get("min_hold_hours", 4.0))
        self.buy_confirm_bars = int(cfg.get("buy_confirm_bars", 2))
        self.take_profit_rr = float(cfg.get("take_profit_rr", 2.0))

    def position_size(self, balance: float, confidence: float,
                      stop_loss_pct: float | None = None) -> float:
        """下单额 = min(余额×仓位上限×置信度, 单笔风险预算反推的仓位)。

        风险预算:止损打掉时最多亏 balance × risk_per_trade_pct%。
        止损距离越宽(波动越大)仓位越小——波动率自适应仓位。
        """
        quote = balance * self.max_position_pct / 100 * confidence
        if self.risk_per_trade_pct > 0 and stop_loss_pct and stop_loss_pct > 0:
            risk_budget = balance * self.risk_per_trade_pct / 100
            quote = min(quote, risk_budget / (stop_loss_pct / 100))
        return quote if quote >= MIN_ORDER_QUOTE else 0.0

    def clamp_stop_loss(self, stop_loss_pct: float | None) -> float:
        if stop_loss_pct is None or stop_loss_pct <= 0:
            return self.default_stop
        return min(max(stop_loss_pct, 0.5), self.max_stop)

    def stop_loss_pct_for(self, price: float, atr_value: float | None,
                          ai_suggestion: float | None) -> float:
        """有效止损百分比:启用 ATR 时 = atr_stop_mult × ATR14 / 价格
        (波动大止损宽、波动小止损紧),否则用 AI 建议;都会钳制到
        [0.5, max_stop_loss_pct]。"""
        if self.atr_stop_mult > 0 and atr_value and price > 0:
            return self.clamp_stop_loss(self.atr_stop_mult * atr_value / price * 100)
        return self.clamp_stop_loss(ai_suggestion)

    def regime_ok(self, adx_value: float | None) -> bool:
        """ADX 行情过滤:低于门槛视为震荡市,不开新的趋势单
        (趋势策略在横盘里会被反复止损+手续费磨损)。数据不足同样不开,保守优先。"""
        if self.adx_min_to_open <= 0:
            return True
        return adx_value is not None and adx_value >= self.adx_min_to_open

    def exposure_headroom(self, total_equity: float, exposure_value: float) -> float:
        """组合敞口余量(USDT)。BTC/ETH/SOL 常年相关性 0.8+,几乎同涨同跌,
        所以把所有交易对的持仓市值合并当作同一个方向赌注,限制其占总权益的
        比例——否则"三只虾分散风险"是假象,实际是三倍同向敞口。"""
        if self.max_total_exposure_pct <= 0:
            return float("inf")
        return max(0.0, total_equity * self.max_total_exposure_pct / 100 - exposure_value)

    def min_hold_ok(self, acc: Account, now_ts_ms: float) -> bool:
        """最短持仓:开仓后 N 小时内忽略主动 SELL,压住"反复进出"的手续费磨损。
        止损/止盈/移动止损是安全出口,永远不受此限制。"""
        if self.min_hold_hours <= 0 or not acc.has_position or acc.opened_ts_ms <= 0:
            return True
        return (now_ts_ms - acc.opened_ts_ms) / 3_600_000 >= self.min_hold_hours

    def confidence_ok(self, confidence: float) -> bool:
        return confidence >= self.min_confidence

    def kill_switch_tripped(self, current_total: float) -> bool:
        """总权益从历史峰值回撤超过阈值 => 永久停止开新仓,等人工复核。

        基于 equity 历史计算,权益不回升就一直处于触发状态;确认要继续,
        调大 total_drawdown_limit_pct 或换新数据库重新开始。
        """
        if self.total_drawdown_limit_pct <= 0:
            return False
        peak = self.store.peak_total()
        if not peak or peak <= 0:
            return False
        drawdown_pct = (peak - current_total) / peak * 100
        if drawdown_pct >= self.total_drawdown_limit_pct:
            log.warning("总回撤 %.2f%% ≥ 开关线 %.2f%%(峰值 %.2f),停止开新仓,请人工复核",
                        drawdown_pct, self.total_drawdown_limit_pct, peak)
            return True
        return False

    def circuit_breaker_tripped(self, current_total: float) -> bool:
        """当日(UTC)总权益回撤超过阈值 => 熔断,今天不再开新仓。"""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_start = self.store.day_start_total(day)
        if not day_start or day_start <= 0:
            return False
        drawdown_pct = (day_start - current_total) / day_start * 100
        if drawdown_pct >= self.daily_loss_limit_pct:
            log.warning("当日回撤 %.2f%% ≥ 熔断线 %.2f%%,今日停止开新仓",
                        drawdown_pct, self.daily_loss_limit_pct)
            return True
        return False
