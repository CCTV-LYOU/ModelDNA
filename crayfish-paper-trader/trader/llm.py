"""AI 决策层:通过订阅制 CLI 的无头模式获取交易建议,无需任何 API key。

支持两个 provider,在 config.yaml 的 llm.provider 里切换:
  - codex : `codex exec "<prompt>"`(ChatGPT/Codex 订阅)
  - claude: `claude -p "<prompt>" --output-format json`(Claude Pro/Max 订阅)

LLM 只负责"分析并建议",输出严格 JSON;下单、止损、熔断全部由确定性代码执行。
任何失败(命令不存在 / 超时 / 输出不合法)一律降级为 HOLD,机器人不崩。
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass

from .data import Candle
from .store import Account

log = logging.getLogger("crayfish.llm")

VALID_ACTIONS = ("BUY", "SELL", "HOLD")


@dataclass
class Decision:
    action: str = "HOLD"
    confidence: float = 0.0
    stop_loss_pct: float | None = None
    reason: str = ""
    source: str = "fallback"


def build_prompt(symbol: str, candles: list[Candle], ind: dict, acc: Account) -> str:
    lines = [
        f"{int(ts)},{o:.6g},{h:.6g},{l:.6g},{c:.6g},{v:.6g}"
        for ts, o, h, l, c, v in candles[-24:]
    ]
    if acc.has_position:
        pos = (f"持有多仓 {acc.pos_amount:.6g},开仓价 {acc.entry_price:.6g},"
               f"止损价 {acc.stop_price:.6g}")
    else:
        pos = "当前空仓"
    fmt = lambda v: f"{v:.4g}" if isinstance(v, (int, float)) else "N/A"  # noqa: E731
    return f"""你是一个加密货币现货模拟盘分析助手。只做分析,不解释交易之外的内容。

交易对: {symbol}(1小时K线,只允许做多现货,无杠杆)
账户状态: {pos},可用余额 {acc.balance:.2f} USDT

最近24根K线(timestamp_ms,open,high,low,close,volume):
{chr(10).join(lines)}

技术指标: 最新价={fmt(ind['last_price'])} SMA10={fmt(ind['sma_fast'])} \
SMA30={fmt(ind['sma_slow'])} RSI14={fmt(ind['rsi14'])} ATR14={fmt(ind['atr14'])} \
ADX14={fmt(ind.get('adx14'))} 24小时涨跌={fmt(ind['change_24h_pct'])}%

请基于以上数据给出下一步操作建议。要求:
1. 只输出一个 JSON 对象,不要输出任何其他文字、markdown 或代码块标记。
2. 格式: {{"action": "BUY|SELL|HOLD", "confidence": 0.0到1.0, \
"stop_loss_pct": 建议止损百分比(数字,例如3表示3%), "reason": "不超过60字的中文理由"}}
3. 空仓时 SELL 无意义请用 HOLD;已持仓时 BUY 无意义请用 HOLD 或 SELL。
4. 没有明确信号就诚实地 HOLD,不要为了交易而交易。"""


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个平衡的 JSON 对象(容忍代码块等噪音)。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def extract_last_json(text: str) -> dict | None:
    """抠出最后一个平衡的 JSON 对象。codex exec 的最终回答在输出末尾,
    前面可能混有含花括号的日志,所以从后往前找。"""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if text[i] == "}":
                depth += 1
            elif text[i] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:end + 1])
                    except json.JSONDecodeError:
                        break
        end = text.rfind("}", 0, end)
    return None


def normalize(raw: dict | None, source: str) -> Decision:
    if not isinstance(raw, dict):
        return Decision(reason="模型输出不合法,本轮观望", source="fallback")
    action = str(raw.get("action", "HOLD")).strip().upper()
    if action not in VALID_ACTIONS:
        action = "HOLD"
    try:
        confidence = min(max(float(raw.get("confidence", 0.0)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0
    stop = raw.get("stop_loss_pct")
    try:
        stop = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        stop = None
    reason = str(raw.get("reason", ""))[:200]
    return Decision(action=action, confidence=confidence,
                    stop_loss_pct=stop, reason=reason, source=source)


def _run_cli(argv: list[str], timeout: int, name: str) -> str | None:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        log.warning("找不到 `%s` 命令,请先安装并登录;本轮 HOLD", argv[0])
        return None
    except subprocess.TimeoutExpired:
        log.warning("%s 响应超时(%ss);本轮 HOLD", name, timeout)
        return None
    if proc.returncode != 0:
        log.warning("%s 退出码 %s: %s", name, proc.returncode, proc.stderr[:300])
        return None
    return proc.stdout


def ask_claude(prompt: str, command: str = "claude", timeout: int = 240,
               extra_args: list[str] | None = None) -> Decision:
    """Claude Code 无头模式。Pro/Max 订阅登录即可,不需要 API key。"""
    argv = [command, "-p", prompt, "--output-format", "json"] + (extra_args or [])
    stdout = _run_cli(argv, timeout, "claude")
    if stdout is None:
        return Decision(reason="claude 调用失败", source="fallback")
    # --output-format json 返回一层信封,真正的回答在 result 字段里
    envelope = extract_json(stdout)
    text = envelope.get("result", "") if isinstance(envelope, dict) else stdout
    return normalize(extract_json(text or ""), source="claude")


def ask_codex(prompt: str, command: str = "codex", timeout: int = 240,
              extra_args: list[str] | None = None) -> Decision:
    """Codex CLI 无头模式(`codex exec`)。ChatGPT 订阅登录即可,不需要 API key。"""
    argv = [command, "exec"] + (extra_args or []) + [prompt]
    stdout = _run_cli(argv, timeout, "codex")
    if stdout is None:
        return Decision(reason="codex 调用失败", source="fallback")
    return normalize(extract_last_json(stdout), source="codex")


def ask(llm_cfg: dict, prompt: str) -> Decision:
    """按 config 的 llm.provider 分发到对应 CLI。"""
    provider = str(llm_cfg.get("provider", "codex")).lower()
    timeout = int(llm_cfg.get("timeout_seconds", 240))
    if provider == "claude":
        return ask_claude(prompt, llm_cfg.get("claude_command", "claude"),
                          timeout, llm_cfg.get("claude_args"))
    if provider == "codex":
        return ask_codex(prompt, llm_cfg.get("codex_command", "codex"),
                         timeout, llm_cfg.get("codex_args"))
    log.warning("未知 provider `%s`,本轮 HOLD", provider)
    return Decision(reason=f"未知 provider {provider}", source="fallback")


def mock_decision(ind: dict, acc: Account) -> Decision:
    """离线验证用的简单动量规则,不代表任何有效策略。"""
    fast, slow, r = ind.get("sma_fast"), ind.get("sma_slow"), ind.get("rsi14")
    if fast is None or slow is None or r is None:
        return Decision(reason="数据不足", source="mock")
    if not acc.has_position and fast > slow and r < 68:
        return Decision("BUY", 0.7, 3.0, "mock: 短均线上穿且RSI未超买", "mock")
    if acc.has_position and (r > 72 or fast < slow):
        return Decision("SELL", 0.8, None, "mock: 超买或动量转弱,离场", "mock")
    return Decision(reason="mock: 无信号,观望", source="mock")
