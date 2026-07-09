# 🦞 小龙虾模拟盘(Crayfish Paper Trader)

一个 **纸面交易(paper trading)** 的 AI 加密货币模拟盘:每个交易对是一只"小龙虾",
拥有独立的虚拟账户;AI(通过你的 **Codex 或 Claude Code 订阅**,无需 API key)定期
分析行情给出建议,确定性代码负责下单、止损和熔断,并用一个本地网页面板展示诚实的
统计数据(含与"买入持有"基准的对比)。

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
每 60 分钟一轮(可配):
  行情(ccxt 公共接口,无需交易所账号)
    → 技术指标摘要(SMA/RSI/ATR,纯 Python)
      → AI 决策(codex exec 或 claude -p,走订阅登录;失败自动降级为 HOLD)
        → 硬风控(仓位上限 / 止损钳制 / 移动止损 / 置信度门槛
                  / 当日 -5% 熔断 / 总回撤 -20% 永久停机)
          → 纸面撮合(0.1% 手续费 + 0.05% 滑点)
            → SQLite 落库 → 网页面板(含买入持有基准对比)
```

关键设计:**AI 只建议,不扣扳机。** 止损与移动止损先于 AI 决策执行;AI 输出不合法时
本轮观望;当日总权益回撤超 5% 熔断当天停止开新仓;总权益从峰值回撤超 20% 永久停止
开新仓等人工复核。这些都写死在 `trader/risk.py` 和 `trader/broker.py` 里,AI 说什么
都改不了。

## 决策引擎:用哪个 AI?

在 `config.yaml` 的 `llm.provider` 里一行切换,两者都走**订阅登录,不需要 API key**:

| provider | CLI | 适合 |
|---|---|---|
| `codex`(默认) | `codex exec` | ChatGPT/Codex 订阅额度充裕时先用它验证 |
| `claude` | `claude -p` | 验证可行后切换;Pro 订阅额度紧,把 `interval_minutes` 拉到 60+ |

模型与思考强度:codex 用 `codex_args`(如 `["-m", "gpt-5.5"]`)或 `~/.codex/config.toml`
配;claude 用 `claude_args`(如 `["--model", "opus"]`)。

## 环境要求

- Python 3.10+
- [Codex CLI](https://developers.openai.com/codex/cli) 或
  [Claude Code](https://claude.com/claude-code) 已安装并用订阅登录
  (终端跑一次 `codex exec "回复ok"` 或 `claude -p "回复ok"` 能出结果即可)

## 快速开始

```bash
cd crayfish-paper-trader
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1. 离线自检(mock 行情 + mock 决策,不联网、不消耗订阅额度)
python -m trader.bot --mock --once

# 2. 看面板
python dashboard/app.py          # 打开 http://127.0.0.1:8787

# 3. 回测:先用两年历史数据看规则基线的表现(几分钟出结果)
python -m trader.backtest --days 730

# 4. 真行情 + 规则决策(联网拉行情,但不消耗订阅额度)
python -m trader.bot --mock-llm --once

# 5. 正式模拟:真行情 + AI 决策(默认 codex,可切 claude),长期运行
python -m trader.bot
```

**注意:回测跑的是内置规则策略,不是 AI**——对上万根历史 K 线逐根调 LLM 既不现实也会
耗尽订阅额度。回测的作用是给基线定标、验证管线;AI 策略只能向前模拟,跑几周后拿面板
上的"策略 − 基准"来评判。

长期运行建议放在一台不关机的电脑或最便宜的云服务器上,用 `tmux` / `screen` /
`nohup python -m trader.bot &` 挂着即可。

## 配置(`config.yaml`)

| 项 | 默认 | 说明 |
|---|---|---|
| `symbols` | BTC/ETH/SOL | 每个交易对一只虾,可自行增减 |
| `interval_minutes` | 60 | 决策间隔;**订阅有 5 小时窗口限额,别低于 30** |
| `initial_balance` | 10000 | 每只虾的虚拟本金(USDT) |
| `risk.max_position_pct` | 20 | 单笔最多动用余额的 20%(再乘以 AI 置信度) |
| `risk.trailing_stop_pct` | 4 | 移动止损:从开仓后最高价回落 4% 落袋(0=关) |
| `risk.daily_loss_limit_pct` | 5 | 当日回撤熔断线(当天停止开新仓) |
| `risk.total_drawdown_limit_pct` | 20 | 总回撤开关:从峰值回撤 20% 永久停止开新仓 |
| `llm.provider` | codex | `codex` 或 `claude`,见上文 |

## 怎么判断"这套东西到底行不行"

面板(和 SQLite)里的指标都是诚实统计,建议至少跑 4–8 周再下结论:

- **"策略 − 基准"**(面板右上角):对比同期"买入持有不动"——跑不赢就没有存在价值;
- **最大回撤** ——盈利曲线的代价;
- **胜率 + 盈亏比** ——胜率 50% 但亏多赚少一样是输;
- **手续费合计** ——高频决策的隐形成本。

一个提前剧透:内置规则基线在合成数据一年回测里 **-8.2%**,同期买入持有 **+49.5%**,
1112 笔交易的手续费近 2900 USDT。这就是"看起来会动的策略"最常见的真实下场,
AI 策略必须证明自己比这强、且强过基准,才值得继续。

数据库就是 `crayfish.db`,用任何 SQLite 工具都能自己做分析。

## 测试

```bash
pytest tests/ -v
```

## 目录结构

```
trader/data.py     行情 + mock + 指标      trader/store.py    SQLite 存储
trader/llm.py      codex/claude 决策层     trader/bot.py      主循环
trader/broker.py   纸面撮合 + 移动止损     trader/backtest.py 历史回测
trader/risk.py     硬风控 + 熔断/总开关    dashboard/         网页面板(含基准对比)
```
