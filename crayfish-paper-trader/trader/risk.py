"""硬风控:仓位上限、止损钳制、置信度门槛、当日亏损熔断。

这些规则写死在代码里,AI 的任何建议都要先过这一层。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .store import Store

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

    def position_size(self, balance: float, confidence: float) -> float:
        """下单额 = 余额 × 仓位上限 × 置信度,再扣掉最小门槛。"""
        quote = balance * self.max_position_pct / 100 * confidence
        return quote if quote >= MIN_ORDER_QUOTE else 0.0

    def clamp_stop_loss(self, stop_loss_pct: float | None) -> float:
        if stop_loss_pct is None or stop_loss_pct <= 0:
            return self.default_stop
        return min(max(stop_loss_pct, 0.5), self.max_stop)

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
