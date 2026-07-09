# 🦞 小龙虾模拟盘(Crayfish Paper Trader)

一个 **纸面交易(paper trading)** 的 AI 加密货币模拟盘:每个交易对是一只"小龙虾",
拥有独立的虚拟账户;AI(通过你的 **Claude Code 订阅**,无需 API key)定期分析行情给出
建议,确定性代码负责下单、止损和熔断,并用一个本地网页面板展示诚实的统计数据。

> **它不碰真钱。** 所有成交都是本地 SQLite 里的纸面撮合(含手续费和滑点模拟)。
> 它的用途是:在投入任何真实资金之前,用几周到几个月的时间验证"AI 交易"到底有没有用。

## ⚠️ 先读这个

- **模拟盘盈利 ≠ 实盘盈利。** 模拟盘没有流动性冲击、没有情绪、滑点只是估算。
- **LLM 没有天然的市场优势。** 短视频里"AI 军团日赚 XX 万"的面板可以随手伪造,
  不要为任何此类内容付费。
- **合约(杠杆)不在本项目范围内**,本项目只模拟现货做多——这是刻意的。
- 中国大陆监管禁止虚拟货币交易炒作活动,实盘的法律与资金风险请自行评估。

## 工作原理

```
每 45 分钟一轮(可配):
  行情(ccxt 公共接口,无需交易所账号)
    → 技术指标摘要(SMA/RSI/ATR,纯 Python)
      → AI 决策(claude -p,走订阅登录;失败自动降级为 HOLD)
        → 硬风控(仓位上限 / 止损钳制 / 置信度门槛 / 当日 -5% 熔断)
          → 纸面撮合(0.1% 手续费 + 0.05% 滑点)
            → SQLite 落库 → 网页面板
```

关键设计:**AI 只建议,不扣扳机。** 止损检查先于 AI 决策执行;AI 输出不合法时本轮观望;
当日总权益回撤超过 5% 自动熔断停止开新仓。这些都写死在 `trader/risk.py` 和
`trader/bot.py` 里,AI 说什么都改不了。

## 环境要求

- Python 3.10+
- [Claude Code](https://claude.com/claude-code) 已安装并用你的订阅登录
  (终端跑一次 `claude -p "回复ok"` 能出结果即可;**不需要 API key**)

## 快速开始

```bash
cd crayfish-paper-trader
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. 离线自检(mock 行情 + mock 决策,不联网、不消耗订阅额度)
python -m trader.bot --mock --once

# 2. 看面板
python dashboard/app.py          # 打开 http://127.0.0.1:8787

# 3. 真行情 + 规则决策(联网拉行情,但不消耗订阅额度)
python -m trader.bot --mock-llm --once

# 4. 正式模拟:真行情 + Claude 决策,长期运行
python -m trader.bot
```

长期运行建议放在一台不关机的电脑或最便宜的云服务器上,用 `tmux` / `screen` /
`nohup python -m trader.bot &` 挂着即可。

## 配置(`config.yaml`)

| 项 | 默认 | 说明 |
|---|---|---|
| `symbols` | BTC/ETH/SOL | 每个交易对一只虾,可自行增减 |
| `interval_minutes` | 45 | 决策间隔;**订阅有 5 小时窗口限额,别低于 30** |
| `initial_balance` | 10000 | 每只虾的虚拟本金(USDT) |
| `risk.max_position_pct` | 20 | 单笔最多动用余额的 20%(再乘以 AI 置信度) |
| `risk.daily_loss_limit_pct` | 5 | 当日回撤熔断线 |
| `llm.command` | claude | 换成其他兼容 CLI 也可以(如 codex) |

## 怎么判断"这套东西到底行不行"

面板(和 SQLite)里的指标都是诚实统计,建议至少跑 4–8 周再下结论:

- **总盈亏 %** 对比同期"买入持有 BTC 不动"——跑不赢就没有存在价值;
- **最大回撤** ——盈利曲线的代价;
- **胜率 + 平均盈亏比** ——胜率 50% 但亏多赚少一样是输;
- **手续费合计** ——高频决策的隐形成本。

数据库就是 `crayfish.db`,用任何 SQLite 工具都能自己做分析。

## 测试

```bash
pytest tests/ -v
```

## 目录结构

```
trader/data.py    行情 + mock + 指标      trader/store.py  SQLite 存储
trader/llm.py     claude -p 决策层        trader/bot.py    主循环
trader/broker.py  纸面撮合                dashboard/       网页面板
trader/risk.py    硬风控
```
