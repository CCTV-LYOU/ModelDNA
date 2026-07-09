"""纸面撮合:虚拟余额 + 手续费/滑点模拟。只支持现货做多,无杠杆。"""
from __future__ import annotations

import logging

from .store import Account, Store

log = logging.getLogger("crayfish.broker")


class PaperBroker:
    def __init__(self, store: Store, fee_pct: float, slippage_pct: float):
        self.store = store
        self.fee = fee_pct / 100
        self.slip = slippage_pct / 100

    def open_long(self, acc: Account, price: float, quote_amount: float,
                  stop_loss_pct: float, note: str = "") -> bool:
        exec_price = price * (1 + self.slip)
        fee = quote_amount * self.fee
        if quote_amount <= 0 or acc.balance < quote_amount + fee:
            log.info("[%s] 余额不足,放弃开仓(需要 %.2f,余额 %.2f)",
                     acc.symbol, quote_amount + fee, acc.balance)
            return False
        amount = quote_amount / exec_price
        acc.balance -= quote_amount + fee
        acc.pos_amount = amount
        acc.entry_price = exec_price
        acc.stop_price = exec_price * (1 - stop_loss_pct / 100)
        acc.peak_price = exec_price
        self.store.save_account(acc)
        self.store.add_trade(acc.symbol, "BUY", exec_price, amount, fee, None, note)
        log.info("[%s] 开仓 %.6g @ %.6g,止损 %.6g,费 %.2f",
                 acc.symbol, amount, exec_price, acc.stop_price, fee)
        return True

    def close_long(self, acc: Account, price: float, note: str = "") -> float | None:
        if not acc.has_position:
            return None
        exec_price = price * (1 - self.slip)
        proceeds = acc.pos_amount * exec_price
        fee = proceeds * self.fee
        pnl = (exec_price - acc.entry_price) * acc.pos_amount - fee
        acc.balance += proceeds - fee
        amount = acc.pos_amount
        acc.pos_amount = 0.0
        acc.entry_price = 0.0
        acc.stop_price = 0.0
        acc.peak_price = 0.0
        self.store.save_account(acc)
        self.store.add_trade(acc.symbol, "SELL", exec_price, amount, fee, pnl, note)
        log.info("[%s] 平仓 %.6g @ %.6g,盈亏 %+.2f(%s)",
                 acc.symbol, amount, exec_price, pnl, note or "主动")
        return pnl

    def apply_trailing_stop(self, acc: Account, price: float,
                            trailing_pct: float) -> None:
        """价格创开仓以来新高后,把止损线跟着抬上来(只升不降)。"""
        if trailing_pct <= 0 or not acc.has_position:
            return
        if price > acc.peak_price:
            acc.peak_price = price
        candidate = acc.peak_price * (1 - trailing_pct / 100)
        if candidate > acc.stop_price:
            acc.stop_price = candidate
            self.store.save_account(acc)
            log.info("[%s] 移动止损上调至 %.6g(峰值 %.6g)",
                     acc.symbol, acc.stop_price, acc.peak_price)

    def check_stop_loss(self, acc: Account, price: float) -> float | None:
        """价格触及止损线 => 强制平仓。返回已实现盈亏,未触发返回 None。"""
        if acc.has_position and price <= acc.stop_price:
            return self.close_long(acc, price, note="止损触发")
        return None

    @staticmethod
    def equity(acc: Account, price: float) -> float:
        return acc.balance + acc.pos_amount * price
